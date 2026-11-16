"""`onus codecheck ...`: code-check commands (Phase 10).

A Typer sub-app registered from onus/cli.py, so Phase 10 adds commands here without touching cli.py, which is
part of the P0 pipeline fingerprint. codecheck/ is excluded from that fingerprint (code-v1 is versioned
separately, codecheck/version.py).

    onus codecheck --data-root <root> --results <dir> [--rerun-ids file | --rerun-all] [--static-only]
    onus codecheck plan --data-root <root> --results <dir> [--rerun-ids file | --rerun-all]   # no spend
    onus codecheck version
    onus freeze --code-version X.Y.Z    (calls freeze_code below)

The run updates each RunResult in --results in place (RunResult.code, the claim checks, verdicts recomputed with
the frozen rules, code_version). The P0 result is kept once in <results>/pre_code/ and every run starts from it,
so the command can be repeated. Static reports and re-run reports are kept in <results>/code/; a stored re-run
report is reused unless --force (no repeated sandbox or agent spend).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Static paper-code checks and sandboxed re-runs (code-v1).", invoke_without_command=True)
console = Console()

DataRoot = Annotated[Path | None, typer.Option("--data-root", help="Split root (reads <root>/papers/ only)")]
Results = Annotated[Path | None, typer.Option("--results", help="Directory of RunResult JSON files (updated)")]
RerunIds = Annotated[Path | None, typer.Option("--rerun-ids", help="File with one paper id per line to re-run")]
RerunAll = Annotated[bool, typer.Option("--rerun-all", help="Re-run every paper with code and numeric targets")]


def _ids_file(p: Path | None) -> set[str]:
    if p is None:
        return set()
    return {ln.strip().split(",")[0] for ln in p.read_text().splitlines() if ln.strip() and not ln.startswith("#")}


def _result_files(results: Path) -> list[Path]:
    return sorted(p for p in results.glob("*.json") if not p.name.startswith(("_", ".")) and p.stem != "metrics")


def _load_p0(results: Path, path: Path):
    from onus.schemas.result import RunResult

    pre = results / "pre_code" / path.name
    if not pre.is_file():
        pre.parent.mkdir(parents=True, exist_ok=True)
        r = RunResult.model_validate_json(path.read_text())
        if r.code.code_version is not None:
            raise RuntimeError(f"{path.name} already has code checks but no pre_code copy")
        shutil.copy2(path, pre)
    return RunResult.model_validate_json(pre.read_text())


@app.callback()
def main(ctx: typer.Context, data_root: DataRoot = None, results: Results = None, rerun_ids: RerunIds = None,
         rerun_all: RerunAll = False,
         static_only: Annotated[bool, typer.Option("--static-only", help="No re-runs")] = False,
         ids: Annotated[str | None, typer.Option("--ids", help="Comma list: only these papers")] = None,
         max_eur: Annotated[float, typer.Option("--max-eur", help="Stop starting papers past this spend")] = 6.0,
         max_minutes: Annotated[float | None, typer.Option("--max-minutes", help="Per-paper re-run wall clock "
                                                           "(default ONUS_RERUN_MAX_MINUTES)")] = None,
         force: Annotated[bool, typer.Option("--force", help="Redo stored re-runs")] = False) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if data_root is None or results is None:
        console.print("[red]--data-root and --results are required (or use a subcommand: plan, version)[/red]")
        raise typer.Exit(code=2)
    asyncio.run(_run(data_root, results, rerun_ids=_ids_file(rerun_ids), rerun_all=rerun_all,
                     static_only=static_only, only=set(ids.split(",")) if ids else None, max_eur=max_eur,
                     max_minutes=max_minutes, force=force))


async def _run(data_root: Path, results: Path, *, rerun_ids: set[str], rerun_all: bool, static_only: bool,
               only: set[str] | None, max_eur: float, max_minutes: float | None, force: bool) -> None:
    from onus.codecheck.apply import apply_code
    from onus.codecheck.common import code_llm, rubric
    from onus.codecheck.rerun.run import rerun_paper
    from onus.codecheck.static import static_check
    from onus.codecheck.version import code_fingerprint, code_version_label
    from onus.config import get_settings
    from onus.llm.costs import BudgetExceeded
    from onus.pipeline import load_or_ingest
    from onus.version import fingerprint as p0_fingerprint

    settings = get_settings()
    llm = code_llm()
    label = code_version_label()
    store = results / "code"
    store.mkdir(parents=True, exist_ok=True)
    scfg = rubric("static.yaml")
    min_shared, claim_types = int(scfg["min_shared_terms"]), tuple(scfg["claim_types"])
    console.print(f"code-v1 {label} (pipeline {p0_fingerprint()}); results {results}")
    spent, rows = 0.0, []
    for path in _result_files(results):
        pid = path.stem
        if only and pid not in only:
            continue
        paper_dir = data_root / "papers" / pid
        if not (paper_dir / "paper.pdf").is_file():
            console.print(f"  {pid}: no paper in {data_root}; skipped")
            continue
        if spent >= max_eur:
            console.print(f"[yellow]  {pid}: skipped, spend €{spent:.2f} reached --max-eur {max_eur}[/yellow]")
            continue
        p0 = _load_p0(results, path)
        eur0 = llm.ledger.paper_eur.get(f"code:{pid}", 0.0)
        doc = load_or_ingest(paper_dir, settings.var_dir)
        try:
            st = await static_check(doc, paper_dir, llm)
        except BudgetExceeded as e:
            console.print(f"[red]  {pid}: budget: {e}[/red]")
            break
        st_eur = sum(m.eur for m in st.metas)
        (store / f"{pid}.static.json").write_text(json.dumps(
            {"status": st.status, "reason": st.reason, "facts": [f.as_dict() for f in st.facts],
             "paper_level": st.paper_level, "dropped": st.dropped, "warnings": st.warnings, "eur": st_eur,
             "code_version": label}, indent=1))
        reruns, cmps = [], []
        want = not static_only and doc.code.present and (rerun_all or pid in rerun_ids)
        rr_path = store / f"{pid}.rerun.json"
        if want and rr_path.is_file() and not force:
            stored = json.loads(rr_path.read_text())
            if stored.get("code_fingerprint") == code_fingerprint():
                reruns, cmps = [stored["report"]], stored["comparisons"]
                console.print(f"  {pid}: re-run reused from {rr_path.name}")
                want = False
        if want:
            try:
                rep, cmps = await rerun_paper(doc, p0, paper_dir, llm, settings=settings, max_minutes=max_minutes)
            except BudgetExceeded as e:
                console.print(f"[red]  {pid}: budget: {e}[/red]")
                break
            reruns = [rep]
            rr_path.write_text(json.dumps({"code_fingerprint": code_fingerprint(), "report": rep,
                                           "comparisons": cmps}, indent=1))
        paper_eur = llm.ledger.paper_eur.get(f"code:{pid}", 0.0) - eur0
        spent += paper_eur
        res = apply_code(p0, doc, st, reruns, cmps, code_version=label, min_shared=min_shared,
                         claim_types=claim_types)
        path.write_text(res.model_dump_json(indent=1))
        pl = st.paper_level
        rr = res.code.reruns[0] if res.code.reruns else {}
        rows.append((pid, st.status if st.status == "done" else f"not run: {st.reason}",
                     "" if not pl else ("mismatch" if pl.get("mismatch") else "match" if pl.get("mismatch") is False
                                        else "n/a"),
                     f"{pl.get('present', 0)}/{pl.get('absent', 0)}/{pl.get('contradicts', 0)}" if pl else "",
                     rr.get("status", "-"), ",".join(c["outcome"] for c in cmps) or "-",
                     f"{rr.get('minutes', 0):.1f}" if rr else "-", f"€{paper_eur:.3f}",
                     str(res.summary.get("code", {}).get("verdicts_changed", 0))))
        console.print(f"  {pid}: static {rows[-1][1]} {rows[-1][2]}; re-run {rows[-1][4]} ({rows[-1][5]}); "
                      f"{rows[-1][7]}")
    t = Table(title=f"code checks ({label})")
    for h in ("paper", "static", "paper level", "core p/a/c", "re-run", "code_rerun", "minutes", "€", "verdicts Δ"):
        t.add_column(h)
    for r in rows:
        t.add_row(*r)
    console.print(t)
    console.print(f"spend €{spent:.3f}")


@app.command("plan")
def plan_cmd(data_root: DataRoot = None, results: Results = None, rerun_ids: RerunIds = None,
             rerun_all: RerunAll = False) -> None:
    """Dry run: papers with code, static facts budget, re-run targets and the cost estimate. No model calls."""
    from onus.codecheck.common import code_root, rubric
    from onus.codecheck.rerun.agent import select_targets
    from onus.codecheck.rerun.sandbox import plan_rerun
    from onus.config import get_settings
    from onus.pipeline import load_or_ingest

    if data_root is None or results is None:
        console.print("[red]--data-root and --results are required[/red]")
        raise typer.Exit(code=2)
    s = get_settings()
    ids = _ids_file(rerun_ids)
    cfg = rubric("rerun.yaml")
    n_static = n_rerun = 0
    t = Table(title="code-check plan")
    for h in ("paper", "code files", "re-run", "targets", "python", "deps", "providers", "external"):
        t.add_column(h)
    for path in _result_files(results):
        pid = path.stem
        paper_dir = data_root / "papers" / pid
        if not (paper_dir / "paper.pdf").is_file():
            continue
        doc = load_or_ingest(paper_dir, s.var_dir)
        if not doc.code.present:
            t.add_row(pid, "0", "-", "-", "-", "-", "-", "-")
            continue
        n_static += 1
        p0 = _load_p0(results, path)
        tg = select_targets(p0, int(cfg["max_targets"]))
        root = code_root(doc, paper_dir)
        pl = plan_rerun(root, doc.code.entry_candidates, s, str(cfg["python_default"])) if root else None
        rr = rerun_all or pid in ids
        n_rerun += rr
        t.add_row(pid, str(len(doc.code.files)), "yes" if rr else "no", str(len(tg)), pl.python if pl else "-",
                  (",".join(pl.requirements + pl.projects) or "imports: " + ",".join(pl.extra_pip[:5]))[:50] if pl
                  else "-", ",".join(pl.providers) if pl else "-", ",".join(pl.external)[:30] if pl else "-")
    console.print(t)
    est_s, est_r = 0.07 * n_static, float(cfg["max_agent_eur"]) * n_rerun
    console.print(f"{n_static} static (≈ €{est_s:.2f}), {n_rerun} re-runs (≤ €{est_r:.2f} agent spend, ≤ "
                  f"{s.onus_rerun_max_minutes} Modal min each): ≤ €{est_s + est_r:.2f}")


@app.command("version")
def version_cmd() -> None:
    """Print the code-v1 version and fingerprint, and whether they match frozen.json."""
    from onus.codecheck.version import CODE_VERSION, code_fingerprint
    from onus.version import load_frozen

    fp = code_fingerprint()
    console.print(f"code-v1 {CODE_VERSION}  fingerprint {fp}")
    frozen = (load_frozen() or {}).get("code")
    if frozen is None:
        console.print("code-v1 not frozen yet (onus freeze --code-version X.Y.Z)")
    else:
        ok = frozen.get("fingerprint") == fp and frozen.get("version") == CODE_VERSION
        console.print(f"frozen code-v1 {frozen.get('version')} ({frozen.get('fingerprint')}): "
                      f"{'matches' if ok else '[red]DRIFTED[/red]'}")


def freeze_code(code_version: str) -> None:
    """`onus freeze --code-version X.Y.Z`: pin the code-v1 fingerprint under "code" in frozen.json."""
    from onus.codecheck.version import freeze_code_version

    try:
        rec = freeze_code_version(code_version)
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from None
    console.print(f"frozen code-v1 {rec['version']}  fingerprint {rec['fingerprint']}")
