"""Dominance check for comparison claims (F5). (Phase 5)

Runs on comparison claims (claim type "comparison", or a comparison marker from `rubrics/strength_rules.yaml` on a
result, generality or causal claim) that have a linked table. Prompt `map_comparison` (extract role) names `{table_id, systems_in, ours,
baselines[], metrics[], higher_is_better[]}`; code resolves every label and checks that "ours" strictly beats
every listed baseline in every listed metric. A tie is not a win. Failing cells are listed.

Severity on failure: "contradicts" when the claim's wording is absolute or its scope universal and "ours" loses at
least one cell outright; else "overstates" (including failures that are only ties).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from onus.llm.client import CallMeta, LLMError
from onus.schemas.result import CheckResult

from .context import ClaimContext
from .models import not_run
from .rubrics import StrengthRules, default_strength_rules
from .tables import TableView, find_table, fmt

MAX_LISTED = 8


class ComparisonMap(BaseModel):
    applicable: bool = True
    table_id: str = ""
    systems_in: Literal["rows", "columns"] = "rows"
    ours: str = ""
    baselines: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    higher_is_better: list[bool] = Field(default_factory=list)
    note: str = ""


def is_comparison(ctx: ClaimContext, rules: StrengthRules) -> list[str] | None:
    """Comparison markers in the claim, or None if it is not a comparison claim."""
    hits = rules.comparison_hits(ctx.claim.quote)
    if ctx.claim.type == "comparison" or hits:
        return hits
    return None


def evaluate(view: TableView, cm: ComparisonMap) -> dict:
    """Pure: resolve labels and compare. Returns {status, wins, failures, unresolved, compared}."""
    sys_axis, met_axis = (view.row, view.col) if cm.systems_in == "rows" else (view.col, view.row)
    out: dict = {"table": view.id, "systems_in": cm.systems_in, "wins": [], "failures": [], "unresolved": [],
                 "missing_cells": []}
    ours = sys_axis(cm.ours)
    if ours.status != "ok":
        return out | {"status": "inconclusive", "why": f"'ours' {cm.ours!r} {ours.status}"}
    out["ours"] = ours.label
    hib = list(cm.higher_is_better)
    if len(hib) == 1 and len(cm.metrics) > 1:
        hib = hib * len(cm.metrics)
    if len(hib) != len(cm.metrics):
        return out | {"status": "inconclusive", "why": "higher_is_better does not match the metrics"}
    bases = []
    for b in cm.baselines:
        r = sys_axis(b)
        if r.status != "ok":
            out["unresolved"].append(f"baseline {b!r} ({r.status})")
        elif r.index != ours.index and all(r.index != x.index for x in bases):
            bases.append(r)
    mets = []
    for m, h in zip(cm.metrics, hib, strict=True):
        r = met_axis(m)
        if r.status != "ok":
            out["unresolved"].append(f"metric {m!r} ({r.status})")
        elif all(r.index != x[0].index for x in mets):
            mets.append((r, h))
    for b in bases:
        for m, h in mets:
            oi, mi = (ours.index, m.index) if cm.systems_in == "rows" else (m.index, ours.index)
            bi, bmi = (b.index, m.index) if cm.systems_in == "rows" else (m.index, b.index)
            oc, bc = view.cell(oi, mi), view.cell(bi, bmi)  # type: ignore[arg-type]
            if oc is None or bc is None or oc.value is None or bc.value is None:
                out["missing_cells"].append(f"{b.label} / {m.label}")
                continue
            better = oc.value > bc.value if h else oc.value < bc.value
            rec = {"baseline": b.label, "metric": m.label, "ours": oc.value, "theirs": bc.value,
                   "higher_is_better": h}
            (out["wins"] if better else out["failures"]).append(rec)
    out["compared"] = len(out["wins"]) + len(out["failures"])
    if out["compared"] == 0:
        return out | {"status": "inconclusive", "why": "no comparable cells resolved"}
    return out | {"status": "fail" if out["failures"] else "pass"}


def _fail_text(view_id: str, ours: str, f: dict) -> str:
    rel = "=" if f["ours"] == f["theirs"] else ("<" if f["ours"] < f["theirs"] else ">")
    return f"'{f['baseline']}' {fmt(f['theirs'])} vs '{ours}' {fmt(f['ours'])} ({rel}) in '{f['metric']}'"


async def check_dominance(ctx: ClaimContext, llm, *, run_index: int, paper_id: str, metas: list[CallMeta],
                          rules: StrengthRules | None = None) -> CheckResult:
    rules = rules or default_strength_rules()
    markers = is_comparison(ctx, rules)
    if markers is None:
        return not_run("dominance", "not a comparison claim")
    if ctx.claim.type not in rules.claim_types:
        return not_run("dominance", f"{ctx.claim.type} claim (describes what was done)", markers=markers)
    if not ctx.tables:
        return not_run("dominance", "no linked table", markers=markers)
    try:
        cm, meta = await llm.complete_json(
            "map_comparison", {"claim": ctx.claim.quote, "tables": "\n\n".join(v.render() for v in ctx.tables)},
            ComparisonMap, run_index=run_index, paper_id=paper_id)
        metas.append(meta)
    except LLMError as e:
        return CheckResult(check="dominance", outcome="inconclusive", detail="Comparison mapping failed.",
                           data={"error": str(e)[:200], "markers": markers})
    if not cm.applicable:
        return not_run("dominance", "the linked tables do not compare the claimed systems", markers=markers,
                       note=cm.note)
    view = find_table(ctx.tables, cm.table_id) or (ctx.tables[0] if len(ctx.tables) == 1 else None)
    if view is None:
        return CheckResult(check="dominance", outcome="inconclusive",
                           detail=f"Table {cm.table_id!r} is not a linked table.", data={"map": cm.model_dump()})
    res = evaluate(view, cm)
    data = {"markers": markers, "map": cm.model_dump(), **res}
    ev = ctx.table_ids(view)
    if res["status"] == "inconclusive":
        return CheckResult(check="dominance", outcome="inconclusive", detail=f"{view.id}: {res['why']}.", data=data,
                           evidence_ids=ev)
    if res["status"] == "pass":
        return CheckResult(check="dominance", outcome="pass",
                           detail=f"{view.id}: '{res['ours']}' beats every listed baseline in all "
                                  f"{res['compared']} compared cells.", data=data, evidence_ids=ev)
    w = ctx.claim.wording
    fails = res["failures"]
    strict_loss = any(f["ours"] != f["theirs"] for f in fails)  # ties alone are "not strictly better"
    severity = ("contradicts" if strict_loss and (w.strength == "absolute" or w.scope == "universal")
                else "overstates")
    listed = "; ".join(_fail_text(view.id, res["ours"], f) for f in fails[:MAX_LISTED])
    more = f" (+{len(fails) - MAX_LISTED} more)" if len(fails) > MAX_LISTED else ""
    return CheckResult(check="dominance", outcome="fail", severity=severity,  # type: ignore[arg-type]
                       detail=f"{view.id}: not better in {len(fails)} of {res['compared']} compared cells: "
                              f"{listed}{more}.", data=data, evidence_ids=ev)
