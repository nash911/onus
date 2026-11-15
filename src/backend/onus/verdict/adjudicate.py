"""Judge-model adjudication: may confirm or downgrade, never create or upgrade a flag. (Phase 6)

Prompt `adjudicate` (judge role) runs on every claim whose rule verdict is not `supported`. It sees the claim,
its evidence, its check results and the rule verdict, and answers `{decision, evidence_ids, parse_problem,
reason, caveat}`. Code enforces the limits:
- "confirm": the rule verdict stands; the judge's reason and caveat replace the code-written ones.
- "supported": allowed only when it cites >= 1 evidence id of this claim (the ids it was shown).
- "not_checkable": allowed only with a stated parse problem (e.g. a misread table), and only from a more
  severe verdict.
Anything else (an unknown decision, an invalid id, a missing parse problem) keeps the rule verdict, and the
attempt is recorded in `adjudication`. If the call fails, the rule verdict and the code reason stand.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from onus.llm.client import CallMeta, LLMError
from onus.schemas.result import RISK, CheckResult, Evidence, Verdict, Wording

from .rules import REASON_CHARS, RuleVerdict

SNIPPET_CHARS = 600


class AdjudicateOut(BaseModel):
    decision: str = "confirm"
    evidence_ids: list[str] = Field(default_factory=list)
    parse_problem: str = ""
    reason: str = ""
    caveat: str = ""


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def render_evidence(evidence: list[Evidence]) -> str:
    if not evidence:
        return "(none)"
    out = []
    for e in evidence:
        loc = f", {e.location.cite()}" if e.location else ""
        out.append(f"[{e.id}] {e.relation or 'unjudged'} · {e.label}{loc}\n{_clip(e.snippet, SNIPPET_CHARS)}")
    return "\n\n".join(out)


def render_checks(checks: list[CheckResult]) -> str:
    lines = []
    for c in checks:
        if c.outcome == "not_run":
            continue
        sev = f", {c.severity}" if c.severity != "none" else ""
        lines.append(f"- {c.check}: {c.outcome}{sev}. {_clip(c.detail, 700)}")
    return "\n".join(lines) or "(no check ran)"


def _sentences(s: str) -> str:
    return _clip(s, REASON_CHARS)


async def adjudicate(*, quote: str, section: str | None, wording: Wording, evidence: list[Evidence],
                     checks: list[CheckResult], rule: RuleVerdict, llm, run_index: int, paper_id: str,
                     metas: list[CallMeta]) -> tuple[Verdict, str, str | None, dict]:
    """Returns (verdict, reason, caveat, adjudication record)."""
    try:
        out, meta = await llm.complete_json("adjudicate", {
            "claim": quote, "section": section or "the paper",
            "wording": f"strength {wording.strength}, scope {wording.scope}"
                       + (f" (markers: {', '.join(wording.markers[:6])})" if wording.markers else ""),
            "verdict": rule.verdict, "rule_reason": rule.reason,
            "evidence": render_evidence(evidence), "checks": render_checks(checks)},
            AdjudicateOut, run_index=run_index, paper_id=paper_id)
        metas.append(meta)
    except LLMError as e:
        return rule.verdict, rule.reason, rule.caveat, {"decision": None, "applied": False,
                                                        "note": f"adjudication failed: {str(e)[:120]}"}
    decision = out.decision.strip().lower().replace(" ", "_")
    valid = {e.id for e in evidence}
    cited = [i.strip().strip("[]") for i in out.evidence_ids if i.strip().strip("[]") in valid]
    rec = {"decision": decision, "evidence_ids": cited, "parse_problem": out.parse_problem.strip(), "applied": True,
           "note": ""}
    reason = _sentences(out.reason) if out.reason.strip() else rule.reason
    caveat = _clip(out.caveat, REASON_CHARS) if out.caveat.strip() else rule.caveat
    if decision == "confirm":
        return rule.verdict, reason, caveat, rec
    if decision == "supported":
        if not cited:
            return rule.verdict, rule.reason, rule.caveat, rec | {"applied": False,
                                                                  "note": "downgrade cited no valid evidence id"}
        return "supported", reason, caveat, rec
    if decision == "not_checkable":
        if not rec["parse_problem"]:
            return rule.verdict, rule.reason, rule.caveat, rec | {"applied": False,
                                                                  "note": "downgrade cited no parse problem"}
        if RISK["not_checkable"] >= RISK[rule.verdict]:
            return rule.verdict, reason, caveat, rec | {"applied": False, "note": "not a downgrade"}
        return "not_checkable", reason, caveat, rec
    return rule.verdict, rule.reason, rule.caveat, rec | {"applied": False, "note": f"unknown decision {decision!r}"}
