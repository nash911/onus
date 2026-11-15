"""Coverage ledger and not-checked list. (Phase 6)

The ledger says, per check, how many claims it ran on and why it did not run on the others; it is computed
from the final claims, so it matches what the report shows. `not_checked` lists what Onus could not look at
in this paper (unparsed tables, figure values, code, dropped quotes, unstable claims, ...).
"""

from __future__ import annotations

from collections import Counter

from onus.schemas.document import Document
from onus.schemas.result import ChecklistAudit, CheckName, Claim, LedgerEntry, NotChecked

CLAIM_CHECKS: tuple[CheckName, ...] = ("link", "number", "dominance", "strength", "scope")
CODE_CHECKS: tuple[CheckName, ...] = ("code_static", "code_rerun")
CHECKLIST_REASON = "checklist claim (checked by the checklist audit)"


def build_ledger(claims: list[Claim], audits: list[ChecklistAudit], mode: str) -> list[LedgerEntry]:
    out: list[LedgerEntry] = []
    regular = [c for c in claims if c.type != "checklist"]
    n_checklist = len(claims) - len(regular)
    for name in CLAIM_CHECKS:
        results = [next((r for r in c.checks if r.check == name), None) for c in regular]
        reasons: Counter = Counter()
        for r in results:
            if r is None:
                reasons["not run"] += 1
            elif r.outcome == "not_run":
                reasons[r.data.get("reason", r.detail) or "not run"] += 1
        if n_checklist:
            reasons[CHECKLIST_REASON] += n_checklist
        ran = sum(1 for r in results if r is not None and r.outcome != "not_run")
        out.append(LedgerEntry(check=name, ran_on=ran, not_run_on=sum(reasons.values()), reasons=dict(reasons)))
    na = sum(1 for a in audits if a.status == "not_applicable")
    out.append(LedgerEntry(check="checklist", ran_on=len(audits) - na, not_run_on=na,
                           reasons={"answered No or NA": na} if na else {}))
    why = "code not checked in live mode" if mode == "live" else "code checks are not part of this version"
    for name in CODE_CHECKS:
        out.append(LedgerEntry(check=name, ran_on=0, not_run_on=len(claims), reasons={why: len(claims)} if claims else {}))
    return out


def not_checked(doc: Document, *, mode: str, dropped_quotes: Counter, unstable: int, runs: int,
                limitations_reason: str | None) -> list[NotChecked]:
    out: list[NotChecked] = []
    for t in doc.tables:
        if not t.parsed:
            out.append(NotChecked(what=f"{t.id} not parsed", reason="the table grid could not be extracted; claims "
                                  "resting on it are checked against its caption only"))
    if doc.figures:
        out.append(NotChecked(what="Figure values not extracted", reason="values plotted in figures are not read; "
                              "figures are matched by their captions", count=len(doc.figures)))
    if doc.code.present:
        why = "code not checked in live mode" if mode == "live" else "code checks are not part of this version"
        out.append(NotChecked(what="Submitted code", reason=why, count=len(doc.code.files)))
    if dropped_quotes.get("unverified"):
        out.append(NotChecked(what="Model claims with unverified quotes", reason="the quoted text was not found in "
                              "the paper, so the claim was dropped", count=dropped_quotes["unverified"]))
    if dropped_quotes.get("over_cap"):
        out.append(NotChecked(what="Claims beyond the cap", reason="only the first 25 claims (main claims first) "
                              "are checked", count=dropped_quotes["over_cap"]))
    if unstable:
        out.append(NotChecked(what="Claims found in only one run", reason=f"not reported: a claim must appear in at "
                              f"least 2 of {runs} runs", count=unstable))
    if limitations_reason:
        out.append(NotChecked(what="Scope against limitations", reason=limitations_reason))
    if doc.ai_checklist:
        out.append(NotChecked(what="AI-involvement checklist", reason="shown as submitted; not scored",
                              count=len(doc.ai_checklist)))
    return out
