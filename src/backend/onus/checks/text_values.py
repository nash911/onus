"""Text values (v1.1): the claim's quantities against a passage the selection model says contradicts it.

The model points, code computes. A text passage that evidence selection marked `contradicts` is a pointer, not a
result, so on its own it never flags a claim (verdict/rules.py). This module compares the quantities in the claim
with those in such passages: a number followed by a unit (%, hours, GPUs, cores, GB, epochs, samples, ...). For a
unit that both the claim and the passage state, the claim's value must appear in the passage (rounded to the
claim's precision). If the passage states that unit only with other values, code has found a mismatch.

Used by the number check (`numbers.check_numbers`) only where the table comparison did not run (no number for the
tables, no linked table, or numbers not from the linked tables). A value that appears in any linked passage that
supports, partly supports or contradicts the claim clears the unit, so a passage about another quantity with the
same unit is not enough on its own when the claim's value is printed elsewhere in the evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from onus.schemas.result import Evidence

_UNITS: dict[str, str] = {
    "%": "%", "percent": "%",
    "hour": "hours", "hours": "hours", "hr": "hours", "hrs": "hours", "h": "hours",
    "minute": "minutes", "minutes": "minutes", "min": "minutes", "mins": "minutes",
    "second": "seconds", "seconds": "seconds", "sec": "seconds", "secs": "seconds",
    "day": "days", "days": "days", "week": "weeks", "weeks": "weeks",
    "gpu": "GPUs", "gpus": "GPUs", "cpu": "CPUs", "cpus": "CPUs", "core": "cores", "cores": "cores",
    "gb": "GB", "mb": "MB", "tb": "TB",
    "epoch": "epochs", "epochs": "epochs", "sample": "samples", "samples": "samples",
    "participant": "participants", "participants": "participants", "subject": "subjects", "subjects": "subjects",
    "run": "runs", "runs": "runs", "seed": "seeds", "seeds": "seeds", "trial": "trials", "trials": "trials",
    "layer": "layers", "layers": "layers", "step": "steps", "steps": "steps",
}
_QTY = re.compile(
    r"(?<![\w.,])(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?(?:-\s?)?"
    r"(?:CPU\s+|GPU\s+)?(?P<unit>%|percent\b|hours?\b|hrs?\b|h\b|minutes?\b|mins?\b|min\b|seconds?\b|secs?\b|"
    r"days?\b|weeks?\b|GPUs?\b|CPUs?\b|cores?\b|GB\b|MB\b|TB\b|epochs?\b|samples?\b|participants?\b|subjects?\b|"
    r"runs?\b|seeds?\b|trials?\b|layers?\b|steps?\b)",
    re.IGNORECASE,
)
# "Table 3 %", "Section 4 hours" never occur, but references and years should not be read as quantities
_REF_BEFORE = re.compile(r"(?:table|fig\.?|figure|section|sec\.|eq\.|equation|appendix|line|page|step)\s*$",
                         re.IGNORECASE)


@dataclass(frozen=True)
class Quantity:
    value: float
    decimals: int
    unit: str
    text: str


def quantities(text: str) -> list[Quantity]:
    out: list[Quantity] = []
    for m in _QTY.finditer(text):
        if _REF_BEFORE.search(text[max(0, m.start() - 12) : m.start()]):
            continue
        raw = m.group("num").replace(",", "")
        unit = _UNITS.get(m.group("unit").lower())
        if unit is None:
            continue
        dec = len(raw.split(".")[1]) if "." in raw else 0
        out.append(Quantity(float(raw), dec, unit, m.group(0).strip()))
    return out


def _same(claimed: Quantity, x: float) -> bool:
    return abs(claimed.value - x) <= 0.5 * 10 ** (-claimed.decimals) + 1e-9


@dataclass
class TextMismatch:
    claimed: Quantity
    found: list[Quantity]
    evidence: Evidence


def compare_text_values(claim: str, linked: list[tuple[Evidence, str]]) -> list[TextMismatch]:
    """Code-found mismatches between the claim's quantities and its contradicting text passages."""
    cq = quantities(claim)
    if not cq:
        return []
    passages = [(e, t) for e, t in linked if e.kind == "text" and e.relation in ("supports", "partial", "contradicts")]
    seen = {(q.unit, v.value) for _, t in passages for v in quantities(t) for q in cq if v.unit == q.unit}
    out: list[TextMismatch] = []
    for q in cq:
        if any(u == q.unit and _same(q, x) for u, x in seen):
            continue  # the claim's value is printed in the evidence
        for e, t in passages:
            if e.relation != "contradicts":
                continue
            other = [v for v in quantities(t) if v.unit == q.unit]
            if other:
                out.append(TextMismatch(q, other, e))
                break
    return out
