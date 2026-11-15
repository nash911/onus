"""`onus eval`: aggregate-only evaluation over a split root. (Phase 7)

Inputs: a split root (`papers/` and `labels/`) and a results directory written by `onus analyze`. Outputs go to
`--out` only (metrics.json, aggregate.md, per_paper.jsonl, mutations/, baseline/, errors.json); nothing is
written into the split root, and for a held-out root (outside the repository) `--out` and `ONUS_VAR_DIR` must
be outside the repository too. The terminal shows the fingerprint, the plan, the expected cost, and then the
aggregate table only: no paper ids, titles, quotes or per-paper rows.

Mutation plan `NxK`: K mutation types (the first K of `mutate.TYPES` for K >= 5, else K types drawn with the
seed) and, per type, up to N papers drawn with the seed from those meeting that type's preconditions. Each
mutated Document is analysed once (1 run, run index 0) under its own paper id `<id>~<type>`, so
ONUS_MAX_EUR_PER_PAPER caps each mutated run. Mutation runs and baseline results are resumable: a stored output
with the same fingerprint (and the same mutation) is reused.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from onus.llm.client import BudgetExceeded
from onus.llm.costs import cost_eur
from onus.schemas.document import Document
from onus.schemas.result import RunResult

from .baseline import BaselineResult, baseline_path, paper_text, run_baseline
from .metrics import (
    code_agreement,
    collateral,
    detect,
    distributions,
    recall_table,
    render_aggregate,
    stability,
)
from .mutate import TYPES, Mutation, mutate, table_renderings, widen_candidates
from .truth import SplitLabels, read_labels

DEFAULT_RUN_EUR = 0.10  # Phase 6 cost test: mean € per paper-run, uncached
DEFAULT_RUN_MIN = 4.1
BASELINE_OUT_TOKENS = 5000
Say = Callable[[str], None]


class EvalError(RuntimeError):
    pass


def parse_plan(s: str | None) -> tuple[int, int] | None:
    if not s:
        return None
    try:
        n, k = (int(x) for x in s.lower().split("x"))
    except ValueError as e:
        raise EvalError(f"--mutations must look like 10x5, got {s!r}") from e
    if n < 1 or not 1 <= k <= len(TYPES):
        raise EvalError(f"--mutations: N >= 1 and 1 <= K <= {len(TYPES)}")
    return n, k


def plan_types(k: int, seed: int) -> list[str]:
    if k >= len(TYPES):
        return list(TYPES)
    pick = set(random.Random(f"{seed}:types").sample(TYPES, k))
    return [t for t in TYPES if t in pick]


def load_results(results_dir: Path) -> list[RunResult]:
    out = []
    for f in sorted(results_dir.glob("*.json")):
        try:
            out.append(RunResult.model_validate_json(f.read_text()))
        except ValueError:
            continue
    return out


def _within(p: Path, root: Path) -> bool:
    p, root = p.resolve(), root.resolve()
    return p == root or root in p.parents


def guard_paths(split_root: Path, var_dir: Path, *outputs: Path) -> None:
    """Hard rule 4 plus: nothing is ever written into the split root."""
    from onus.config import UnsafePathsError, assert_safe_paths

    try:
        assert_safe_paths(split_root, var_dir, extra_outputs=tuple(outputs))
    except UnsafePathsError as e:
        raise EvalError(str(e)) from e
    for p in (var_dir, *outputs):
        if _within(p, split_root):
            raise EvalError(f"{p} is inside the split root {split_root}; outputs must go elsewhere.")


# ---------------------------------------------------------------- precondition scan (no network)


async def scan(docs: dict[str, Document], results: dict[str, RunResult], types: list[str],
               seed: int) -> dict[str, list[str]]:
    """Papers meeting each type's preconditions (deterministic types are applied without an LLM)."""
    out: dict[str, list[str]] = {t: [] for t in types}
    for pid in sorted(docs):
        for t in types:
            if t == "widen_scope":
                ok = bool(widen_candidates(docs[pid], results[pid]))
            else:
                _, d = await mutate(docs[pid], results[pid], t, seed)
                ok = d is not None
            if ok:
                out[t].append(pid)
    return out


def choose(eligible_by_type: dict[str, list[str]], n: int, seed: int) -> list[tuple[str, str]]:
    plan = []
    for t, pids in eligible_by_type.items():
        pool = sorted(pids)
        random.Random(f"{seed}:{t}:papers").shuffle(pool)
        plan += [(pid, t) for pid in pool[:n]]
    return plan


# ---------------------------------------------------------------- cost estimate


def per_run_eur(results: list[RunResult]) -> float:
    spent = [r.cost.get("eur", 0.0) / max(r.runs, 1) for r in results]
    mean = sum(spent) / len(spent) if spent else 0.0
    return mean if mean >= 0.02 else DEFAULT_RUN_EUR  # cached results under-report what a fresh run costs


def baseline_eur(doc: Document, judge_model: str, n_claims: int) -> float:
    chars = len(paper_text(doc)) + len(table_renderings(doc)) + 220 * n_claims + 1500
    try:
        return cost_eur(judge_model, int(chars / 3.6), BASELINE_OUT_TOKENS + 60 * n_claims)
    except Exception:  # noqa: BLE001
        return 0.03


@dataclass
class Estimate:
    mutation_runs: int
    baseline_calls: int
    eur: float
    minutes: float
    notes: list[str] = field(default_factory=list)


def estimate(results: list[RunResult], docs: dict[str, Document], plan: list[tuple[str, str]], with_baseline: bool,
             judge_model: str, concurrency: int) -> Estimate:
    run_eur = per_run_eur(results)
    eur = run_eur * len(plan)
    calls = 0
    if with_baseline:
        by = {r.paper["id"]: r for r in results}
        for pid, doc in docs.items():
            eur += baseline_eur(doc, judge_model, len(by[pid].claims))
            calls += 1
        for pid, _ in plan:
            eur += baseline_eur(docs[pid], judge_model, len(by[pid].claims))
            calls += 1
    waves = -(-len(plan) // max(concurrency, 1))
    minutes = waves * DEFAULT_RUN_MIN + (-(-calls // max(concurrency, 1))) * 1.5
    return Estimate(len(plan), calls, eur, minutes, [f"€{run_eur:.3f} per mutated paper-run"])


# ---------------------------------------------------------------- the run


@dataclass
class EvalInputs:
    split_root: Path
    results_dir: Path
    out: Path
    var_dir: Path
    plan: tuple[int, int] | None
    with_baseline: bool
    seed: int = 7
    concurrency: int = 4


def _mutation_paths(out: Path, mut: Mutation) -> tuple[Path, Path]:
    d = out / "mutations"
    return d / f"{mut.mutated_id}.mutation.json", d / f"{mut.mutated_id}.result.json"


def _reuse(path: Path, fp: str) -> RunResult | None:
    if not path.is_file():
        return None
    try:
        r = RunResult.model_validate_json(path.read_text())
    except ValueError:
        return None
    return r if r.fingerprint == fp else None


def _reuse_baseline(path: Path, fp: str) -> BaselineResult | None:
    if not path.is_file():
        return None
    try:
        b = BaselineResult.model_validate_json(path.read_text())
    except ValueError:
        return None
    return b if b.fingerprint == fp and not b.errors else None


async def run_eval(inp: EvalInputs, llm, *, fingerprint: str, judge_model: str, say: Say = print,
                   docs_loader: Callable[[Path], Document] | None = None) -> dict:
    from onus.ingest.document_builder import paper_dirs
    from onus.pipeline import analyze_doc_runs, load_or_ingest

    t0 = time.monotonic()
    started = datetime.now(UTC).isoformat()
    guard_paths(inp.split_root, inp.var_dir, inp.out)
    labels = read_labels(inp.split_root)
    dirs = {d.name: d for d in paper_dirs(inp.split_root)}
    results = [r for r in load_results(inp.results_dir) if r.paper["id"] in dirs]
    if not results:
        raise EvalError("no results in --results match a paper under the split root")
    stale = sum(1 for r in results if r.fingerprint != fingerprint)
    load = docs_loader or (lambda d: load_or_ingest(d, inp.var_dir))
    docs = {r.paper["id"]: load(dirs[r.paper["id"]]) for r in results}
    by = {r.paper["id"]: r for r in results}

    plan: list[tuple[str, str]] = []
    attempted: Counter = Counter()
    if inp.plan:
        n, k = inp.plan
        types = plan_types(k, inp.seed)
        elig = await scan(docs, by, types, inp.seed)
        plan = choose(elig, n, inp.seed)
        attempted = Counter(t for _, t in plan)
        for t in types:
            attempted.setdefault(t, 0)
    est = estimate(results, docs, plan, inp.with_baseline, judge_model, inp.concurrency)
    from onus.version import harness_fingerprint

    hfp = harness_fingerprint()
    say(f"pipeline fingerprint {fingerprint}, harness fingerprint {hfp}; {len(results)} papers with results"
        + (f" ({stale} produced by another fingerprint)" if stale else ""))
    if inp.plan:
        say(f"mutation plan {inp.plan[0]}x{inp.plan[1]} (seed {inp.seed}): "
            + ", ".join(f"{t} {attempted[t]}" for t in attempted) + f"; {len(plan)} mutated runs")
    say(f"expected cost ≈ €{est.eur:.2f} ({est.mutation_runs} mutated runs, {est.baseline_calls} baseline calls; "
        f"{est.notes[0]}), ≈ {est.minutes:.0f} min at concurrency {inp.concurrency}. "
        f"Per-paper cap ONUS_MAX_EUR_PER_PAPER applies to each run.")

    inp.out.mkdir(parents=True, exist_ok=True)
    errors: list[dict] = []
    sem = asyncio.Semaphore(max(1, inp.concurrency))
    stop = asyncio.Event()
    done = Counter()

    async def one_mutation(pid: str, mtype: str):
        async with sem:
            if stop.is_set():
                return None
            try:
                mut, mdoc = await mutate(docs[pid], by[pid], mtype, inp.seed, llm)
                mpath, rpath = _mutation_paths(inp.out, mut)
                mpath.parent.mkdir(parents=True, exist_ok=True)
                same = mpath.is_file() and mpath.read_text() == mut.model_dump_json(indent=1)
                mpath.write_text(mut.model_dump_json(indent=1))
                if mdoc is None:
                    return mut, None, None
                mres = _reuse(rpath, fingerprint) if same else None
                if mres is None:
                    mres = await analyze_doc_runs(mdoc, 1, "batch", llm=llm, fingerprint=fingerprint)
                    rpath.write_text(mres.model_dump_json(indent=1))
                mb = None
                if inp.with_baseline:
                    bpath = baseline_path(inp.out / "baseline" / "mutations", mut.mutated_id)
                    mb = _reuse_baseline(bpath, fingerprint) if same and mres is not None else None
                    if mb is None or {c.claim_id for c in mb.claims} != {c.id for c in mres.claims}:
                        mb = await run_baseline(mdoc, mres, llm, fingerprint=fingerprint)
                        bpath.parent.mkdir(parents=True, exist_ok=True)
                        bpath.write_text(mb.model_dump_json(indent=1))
                done["mutations"] += 1
                say(f"  mutation runs done: {done['mutations']}/{len(plan)}")
                return mut, mres, mb
            except BudgetExceeded as e:
                stop.set()
                errors.append({"paper": pid, "type": mtype, "error": str(e)[:300]})
                return None
            except Exception as e:  # noqa: BLE001 - recorded in errors.json, counted in the table
                errors.append({"paper": pid, "type": mtype, "error": f"{type(e).__name__}: {e}"[:300]})
                return None

    async def one_baseline(pid: str):
        async with sem:
            if stop.is_set():
                return None
            bpath = baseline_path(inp.out / "baseline", pid)
            b = _reuse_baseline(bpath, fingerprint)
            if b is not None:
                return b
            try:
                b = await run_baseline(docs[pid], by[pid], llm, fingerprint=fingerprint)
            except BudgetExceeded as e:
                stop.set()
                errors.append({"paper": pid, "type": "baseline", "error": str(e)[:300]})
                return None
            bpath.parent.mkdir(parents=True, exist_ok=True)
            bpath.write_text(b.model_dump_json(indent=1))
            return b

    base_tasks = [one_baseline(pid) for pid in sorted(docs)] if inp.with_baseline else []
    outs = await asyncio.gather(*base_tasks, *[one_mutation(pid, t) for pid, t in plan])
    obase = {b.paper_id: b for b in outs[: len(base_tasks)] if b is not None}
    mouts = [o for o in outs[len(base_tasks):] if o is not None]

    dets = []
    per_paper: dict[str, dict] = {pid: {"paper_id": pid, "mutations": []} for pid in docs}
    for mut, mres, mb in mouts:
        if mres is None:
            if mut.error:  # a model call failed: a failed run, not an inapplicable paper
                errors.append({"paper": mut.paper_id, "type": mut.type, "error": mut.reason})
            else:
                attempted[mut.type] -= 1  # became inapplicable when applied with the LLM (rewrite not verified)
            per_paper[mut.paper_id]["mutations"].append({"type": mut.type, "applied": False, "reason": mut.reason})
            continue
        det = detect(mut, mres, by[mut.paper_id], mb, obase.get(mut.paper_id))
        dets.append(det)
        per_paper[mut.paper_id]["mutations"].append(
            {"type": mut.type, "applied": True, "reason": mut.reason, "onus": det.onus, "baseline": det.baseline,
             "target_found": det.target_found, "collateral": det.collateral, "note": det.note})
    spend = _spend_since(started, llm)
    metrics = {
        "fingerprint": fingerprint, "harness_fingerprint": hfp, "created_at": started, "seed": inp.seed,
        "mutation_plan": f"{inp.plan[0]}x{inp.plan[1]}" if inp.plan else None,
        "runs": sorted({r.runs for r in results}),
        "results_fingerprints_other": stale,
        "labels": {"papers_labelled": sum(1 for pid in docs if pid in labels.papers)},
        "distributions": distributions(results, obase or None),
        "stability": stability(results),
        "code_agreement": code_agreement(results, labels),
        "mutations": {"recall": recall_table(dets, attempted), "collateral": collateral(dets),
                      "failed": len([e for e in errors if e["type"] != "baseline"])} if inp.plan else None,
        "baseline_failed": len([e for e in errors if e["type"] == "baseline"]),
        "cost": {"eur": spend, "minutes": (time.monotonic() - t0) / 60, "estimate_eur": est.eur},
    }
    (inp.out / "metrics.json").write_text(json.dumps(metrics, indent=1, default=str))
    for pid, r in by.items():
        per_paper[pid].update({"counts": r.summary.get("counts"), "flagged_share": r.summary.get("flagged_share"),
                               "baseline_not_supported": sum(1 for c in obase[pid].claims if c.label == "not_supported")
                               if pid in obase else None})
    with (inp.out / "per_paper.jsonl").open("w") as f:
        for row in per_paper.values():
            f.write(json.dumps(row, default=str) + "\n")
    (inp.out / "errors.json").write_text(json.dumps(errors, indent=1))
    table = render_aggregate(metrics)
    (inp.out / "aggregate.md").write_text(table + "\n")
    metrics["_table"] = table
    metrics["_errors"] = len(errors)
    return metrics


def _spend_since(started: str, llm) -> float:
    ledger = getattr(llm, "ledger", None)
    if ledger is None:
        return 0.0
    return sum(float(r.get("eur", 0.0)) for r in ledger.rows() if r.get("ts", "") >= started)


# ---------------------------------------------------------------- --check (dry run)


def check_inputs(split_root: Path, results_dir: Path | None, var_dir: Path, out: Path | None,
                 docs_loader: Callable[[Path], Document] | None = None, fingerprint: str | None = None) -> list[str]:
    """Counts only: no network, no spend. Raises EvalError on a blocking problem."""
    from onus.ingest.document_builder import paper_dirs

    if not split_root.is_dir():
        raise EvalError(f"split root {split_root} does not exist")
    guard_paths(split_root, var_dir, *([out] if out else []))
    lines = []
    dirs = paper_dirs(split_root)
    with_code = sum(1 for d in dirs if (d / "code").is_dir())
    lines.append(f"papers: {len(dirs)} with paper.pdf ({with_code} with code/)")
    labels: SplitLabels = read_labels(split_root)
    if not labels.root.is_dir():
        raise EvalError(f"no labels/ under {split_root}")
    ids = {d.name for d in dirs}
    lines.append(f"labels: papers.csv {len(labels.papers)} rows, {len(ids & set(labels.papers))} match a paper; "
                 f"reviews {len(labels.reviews & ids)}; code_tests.csv {len(labels.code_tests)} rows")
    status = Counter(labels.status(pid) or "missing" for pid in ids)
    lines.append("labels: status " + ", ".join(f"{k} {v}" for k, v in sorted(status.items())))
    scored = sum(1 for pid in ids if labels.score(pid, "ai_overall_mean") is not None)
    hum = sum(1 for pid in ids if labels.score(pid, "human_overall_mean") is not None)
    lines.append(f"labels: AI reviewer score {scored}, human score {hum}")
    audit = Counter(labels.audits.get(pid, "") or "missing" for pid in ids)
    usable = sum(1 for pid in ids if labels.audit_class(pid) is not None)
    lines.append("labels: code audit " + ", ".join(f"{k} {v}" for k, v in sorted(audit.items()))
                 + f" -> {usable} usable for code agreement (ERROR and NO CODEBASE excluded)")
    if results_dir is not None:
        res = [r for r in load_results(results_dir) if r.paper["id"] in ids] if results_dir.is_dir() else []
        lines.append(f"results: {len(res)} of {len(ids)} papers"
                     + (f"; runs {sorted(Counter(r.runs for r in res).items())}" if res else "")
                     + (f"; {sum(1 for r in res if r.fingerprint == fingerprint)} with the current fingerprint"
                        if fingerprint and res else "")
                     + f"; code-check results {sum(1 for r in res if r.code.static or r.code.reruns)}")
        if res and docs_loader is not None:
            by = {r.paper["id"]: r for r in res}
            dmap = {d.name: d for d in dirs}
            docs = {pid: docs_loader(dmap[pid]) for pid in by}
            elig = asyncio.run(scan(docs, by, list(TYPES), 7))
            lines.append("mutation preconditions met: " + ", ".join(f"{t} {len(v)}" for t, v in elig.items()))
    lines.append(f"ONUS_VAR_DIR {var_dir}" + (f"; --out {out}" if out else "") + ": paths OK")
    return lines
