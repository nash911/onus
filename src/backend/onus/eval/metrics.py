"""Recall, collateral flags, stability, code agreement and distributions. (Phase 7)

Everything here is computed from RunResults, mutation records, baseline results and split labels. The
aggregate table never names a paper: every cell is a count or a rate, and a cell resting on fewer than two
papers is shown as "n<2".
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from onus.claims.quotes import match_key
from onus.schemas.result import RunResult

from .baseline import BaselineResult
from .mutate import TYPES, Mutation, match_claims
from .truth import SplitLabels

FLAGGED = {"contradicted", "overstated", "untraced"}
VERDICTS = ("contradicted", "overstated", "untraced", "not_checkable", "supported")
AUDITS = ("backed", "unbacked", "contradicted", "not_checkable", "not_applicable")
STATIC_MISMATCH = {"mismatch", "contradicted"}  # Phase 10 statuses that make a paper-level code mismatch
RERUN_MISMATCH = {"inconsistent"}
MATCH_MIN = 85.0


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def rate(k: int, n: int, papers: int | None = None) -> dict:
    return {"k": k, "n": n, "rate": (k / n) if n else None, "ci95": wilson(k, n),
            "papers": papers if papers is not None else n}


def cohen_kappa(pairs: list[tuple[int, int]]) -> float | None:
    n = len(pairs)
    if n == 0:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    pa = sum(a for a, _ in pairs) / n
    pb = sum(b for _, b in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe >= 1 else (po - pe) / (1 - pe)


# ---------------------------------------------------------------- mutation detection


@dataclass
class Detection:
    mutation: Mutation
    onus: bool
    baseline: bool | None  # None when the baseline did not run
    target_found: bool
    target_ids: list[str] = field(default_factory=list)  # claim ids in the mutated result
    collateral: int = 0
    collateral_n: int = 0
    baseline_collateral: int = 0
    baseline_collateral_n: int = 0
    note: str = ""


def _targets(mut: Mutation, mres: RunResult) -> list[list]:
    """Per target: the mutated result's claims at it."""
    if mut.type in ("checklist_flip", "delete_errorbars"):
        return [[c for c in mres.claims if c.type == "checklist" and c.checklist_key == mut.checklist_key]]
    if mut.type == "abstract_number":
        return [match_claims(mres, mut.span)]
    return [match_claims(mres, tuple(t["span"]), t["quote"]) for t in mut.target_claims]  # type: ignore[arg-type]


def detect(mut: Mutation, mres: RunResult, orig: RunResult, mbase: BaselineResult | None = None,
           obase: BaselineResult | None = None) -> Detection:
    groups = _targets(mut, mres)
    found = [g for g in groups if g]
    note = ""
    if mut.type in ("checklist_flip", "delete_errorbars"):
        audit = next((a for a in mres.checklist if a.key == mut.checklist_key), None)
        onus = audit is not None and audit.status in ("unbacked", "contradicted")
        target_found = audit is not None
        note = audit.status if audit else "item not audited"
    elif mut.type == "abstract_number":
        onus = any(c.verdict == "contradicted" and any(k.check == "number" and k.outcome == "fail" for k in c.checks)
                   for g in found for c in g)
        target_found = bool(found)
    elif mut.type == "remove_table":
        onus = bool(found) and all(any(c.verdict in ("untraced", "not_checkable") for c in g) for g in found)
        target_found = bool(found)
    else:  # widen_scope
        onus = any(c.verdict in ("overstated", "contradicted") for g in found for c in g)
        target_found = bool(found)
    tids = [c.id for g in found for c in g]
    base: bool | None = None
    if mbase is not None:
        labs = [[mbase.label_of(c.id) for c in g] for g in found]
        if mut.type == "remove_table":
            base = bool(labs) and all(any(x == "not_supported" for x in g) for g in labs)
        else:
            base = any(x == "not_supported" for g in labs for x in g)
    d = Detection(mutation=mut, onus=onus, baseline=base, target_found=target_found, target_ids=tids, note=note)
    _collateral(d, mres, orig, mbase, obase)
    return d


def _match(c, pool: list):
    if c.type == "checklist":
        return next((o for o in pool if o.type == "checklist" and o.checklist_key == c.checklist_key), None)
    k = match_key(c.quote)
    best, score = None, MATCH_MIN
    for o in pool:
        if o.type == "checklist":
            continue
        s = 100.0 if match_key(o.quote) == k else fuzz.ratio(match_key(o.quote), k)
        if s >= score:
            best, score = o, s
    return best


def _collateral(d: Detection, mres: RunResult, orig: RunResult, mbase: BaselineResult | None,
                obase: BaselineResult | None) -> None:
    """Flags in the mutated run that are not at the target and not flagged (or absent) in the original run."""
    tids = set(d.target_ids)
    for c in mres.claims:
        if c.id in tids:
            continue
        d.collateral_n += 1
        o = _match(c, orig.claims)
        if c.verdict in FLAGGED and (o is None or o.verdict not in FLAGGED):
            d.collateral += 1
        if mbase is not None and obase is not None:
            d.baseline_collateral_n += 1
            if mbase.label_of(c.id) == "not_supported" and (o is None or obase.label_of(o.id) != "not_supported"):
                d.baseline_collateral += 1


def recall_table(dets: list[Detection], attempted: Counter) -> dict:
    out: dict = {}
    for t in TYPES:
        ds = [d for d in dets if d.mutation.type == t]
        papers = len({d.mutation.paper_id for d in ds})
        bs = [d for d in ds if d.baseline is not None]
        out[t] = {"attempted": attempted.get(t, 0), "applied": len(ds), "papers": papers,
                  "target_missing": sum(1 for d in ds if not d.target_found),
                  "onus": rate(sum(d.onus for d in ds), len(ds), papers),
                  "baseline": rate(sum(bool(d.baseline) for d in bs), len(bs),
                                   len({d.mutation.paper_id for d in bs})) if bs else None}
    bs = [d for d in dets if d.baseline is not None]
    out["all"] = {"attempted": sum(attempted.values()), "applied": len(dets),
                  "papers": len({d.mutation.paper_id for d in dets}),
                  "target_missing": sum(1 for d in dets if not d.target_found),
                  "onus": rate(sum(d.onus for d in dets), len(dets), len({d.mutation.paper_id for d in dets})),
                  "baseline": rate(sum(bool(d.baseline) for d in bs), len(bs),
                                   len({d.mutation.paper_id for d in bs})) if bs else None}
    return out


def collateral(dets: list[Detection]) -> dict:
    papers = len({d.mutation.paper_id for d in dets})
    out = {"onus": rate(sum(d.collateral for d in dets), sum(d.collateral_n for d in dets), papers)}
    bd = [d for d in dets if d.baseline_collateral_n]
    out["baseline"] = rate(sum(d.baseline_collateral for d in bd), sum(d.baseline_collateral_n for d in bd),
                           len({d.mutation.paper_id for d in bd})) if bd else None
    return out


# ---------------------------------------------------------------- results-level metrics


def stability(results: list[RunResult]) -> dict:
    multi = [r for r in results if r.runs > 1]
    if not multi:
        return {"runs": 1, "na": "n/a (1 run)"}
    claims = [c for r in multi for c in r.claims if c.stability is not None]
    full = sum(1 for c in claims if c.stability.agree == c.stability.runs)  # type: ignore[union-attr]
    matrix: dict[str, Counter] = defaultdict(Counter)
    for c in claims:
        for v in c.stability.verdicts:  # type: ignore[union-attr]
            matrix[c.verdict][v] += 1
    return {"runs": max(r.runs for r in multi), "claims": len(claims), "papers": len(multi),
            "full_agreement": rate(full, len(claims), len(multi)),
            "mean_agreement": (sum(c.stability.agree / c.stability.runs for c in claims) / len(claims))  # type: ignore[union-attr]
            if claims else None,
            "matrix": {k: dict(v) for k, v in matrix.items()}}


def code_mismatch(r: RunResult) -> int | None:
    """Onus's paper-level code verdict: 1 mismatch, 0 consistent, None when no code check ran (before Phase 10)."""
    if not r.code.static and not r.code.reruns:
        return None
    bad = any(str(f.get("status", "")).lower() in STATIC_MISMATCH for f in r.code.static) or \
        any(str(x.get("status", "")).lower() in RERUN_MISMATCH for x in r.code.reruns)
    return int(bad)


def code_agreement(results: list[RunResult], labels: SplitLabels) -> dict:
    labelled = {pid: labels.audit_class(pid) for pid in (r.paper["id"] for r in results)}
    usable = {pid: y for pid, y in labelled.items() if y is not None}
    out = {"labelled": len(usable), "excluded": sum(1 for y in labelled.values() if y is None),
           "audit_classes": dict(Counter(labels.audits.get(pid, "") or "missing" for pid in labelled))}
    pairs = []
    for r in results:
        y = usable.get(r.paper["id"])
        p = code_mismatch(r)
        if y is not None and p is not None:
            pairs.append((p, y))
    if not pairs:
        out["na"] = "n/a (no code-check results yet)"
        return out
    cm = Counter(pairs)
    out.update({"n": len(pairs), "accuracy": sum(1 for a, b in pairs if a == b) / len(pairs),
                "kappa": cohen_kappa(pairs),
                "confusion": {"pred_mismatch_audit_mismatch": cm[(1, 1)], "pred_mismatch_audit_passed": cm[(1, 0)],
                              "pred_ok_audit_mismatch": cm[(0, 1)], "pred_ok_audit_passed": cm[(0, 0)]}})
    return out


def distributions(results: list[RunResult], baselines: dict[str, BaselineResult] | None = None) -> dict:
    v = Counter(c.verdict for r in results for c in r.claims)
    reg = [c for r in results for c in r.claims if c.type != "checklist"]
    mains = [c for c in reg if c.is_main]
    a = Counter(x.status for r in results for x in r.checklist)
    out = {"papers": len(results), "claims": sum(v.values()), "non_checklist_claims": len(reg),
           "verdicts": {k: v.get(k, 0) for k in VERDICTS},
           "flagged": rate(sum(v.get(k, 0) for k in FLAGGED), sum(v.values()), len(results)),
           "main_flagged": rate(sum(1 for c in mains if c.verdict in FLAGGED), len(mains), len(results)),
           "checklist": {k: a.get(k, 0) for k in AUDITS}}
    if baselines:
        bs = [b for r in results if (b := baselines.get(r.paper["id"])) is not None]
        labs = Counter(c.label for b in bs for c in b.claims)
        out["baseline"] = {"papers": len(bs), "labels": dict(labs),
                           "not_supported": rate(labs.get("not_supported", 0), sum(labs.values()), len(bs))}
    return out


# ---------------------------------------------------------------- the printed table


def _cell(r: dict | None, pct: bool = True) -> str:
    if r is None:
        return "—"
    if r.get("papers", 0) < 2 and r.get("n", 0) > 0:
        return "n<2"
    if not r.get("n"):
        return "—"
    ci = r.get("ci95")
    s = f"{r['k']}/{r['n']} = {r['rate']:.0%}" if pct else f"{r['rate']:.2f}"
    return s + (f" [{ci[0]:.0%}–{ci[1]:.0%}]" if ci else "")


def _n(x: int, papers: int) -> str:
    return "n<2" if 0 < papers < 2 else str(x)


def render_aggregate(m: dict) -> str:
    """Markdown table of aggregates only: no paper ids, titles, quotes or per-paper rows."""
    d = m["distributions"]
    rows: list[tuple[str, str]] = [
        ("Version fingerprint", f"`{m['fingerprint']}`"),
        ("Papers (results / labelled)", f"{d['papers']} / {m['labels']['papers_labelled']}"),
        ("Runs per paper", ", ".join(str(x) for x in m["runs"])),
        ("Claims (all / non-checklist)", f"{d['claims']} / {d['non_checklist_claims']}"),
    ]
    p = d["papers"]
    rows += [(f"Verdict: {k}", _n(n, p) + (f" ({n / d['claims']:.0%})" if d["claims"] and p >= 2 else ""))
             for k, n in d["verdicts"].items()]
    rows += [("Flagged share (claims)", _cell(d["flagged"])), ("Flagged share (main claims)", _cell(d["main_flagged"]))]
    rows += [("Checklist audit " + ", ".join(AUDITS), " / ".join(_n(d["checklist"][k], p) for k in AUDITS))]
    st = m["stability"]
    rows.append(("Stability (claims with full agreement)", st.get("na") or _cell(st["full_agreement"])))
    if st.get("matrix"):
        diag = sum(st["matrix"].get(k, {}).get(k, 0) for k in VERDICTS)
        tot = sum(sum(v.values()) for v in st["matrix"].values())
        rows.append(("Stability (run verdicts = final verdict)", f"{diag}/{tot}" if p >= 2 else "n<2"))
    mu = m.get("mutations")
    if mu:
        rows.append(("Mutations: seed / plan", f"{m['seed']} / {m['mutation_plan']}"))
        for t in (*TYPES, "all"):
            x = mu["recall"][t]
            if not x["attempted"]:
                continue
            label = "all types" if t == "all" else t
            rows.append((f"Recall {label}: Onus", _cell(x["onus"])))
            if x.get("baseline") is not None:
                rows.append((f"Recall {label}: baseline", _cell(x["baseline"])))
            rows.append((f"  {label}: attempted / applied / target not extracted",
                         f"{x['attempted']} / {_n(x['applied'], x['papers'])} / {_n(x['target_missing'], x['papers'])}"))
        rows.append(("Collateral flag rate: Onus", _cell(mu["collateral"]["onus"])))
        if mu["collateral"].get("baseline"):
            rows.append(("Collateral flag rate: baseline", _cell(mu["collateral"]["baseline"])))
        if mu.get("failed"):
            rows.append(("Mutation runs failed", str(mu["failed"])))
    if d.get("baseline"):
        rows.append(("Baseline not_supported share (original claims)", _cell(d["baseline"]["not_supported"])))
    ca = m["code_agreement"]
    if ca.get("na"):
        rows.append(("Code agreement (vs audit)", f"{ca['na']}; {ca['labelled']} labelled, {ca['excluded']} excluded"))
    elif ca["n"] < 2:
        rows.append(("Code agreement (vs audit)", "n<2"))
    else:
        k = ca["kappa"]
        rows.append(("Code agreement (vs audit)", f"accuracy {ca['accuracy']:.0%}, κ {k:.2f} (n={ca['n']})" if k is not None
                     else f"accuracy {ca['accuracy']:.0%} (n={ca['n']})"))
        c = ca["confusion"]
        rows.append(("  confusion mm/mp/om/op", (f"{c['pred_mismatch_audit_mismatch']}/{c['pred_mismatch_audit_passed']}/"
                     f"{c['pred_ok_audit_mismatch']}/{c['pred_ok_audit_passed']}")))
    cost = m.get("cost", {})
    rows.append(("Eval spend / wall time", f"€{cost.get('eur', 0):.2f} / {cost.get('minutes', 0):.0f} min"))
    w = max(len(a) for a, _ in rows)
    out = [f"| {'Metric'.ljust(w)} | Value |", f"| {'-' * w} | --- |"]
    out += [f"| {a.ljust(w)} | {b} |" for a, b in rows]
    return "\n".join(out)


def merge_metrics(metrics: dict[str, dict], label_scores: dict | None) -> dict:
    """metrics.json files of several splits -> one file for the Trust page (recall pooled over splits)."""
    pooled: dict = {}
    for t in (*TYPES, "all"):
        for system in ("onus", "baseline"):
            k = n = papers = 0
            for m in metrics.values():
                x = ((m.get("mutations") or {}).get("recall") or {}).get(t, {}).get(system)
                if x:
                    k, n, papers = k + x["k"], n + x["n"], papers + x.get("papers", 0)
            pooled.setdefault(t, {})[system] = rate(k, n, papers) if n else None
    col = {"k": 0, "n": 0, "papers": 0}
    for m in metrics.values():
        x = ((m.get("mutations") or {}).get("collateral") or {}).get("onus")
        if x:
            col = {"k": col["k"] + x["k"], "n": col["n"] + x["n"], "papers": col["papers"] + x.get("papers", 0)}
    return {"splits": metrics, "pooled": {"recall": pooled,
                                          "collateral": rate(col["k"], col["n"], col["papers"]) if col["n"] else None},
            "labels": label_scores}
