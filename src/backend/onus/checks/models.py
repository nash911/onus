"""Phase 5 output (`CheckSet`), consumed by the verdict rules in Phase 6."""

from __future__ import annotations

from pydantic import BaseModel, Field

from onus.schemas.result import ChecklistAudit, CheckName, CheckResult, LedgerEntry, Location

CLAIM_CHECKS: tuple[CheckName, ...] = ("number", "dominance", "strength", "scope")


def not_run(check: CheckName, reason: str, **data) -> CheckResult:
    return CheckResult(check=check, outcome="not_run", detail=reason, data={"reason": reason, **data})


class ClaimChecks(BaseModel):
    claim_id: str
    checks: list[CheckResult] = Field(default_factory=list)  # one per CLAIM_CHECKS entry, in that order
    rubric: dict = Field(default_factory=dict)  # F4 rubric fields (seeds, sample size, variance, tests, ...)

    def get(self, check: str) -> CheckResult | None:
        return next((c for c in self.checks if c.check == check), None)


class Limitation(BaseModel):
    id: str  # "L1"
    quote: str  # verified paper text
    location: Location


class CheckSet(BaseModel):
    paper_id: str
    run_index: int = 0
    fingerprint: str
    claims: list[ClaimChecks] = Field(default_factory=list)
    checklist: list[ChecklistAudit] = Field(default_factory=list)
    derived_checklist: list[str] = Field(default_factory=list)  # item keys settled after verdicts (Phase 6)
    limitations: list[Limitation] = Field(default_factory=list)
    ledger: list[LedgerEntry] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
    cost: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    def for_claim(self, claim_id: str) -> ClaimChecks | None:
        return next((c for c in self.claims if c.claim_id == claim_id), None)
