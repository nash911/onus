"""Intermediate claim records: the output of Phase 3, before evidence, checks and verdicts.

`DraftClaim` becomes `onus.schemas.result.Claim` in the pipeline (Phase 6) once it has a verdict.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from onus.schemas.result import Location, Wording

ClaimType = Literal["result", "comparison", "generality", "causal", "method", "availability", "checklist"]
LLM_TYPES: tuple[str, ...] = ("result", "comparison", "generality", "causal", "method", "availability")


class DraftClaim(BaseModel):
    id: str  # sha1(match key of the quote)[:12]; checklist claims also hash their item key
    type: ClaimType
    quote: str  # the paper's own text at `location`, whitespace collapsed
    location: Location
    is_main: bool = False
    wording: Wording
    origin: Literal["llm", "checklist", "checklist_number"]
    section_role: str | None = None
    checklist_key: str | None = None
    match: Literal["exact", "exact_nonum", "fuzzy", "parse"] = "parse"
    match_score: float = 100.0
    scope_source: Literal["markers", "model", "default"] = "default"
    model_scope: str | None = None


class DroppedQuote(BaseModel):
    quote: str
    reason: Literal["unverified", "too_short", "too_long", "unlocatable", "duplicate", "over_cap"]


class ClaimSet(BaseModel):
    paper_id: str
    run_index: int = 0
    fingerprint: str
    claims: list[DraftClaim] = Field(default_factory=list)
    dropped: list[DroppedQuote] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
    cost: dict = Field(default_factory=dict)  # {eur, calls, cache_hits, tokens_in, tokens_out}
    warnings: list[str] = Field(default_factory=list)

    def by_origin(self, origin: str) -> list[DraftClaim]:
        return [c for c in self.claims if c.origin == origin]
