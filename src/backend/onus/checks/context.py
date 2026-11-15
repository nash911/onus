"""What every check sees: one `PaperContext` per paper and one `ClaimContext` per claim.

A claim's *linked* evidence is every evidence item the selection kept with a relation other than "unrelated",
plus every object the claim cites (explicit references), whatever its relation. Evidence items carry a 500-char
snippet; the checks need the full text, so it is rebuilt from the Document (tables and figures by id,
paragraphs by location).

`fallback_tables` are the paper's other parsed numeric tables, ranked by word overlap between the claim and the
table's caption and labels (numbers ignored, so a changed number does not change the ranking). The number check
offers them only when the linked tables cannot account for the claim's numbers.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field

from onus.claims.models import DraftClaim
from onus.claims.quotes import QuoteVerifier
from onus.evidence.explicit import ObjectIndex
from onus.evidence.models import ClaimEvidence
from onus.evidence.retrieve import _STOP
from onus.ingest.lines import normalise
from onus.schemas.document import Document
from onus.schemas.result import Evidence

from .tables import TableView

FALLBACK_TABLES = 3
FALLBACK_MIN_OVERLAP = 2
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
    fallback_tables: list[TableView] = field(default_factory=list)  # unlinked parsed numeric tables, ranked
    context: str = ""  # the claim ±1 sentence

    @property
    def evidence_ids(self) -> list[str]:
        return [e.id for e, _ in self.linked]

    def table_ids(self, view: TableView) -> list[str]:
        return [e.id for e, _ in self.linked if e.kind == "table" and e.label == view.id]


def claim_context(pc: PaperContext, claim: DraftClaim, ce: ClaimEvidence | None) -> ClaimContext:
    lo, hi = pc.index.context_span(claim.location.start, claim.location.end)
    ctx = ClaimContext(claim=claim, ce=ce, context=pc.text(lo, hi))
    if ce is not None:
        for e in ce.evidence:
            if e.relation == "unrelated" and e.link_method != "explicit_ref":
                continue
            ctx.linked.append((e, pc.evidence_text(e)))
            if e.kind == "table":
                view = pc.views.get(e.label)
                if view is not None and view.table.parsed and view.numeric and all(t.id != view.id for t in ctx.tables):
                    ctx.tables.append(view)
    ctx.fallback_tables = fallback_tables(pc, claim.quote, {t.id for t in ctx.tables})
    return ctx


def _words(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z][a-z0-9\-]+", text.lower()) if len(t) > 2 and t not in _STOP}


def fallback_tables(pc: PaperContext, quote: str, exclude: set[str]) -> list[TableView]:
    words = _words(quote)
    scored = []
    for v in pc.views.values():
        if v.id in exclude or not v.table.parsed or not v.numeric:
            continue
        overlap = len(words & _words(" ".join([v.table.caption, *v.col_labels, *v.row_labels])))
        if overlap >= FALLBACK_MIN_OVERLAP:
            scored.append((-overlap, v.id, v))
    return [v for *_, v in sorted(scored, key=lambda x: (x[0], x[1]))[:FALLBACK_TABLES]]
