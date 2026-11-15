"""Deterministic verdict precedence (Appendix B.5). (Phase 6)

| Order | Condition | Verdict |
| 1 | any check `fail` with severity `contradicts` | contradicted |
| 2 | no evidence with relation supports or partial, or a dangling reference inside the claim itself | untraced |
| 3 | any check with severity `overstates` (a `fail`, or the number check's pp-vs-% caveat on a `pass`) | overstated |
| 4 | evidence only `partial`, and no applicable check passed | not_checkable |
| 5 | otherwise | supported |

Checklist claims take their verdict from the checklist audit: backed -> supported, unbacked -> untraced,
contradicted -> contradicted, not_checkable -> not_checkable, not_applicable -> excluded (None).

The selection model's `relation="contradicts"` is a pointer, not a computed result, so it never flags a claim by
itself; a supported claim with such evidence gets a caveat naming it.
"""

from __future__ import annotations

from dataclasses import dataclass

from onus.schemas.result import RISK, ChecklistAudit, CheckResult, Evidence, Verdict

AUDIT_VERDICT: dict[str, Verdict | None] = {"backed": "supported", "unbacked": "untraced",
                                            "contradicted": "contradicted", "not_checkable": "not_checkable",
                                            "not_applicable": None}
REASON_CHARS = 420


@dataclass
class RuleVerdict:
    verdict: Verdict
    row: int  # 1-5 (Appendix B.5), 0 for checklist claims
    reason: str  # code-written, 1-2 sentences
    caveat: str | None = None


def _sentences(text: str, n: int = 2, limit: int = REASON_CHARS) -> str:
    text = " ".join(text.split())
    parts, out = text.split(". "), []
    for p in parts:
        out.append(p)
        if len(out) >= n:
            break
    s = ". ".join(out)
    s = s if s.endswith((".", "!", "?", "…")) else s + "."
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _names(ev: list[Evidence], k: int = 3) -> str:
    labels = list(dict.fromkeys(e.label for e in ev))
    out = ", ".join(labels[:k])
    return out + (f" and {len(labels) - k} more" if len(labels) > k else "")


def rule_verdict(evidence: list[Evidence], checks: list[CheckResult], own_dangling: list[str]) -> RuleVerdict:
    """Verdict for a non-checklist claim. `checks` includes the link check; `own_dangling` lists objects the
    claim's own sentence cites that are not in the paper."""
    contra = [c for c in checks if c.outcome == "fail" and c.severity == "contradicts"]
    if contra:
        return RuleVerdict("contradicted", 1, _sentences(contra[0].detail))
    sup = [e for e in evidence if e.relation == "supports"]
    par = [e for e in evidence if e.relation == "partial"]
    if own_dangling:
        return RuleVerdict("untraced", 2, f"The claim cites {', '.join(own_dangling)}, which is not in the paper.")
    if not sup and not par:
        link = next((c for c in checks if c.check == "link"), None)
        why = link.detail if link and link.detail else "No supporting evidence located in the paper."
        return RuleVerdict("untraced", 2, _sentences(why))
    over = [c for c in checks if c.severity == "overstates" and c.outcome in ("fail", "pass")]
    if over:
        return RuleVerdict("overstated", 3, _sentences(over[0].detail))
    applicable = [c for c in checks if c.check != "link" and c.outcome != "not_run"]
    passed = [c for c in applicable if c.outcome == "pass"]
    if not sup and not passed:
        return RuleVerdict("not_checkable", 4, f"Only partial evidence was found ({_names(par)}), and no check "
                                               "could confirm the claim as stated.")
    reason = f"Supported by {_names(sup or par)}."
    nums = [c for c in passed if c.check in ("number", "dominance")]
    if nums:
        reason += " " + _sentences(nums[0].detail, 1, 300)
    contra_ev = [e for e in evidence if e.relation == "contradicts"]
    caveat = (f"{_names(contra_ev)} may show something different from the claim; the checks did not confirm a "
              "mismatch." if contra_ev else None)
    return RuleVerdict("supported", 5, reason, caveat)


def checklist_verdict(audit: ChecklistAudit | None) -> RuleVerdict | None:
    """Verdict for a checklist claim; None when it is excluded (answered No or NA)."""
    if audit is None:
        return RuleVerdict("not_checkable", 0, "The checklist item was not audited.")
    v = AUDIT_VERDICT[audit.status]
    if v is None:
        return None
    return RuleVerdict(v, 0, _sentences(audit.reason))


def audit_check(audit: ChecklistAudit) -> CheckResult:
    outcome, severity = {"backed": ("pass", "none"), "unbacked": ("fail", "none"),
                         "contradicted": ("fail", "contradicts"), "not_checkable": ("inconclusive", "none"),
                         "not_applicable": ("not_run", "none")}[audit.status]
    return CheckResult(check="checklist", outcome=outcome, severity=severity, detail=audit.reason,  # type: ignore[arg-type]
                       data={"status": audit.status, "answer": audit.answer},
                       evidence_ids=[e.id for e in audit.evidence])


def risk_of(verdict: Verdict) -> int:
    return RISK[verdict]
