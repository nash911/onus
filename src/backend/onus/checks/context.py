"""What every check sees: one `PaperContext` per paper and one `ClaimContext` per claim.

A claim's *linked* evidence is every evidence item the selection kept with a relation other than "unrelated",
plus every object the claim cites (explicit references), whatever its relation. Evidence items carry a 500-char
snippet; the checks need the full text, so it is rebuilt from the Document (tables and figures by id,
paragraphs by location).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

from onus.claims.models import DraftClaim
from onus.claims.quotes import QuoteVerifier
from onus.evidence.explicit import ObjectIndex
from onus.evidence.models import ClaimEvidence
from onus.ingest.lines import normalise
from onus.schemas.document import Document
from onus.schemas.result import Evidence

from .tables import TableView

BODY_EXCLUDED = {"checklist", "ai_checklist", "references"}
PAPER_FEATURE_ROLES = {"method", "experiments", "results", "discussion", "appendix", "other"}


class PaperContext:
    def __init__(self, doc: Document, index: ObjectIndex | None = None):
        self.doc = doc
        self.index = index or ObjectIndex(doc, QuoteVerifier(doc))
        self.v = self.index.v
        self.views: dict[str, TableView] = {t.id: TableView(t) for t in doc.tables}
        self._para_starts = [p.start for p in doc.paragraphs]

    def role_at(self, offset: int) -> str | None:
        sec = self.v.locator.section_at(offset)
        return sec.role if sec else None

    def top_role_at(self, offset: int) -> str | None:
        roles = [s.role for s in self.doc.sections if s.start <= offset < s.end]
        return roles[0] if roles else None

    def body_paragraphs(self, roles: set[str] | None = None):
        """Paragraphs outside checklists and references (optionally only in the given section roles)."""
        for p in self.doc.paragraphs:
            top = self.top_role_at(p.start)
            if top in BODY_EXCLUDED:
                continue
            if roles is not None and self.role_at(p.start) not in roles:
                continue
            yield p

    def paragraph_at(self, offset: int):
        i = bisect_right(self._para_starts, offset) - 1
        if i >= 0 and self.doc.paragraphs[i].start <= offset < max(self.doc.paragraphs[i].end, offset + 1):
            return self.doc.paragraphs[i]
        return None

    def evidence_text(self, e: Evidence) -> str:
        if e.kind in ("table", "figure"):
            c = self.index.get(e.label)
            return c.text if c else e.snippet
        if e.location is not None:
            p = self.paragraph_at(e.location.start)
            if p is not None:
                return normalise(self.doc.text[p.start : p.end])
        return e.snippet

    def text(self, start: int, end: int) -> str:
        return normalise(self.doc.text[start:end])


@dataclass
class ClaimContext:
    claim: DraftClaim
    ce: ClaimEvidence | None
    linked: list[tuple[Evidence, str]] = field(default_factory=list)  # (evidence, full text)
    tables: list[TableView] = field(default_factory=list)  # linked, parsed, with numeric cells
    context: str = ""  # the claim ±1 sentence

    @property
    def evidence_ids(self) -> list[str]:
        return [e.id for e, _ in self.linked]

    def table_ids(self, view: TableView) -> list[str]:
        return [e.id for e, _ in self.linked if e.kind == "table" and e.label == view.id]


def claim_context(pc: PaperContext, claim: DraftClaim, ce: ClaimEvidence | None) -> ClaimContext:
    lo, hi = pc.index.context_span(claim.location.start, claim.location.end)
    ctx = ClaimContext(claim=claim, ce=ce, context=pc.text(lo, hi))
    if ce is None:
        return ctx
    for e in ce.evidence:
        if e.relation == "unrelated" and e.link_method != "explicit_ref":
            continue
        ctx.linked.append((e, pc.evidence_text(e)))
        if e.kind == "table":
            view = pc.views.get(e.label)
            if view is not None and view.table.parsed and view.numeric and all(t.id != view.id for t in ctx.tables):
                ctx.tables.append(view)
    return ctx
