"""Compare re-run output with claimed values within tolerance (Appendix B.3). (Phase 10)

Code, not model (BUILD_PLAN §7.5): the agent's `source_line` must occur in the captured output of a command it
ran; the number is parsed from that line by code (the agent's own `value` only picks which number on the line,
when there are several); the tolerance is rubrics/tolerances.yaml, read through checks/rubrics.py.

Outcomes per target claim:
- pass: a verified value is within tolerance of the claim's number (as written, or ×100 / ÷100 for a fraction
  printed against a percentage);
- fail (severity contradicts): a verified value from a faithful run (no recorded modification that the agent
  marked unfaithful, no reduced configuration) is outside tolerance;
- inconclusive: a verified value outside tolerance from a reduced or modified run;
- not_run: no verified value, with the reason.
When the submitted code was modified during the re-run (any diff against the snapshot), every check text says so
and names the files; such a value is never faithful, so it can pass but never contradict.
"""

from __future__ import annotations

from onus.checks.numbers import extract_numbers
from onus.checks.rubrics import Tolerances, default_tolerances


def _norm(s: str) -> str:
    return " ".join(s.split())


def line_in_outputs(line: str, outputs: list[str]) -> str | None:
    want = _norm(line)
    if len(want) < 3:
        return None
    for o in outputs:
        for ln in o.splitlines():
            got = _norm(ln)
            if got and (want == got or (len(want) >= 6 and want in got)):
                return ln.strip()
    return None


def _decimals(x: float) -> int:
    s = repr(float(x))
    return len(s.split(".")[1].rstrip("0")) if "." in s and "e" not in s else 0


def _mod_note(modified: list[str]) -> str:
    if not modified:
        return ""
    files = ", ".join(modified[:4]) + (f" and {len(modified) - 4} more" if len(modified) > 4 else "")
    return f" The submitted code was modified for this re-run ({files})."


def compare_one(target: dict, extracted: list[dict], outputs: list[str], tol: Tolerances, why_none: str,
                modified: list[str] | None = None) -> dict:
    claimed = float(target["value"])
    ms = extract_numbers(target.get("text") or "")
    d = ms[0].decimals if ms else _decimals(claimed)
    t = tol.tol(claimed, d)
    base = {"claim_id": target["claim_id"], "claimed": claimed, "claimed_text": target.get("text")}
    note = _mod_note(modified or [])
    if modified:
        base["modified"] = list(modified)
    mine = [x for x in extracted if x["claim_id"] == target["claim_id"]]
    if not mine:
        return base | {"outcome": "not_run", "reason": why_none,
                       "detail": f"Re-run gave no value: {why_none}.{note}"}
    notes = []
    for x in mine:
        line = line_in_outputs(x.get("source_line") or "", outputs)
        if line is None:
            notes.append("cited output line not found in the captured output")
            continue
        nums = [m.value for m in extract_numbers(line)]
        if not nums:
            notes.append("no number on the cited output line")
            continue
        got = min(nums, key=lambda v: abs(v - float(x.get("value") or 0)))
        scales = [1.0] + ([100.0, 0.01] if tol.percent_scaling else [])
        match = next((s for s in scales if abs(got * s - claimed) <= t + 1e-9), None)
        rec = base | {"value": got, "source_line": line,
                      "faithful": bool(x.get("faithful", True)) and not modified, "tolerance": round(t, 6)}
        if match is not None:
            return rec | {"outcome": "pass", "scale": match,
                          "detail": f"Re-run printed {got:g} (claimed {target.get('text') or claimed:}); within "
                                    f"tolerance.{note}"}
        if rec["faithful"]:
            return rec | {"outcome": "fail", "detail": f"Re-running the submitted code printed {got:g}, but the "
                                                      f"paper claims {target.get('text') or claimed}."}
        kind = "modified" if modified else "reduced-configuration"
        return rec | {"outcome": "inconclusive", "detail": f"A {kind} re-run printed {got:g} (claimed "
                                                          f"{target.get('text') or claimed}); not comparable.{note}"}
    reason = "; ".join(dict.fromkeys(notes))
    return base | {"outcome": "not_run", "reason": reason, "detail": f"Re-run value not verified: {reason}.{note}"}


def compare(targets: list[dict], extracted: list[dict], outputs: list[str], *, why_none: str,
            tol: Tolerances | None = None, modified: list[str] | None = None) -> list[dict]:
    tol = tol or default_tolerances()
    return [compare_one(t, extracted, outputs, tol, why_none, modified) for t in targets]
