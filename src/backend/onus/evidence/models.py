"""Evidence candidates and the Phase 4 output (`EvidenceSet`), consumed by the checks in Phase 5."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from onus.schemas.result import CheckResult, Evidence, LedgerEntry, Location

CandidateKind = Literal["table", "figure", "text"]


@dataclass(frozen=True)
class Candidate:
    """A locatable object a claim could rest on. `key` is stable within a Document:
    "para:<start>", "Table 2", "Figure 3", "sec:3.1" or "eq:5"."""

    key: str
    kind: CandidateKind
    label: str  # "Table 2", "Figure 3", "p. 7, ¶ 3", "Section 3.1 Setup", "Equation (5)"
    text: str  # what retrieval and the selection prompt see
    snippet: str  # what the report shows
    location: Location | None
    start: int = -1  # span in Document.text, -1 when unknown
    end: int = -1

    def evidence(self, ev_id: str, link_method: str, relation: str | None) -> Evidence:
        return Evidence(id=ev_id, kind=self.kind, label=self.label, snippet=self.snippet, location=self.location,
                        link_method=link_method, relation=relation)  # type: ignore[arg-type]


class ClaimEvidence(BaseModel):
    claim_id: str
    evidence: list[Evidence] = Field(default_factory=list)
    explicit: list[str] = Field(default_factory=list)  # resolved labels the claim (±1 sentence) cites
    dangling: list[str] = Field(default_factory=list)  # cited labels that resolve to nothing in the paper
    candidates: list[str] = Field(default_factory=list)  # candidate keys shown to the selection prompt
    notes: dict[str, str] = Field(default_factory=dict)  # evidence id -> the model's note
    select_failed: bool = False
    link: CheckResult


class EvidenceSet(BaseModel):
    paper_id: str
    run_index: int = 0
    fingerprint: str
    claims: list[ClaimEvidence] = Field(default_factory=list)
    ledger: LedgerEntry
    stats: dict = Field(default_factory=dict)
    cost: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    def for_claim(self, claim_id: str) -> ClaimEvidence | None:
        return next((c for c in self.claims if c.claim_id == claim_id), None)
