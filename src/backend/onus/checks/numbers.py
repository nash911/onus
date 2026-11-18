"""Number recheck (F5): the model maps cells, code recomputes. (Phase 5)

1. Code extracts the numbers in the claim: percentages, percentage points, decimals, "×" and "-fold" (bare
   integers are counts and settings, not results, and are skipped).
2. Prompt `map_number` (extract role) maps each number to `{kind, operands: [{table_id, row, col}],
   higher_is_better}` over the claim's linked tables, or says it is not from those tables.
3. Code resolves the cells (ids or labels, rapidfuzz >= 85; ambiguity -> inconclusive), computes the value and
   compares it with the tolerance in `rubrics/tolerances.yaml` (Appendix B.3). Fractional cells may back a
   percentage (0.812 -> 81.2%) and the reverse.
4. Relative changes are also tested as percentage points, and differences as relative changes. A match only
   under the other reading passes with a caveat and severity "overstates" (the outcome stays "pass"; the
   verdict rules treat a non-"none" severity as a flag).
5. A mismatch is "fail" / "contradicts", unless the claimed number appears verbatim in another cell of the
   linked tables: then the mapping is suspect and the number is inconclusive.
6. If the claim has no linked table, or the model says none of its numbers come from the linked tables, the
   mapping is retried once over the paper's other parsed tables (`ClaimContext.fallback_tables`, ranked by word
   overlap with numbers ignored). A result found this way is recorded with `data["fallback_tables"]`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from onus.llm.client import CallMeta, LLMError
from onus.schemas.document import Cell
from onus.schemas.result import CheckResult

from .context import ClaimContext
from .models import not_run
from .rubrics import Tolerances, default_tolerances
from .tables import TableView, find_table
from .tables import fmt as _f

KINDS = ("value", "abs_diff", "rel_change", "ratio", "mean", "max", "min", "not_in_tables")
MAX_NUMBERS = 8

_NUMBER = re.compile(
    r"(?<![\w.\-/])(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+|\.\d+)(?![\w.]*\d)"
    r"(?:\s*(?P<unit>%|percent(?:age)?[\s-]+points?|percent|pp\b|p\.p\.|-?points?\b|×|x\b|-?fold\b|times\b))?",
    re.IGNORECASE,
)
_SKIP_BEFORE = re.compile(
    r"(?:^|\W)(?:table|tab\.|figure|fig\.|section|sec\.|§|eq\.|equation|appendix|app\.|line|lines|step|version|v|"
    r"p\s*[<=>≤≥]|n\s*=|k\s*=|top-|@)\s*\(?$",
    re.IGNORECASE,
)


@dataclass
class Mention:
    id: str  # "N1"
    text: str
    value: float
    decimals: int
    unit: str | None  # "percent" | "pp" | "fold" | None
    start: int
    end: int

    @property
    def percentish(self) -> bool:
        return self.unit in ("percent", "pp")


def _unit(u: str | None) -> str | None:
    if not u:
        return None
    u = u.lower().strip("-")
    if u in ("%", "percent"):
        return "percent"
    if "point" in u or u in ("pp", "p.p."):
        return "pp"
    return "fold"


# "96.81 ± 0.42%", "0.81±0.02", "12.3 +/- 1.1 pp": the spread after ± is not a claimed value of its own, and a
# unit written after the spread belongs to the mean as well
_SPREAD_AFTER = re.compile(
    r"\s*(?:±|\+/-|\+-|\\pm)\s*(?:\d+(?:\.\d+)?|\.\d+)"
    r"(?:\s*(?P<unit>%|percent(?:age)?[\s-]+points?|percent|pp\b|p\.p\.|-?points?\b))?",
    re.IGNORECASE)


def _after_plus_minus(text: str, start: int) -> bool:
    head = text[max(0, start - 6) : start]
    return bool(re.search(r"(?:±|\+/-|\+-|\\pm)\s*$", head))


def extract_numbers(text: str) -> list[Mention]:
    out: list[Mention] = []
    for m in _NUMBER.finditer(text):
        raw, unit = m.group("num"), _unit(m.group("unit"))
        if _after_plus_minus(text, m.start()):
            continue  # a spread, not a value
        if unit is None:
            sp = _SPREAD_AFTER.match(text, m.end())
            if sp is not None and sp.group("unit"):
                unit = _unit(sp.group("unit"))
        if unit is None and "." not in raw:
            continue  # bare integers: counts, years, settings
        if _SKIP_BEFORE.search(text[max(0, m.start() - 14) : m.start()]):
            continue
        if unit is None and re.match(r"\d", text[m.end() : m.end() + 2].strip() or "x"):
            continue
        val = float(raw.replace(",", ""))
        dec = len(raw.split(".")[1]) if "." in raw else 0
        out.append(Mention(id=f"N{len(out) + 1}", text=m.group(0).strip(), value=val, decimals=dec, unit=unit,
                           start=m.start(), end=m.end()))
        if len(out) >= MAX_NUMBERS:
            break
    return out


# ---------------------------------------------------------------- model output


class Operand(BaseModel):
    table_id: str = ""
    row: str = ""
    col: str = ""


class NumberMap(BaseModel):
    number_id: str
    kind: str = "not_in_tables"
    operands: list[Operand] = Field(default_factory=list)
    higher_is_better: bool | None = None
    note: str = ""


class MapNumberOut(BaseModel):
    items: list[NumberMap] = Field(default_factory=list)


# ---------------------------------------------------------------- maths


@dataclass
class Resolved:
    value: float
    cell: Cell
    where: str
    table: TableView


def resolve_operands(views: list[TableView], ops: list[Operand]) -> tuple[list[Resolved] | None, str]:
    out: list[Resolved] = []
    for op in ops:
        v = find_table(views, op.table_id) or (views[0] if len(views) == 1 and not op.table_id.strip() else None)
        if v is None:
            return None, f"table {op.table_id!r} is not a linked table"
        r, c = v.row(op.row), v.col(op.col)
        if r.status != "ok" or c.status != "ok":
            bad = [f"row {op.row!r} {r.status}" if r.status != "ok" else "",
                   f"col {op.col!r} {c.status}" if c.status != "ok" else ""]
            return None, f"{v.id}: " + ", ".join(b for b in bad if b)
        cell = v.cell(r.index, c.index)  # type: ignore[arg-type]
        if cell is None or cell.value is None:
            return None, f"{v.where(r.index, c.index)} has no number"  # type: ignore[arg-type]
        out.append(Resolved(cell.value, cell, v.where(r.index, c.index), v))  # type: ignore[arg-type]
    return out, ""


def compute(kind: str, vals: list[float]) -> tuple[float | None, str]:
    """Value and a readable expression, or (None, why)."""
    n = len(vals)
    if kind == "value":
        return (vals[0], _f(vals[0])) if n == 1 else (None, f"value needs 1 cell, got {n}")
    if kind in ("abs_diff", "rel_change", "ratio"):
        if n != 2:
            return None, f"{kind} needs 2 cells, got {n}"
        a, b = vals
        if kind == "abs_diff":
            return a - b, f"{_f(a)} − {_f(b)} = {_f(a - b)}"
        if b == 0:
            return None, "division by zero"
        if kind == "rel_change":
            return (a - b) / b * 100, f"({_f(a)} − {_f(b)}) / {_f(b)} = {_f((a - b) / b * 100)}%"
        return a / b, f"{_f(a)} / {_f(b)} = {_f(a / b)}"
    if kind in ("mean", "max", "min"):
        if n < 2:
            return None, f"{kind} needs ≥ 2 cells, got {n}"
        x = sum(vals) / n if kind == "mean" else max(vals) if kind == "max" else min(vals)
        return x, f"{kind}({', '.join(_f(v) for v in vals)}) = {_f(x)}"
    return None, f"unknown kind {kind!r}"


def _fractional(cells: list[Resolved]) -> bool:
    return all(abs(r.value) <= 1.0 and not r.cell.percent for r in cells)


def _scales(m: Mention, cells: list[Resolved], tol: Tolerances, kind: str) -> list[float]:
    if kind in ("rel_change", "ratio") or not tol.percent_scaling:
        return [1.0]
    out = [1.0]
    if m.percentish and _fractional(cells):
        out.append(100.0)
    if not m.percentish and any(r.cell.percent for r in cells) and abs(m.value) <= 1.0:
        out.append(0.01)
    return out


def _matches(m: Mention, x: float, scales: list[float], tol: Tolerances) -> float | None:
    t = tol.tol(m.value, m.decimals)
    for s in scales:
        if abs(abs(x * s) - abs(m.value)) <= t + 1e-9:
            return s
    return None


def check_mention(m: Mention, nm: NumberMap, views: list[TableView], tol: Tolerances) -> dict:
    res: dict = {"id": m.id, "text": m.text, "claimed": m.value, "kind": nm.kind, "note": nm.note}
    if nm.kind not in KINDS:
        return res | {"status": "inconclusive", "why": f"unknown kind {nm.kind!r}"}
    if nm.kind == "not_in_tables":
        return res | {"status": "skipped"}
    cells, why = resolve_operands(views, nm.operands)
    if cells is None:
        return res | {"status": "inconclusive", "why": why}
    res["cells"] = [c.where for c in cells]
    res["tables"] = list(dict.fromkeys(c.table.id for c in cells))
    vals = [c.value for c in cells]
    x, expr = compute(nm.kind, vals)
    if x is None:
        return res | {"status": "inconclusive", "why": expr}
    res |= {"computed": x, "expr": expr}
    s = _matches(m, x, _scales(m, cells, tol, nm.kind), tol)
    if s is not None:
        return res | {"status": "pass", "scale": s}
    if nm.kind == "ratio" and x != 0 and _matches(m, 1 / x, [1.0], tol) is not None:  # "2× faster": old/new
        a, b = vals
        return res | {"status": "pass", "expr": f"{_f(b)} / {_f(a)} = {_f(1 / x)}", "note": "inverse ratio"}
    alt = {"rel_change": "abs_diff", "abs_diff": "rel_change"}.get(nm.kind)
    if alt is not None:
        y, yexpr = compute(alt, vals)
        if y is not None:
            s2 = _matches(m, y, _scales(m, cells, tol, alt), tol)
            if s2 is not None:
                cav = ("reported as % but matches percentage points" if nm.kind == "rel_change"
                       else "reported as percentage points but matches a relative change")
                return res | {"status": "caveat", "caveat": cav, "alt_expr": yexpr}
    # mapping guard: the claimed number is printed in some linked cell
    for v in views:
        for i, j, c in v.cells():
            if c.value is None:
                continue
            sc = [1.0, 100.0] if (m.percentish and abs(c.value) <= 1 and not c.percent) else [1.0]
            if _matches(m, c.value, sc, tol) is not None and v.where(i, j) not in res["cells"]:
                return res | {"status": "inconclusive",
                              "why": f"recomputed {expr}, but {m.text} appears in {v.where(i, j)}: mapping suspect"}
    return res | {"status": "fail", "parse_flags": parse_flags(m, cells)}


_NUM_TOKEN = re.compile(r"-?\d+(?:[.,]\d+)*")
_UNIT_WORDS = {"percent": ("%", "percent", "pct"), "pp": ("%", "percent", "pp", "point"), "fold": ("×", "x", "fold",
                                                                                                  "times", "ratio")}


def parse_flags(m: Mention, cells: list[Resolved]) -> list[str]:
    """Code-confirmed reasons a failed comparison may rest on a parsing problem (used by adjudication)."""
    out = []
    for c in cells:
        n = len(_NUM_TOKEN.findall(c.cell.raw))
        if n > (3 if c.cell.spread is not None else 1):
            out.append(f"{c.where}: cell {c.cell.raw!r} holds {n} numbers")
    if m.unit in _UNIT_WORDS:
        ctx = " ".join([*(c.cell.raw for c in cells), *(c.where for c in cells),
                        *(c.table.table.caption for c in cells)]).lower()
        if not any(w in ctx for w in _UNIT_WORDS[m.unit]) and not any(c.cell.percent for c in cells):
            out.append(f"the claim's unit ({m.unit}) is not shown in the cell, column or caption")
    return out


def _detail(r: dict) -> str:
    where = "; ".join(r.get("cells", []))
    if r["status"] == "fail":
        return f"Claimed {r['text']}; recomputed {r['expr']} ({where})."
    if r["status"] == "caveat":
        return f"Claimed {r['text']}: {r['caveat']} ({r['alt_expr']}; {where})."
    if r["status"] == "pass":
        return f"{r['text']} = {r['expr']} ({where})."
    return f"{r['text']}: {r.get('why', 'not resolved')}."


async def check_numbers(ctx: ClaimContext, llm, *, run_index: int, paper_id: str, metas: list[CallMeta],
                        tol: Tolerances | None = None) -> CheckResult:
    tol = tol or default_tolerances()
    mentions = extract_numbers(ctx.claim.quote)
    if not mentions:
        return not_run("number", "no number in the claim")
    if not ctx.tables and not ctx.fallback_tables:
        return not_run("number", "no linked table", numbers=[m.text for m in mentions])

    async def _map(views: list[TableView]) -> list[dict] | CheckResult:
        try:
            out, meta = await llm.complete_json(
                "map_number",
                {"claim": ctx.claim.quote, "numbers": "\n".join(f"{m.id}: {m.text}" for m in mentions),
                 "tables": "\n\n".join(v.render() for v in views)},
                MapNumberOut, run_index=run_index, paper_id=paper_id)
            metas.append(meta)
        except LLMError as e:
            return CheckResult(check="number", outcome="inconclusive", detail="Number mapping failed.",
                               data={"error": str(e)[:200], "numbers": [m.text for m in mentions]},
                               evidence_ids=[i for v in ctx.tables for i in ctx.table_ids(v)])
        by_id = {}
        for it in out.items:
            by_id.setdefault(it.number_id.strip().strip("[]").upper(), it)
        return [check_mention(m, by_id[m.id], views, tol) if m.id in by_id else
                {"id": m.id, "text": m.text, "claimed": m.value, "status": "inconclusive",
                 "why": "not mapped by the model"} for m in mentions]

    results: list[dict] | CheckResult = []
    fallback = False
    if ctx.tables:
        results = await _map(ctx.tables)
        if isinstance(results, CheckResult):
            return results
    if ctx.fallback_tables and (not results or all(r["status"] == "skipped" for r in results)):
        # the linked tables do not hold the numbers: offer the paper's other parsed tables
        alt = await _map(ctx.fallback_tables)
        if isinstance(alt, CheckResult):
            if not results:
                return alt
        elif any(r["status"] != "skipped" for r in alt):
            results, fallback = alt, True
        elif not results:
            results = alt
    if not ctx.tables and not fallback:
        return not_run("number", "no linked table", numbers=results)
    used = {t for r in results for t in r.get("tables", [])}
    ev_ids = [i for v in ctx.tables if v.id in used for i in ctx.table_ids(v)] or \
        ([] if fallback else [i for v in ctx.tables for i in ctx.table_ids(v)])
    data: dict = {"numbers": results}
    if fallback:
        data["fallback_tables"] = sorted(used)
    st = [r["status"] for r in results]
    if all(s == "skipped" for s in st):
        return not_run("number", "numbers not from the linked tables", numbers=results)
    fails = [r for r in results if r["status"] == "fail"]
    if fails:
        return CheckResult(check="number", outcome="fail", severity="contradicts",
                           detail=" ".join(_detail(r) for r in fails), data=data, evidence_ids=ev_ids)
    cav = [r for r in results if r["status"] == "caveat"]
    if cav:
        return CheckResult(check="number", outcome="pass", severity="overstates",
                           detail=" ".join(_detail(r) for r in cav), data=data | {"caveat": cav[0]["caveat"]},
                           evidence_ids=ev_ids)
    ok = [r for r in results if r["status"] == "pass"]
    if ok:
        return CheckResult(check="number", outcome="pass", detail="Recomputed: " + " ".join(_detail(r) for r in ok),
                           data=data, evidence_ids=ev_ids)
    return CheckResult(check="number", outcome="inconclusive",
                       detail=" ".join(_detail(r) for r in results if r["status"] == "inconclusive"), data=data,
                       evidence_ids=ev_ids)
