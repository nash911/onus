"""Explicit references: mentions in the claim and its ±1 sentence context → resolved objects. (Phase 4)

Code resolves "Table 2", "Fig. 3", "Section 4.1", "Appendix B" and "Eq. (5)" through the ingest mention index;
the model is not involved. A mention that resolves to nothing in the paper is a **dangling reference**,
which feeds `untraced` in the verdict rules.

**External pointers** (v1.1): wording in the claim ±1 sentence that sends the reader outside the PDF (a video, a
project page or URL, supplementary material or a supplementary table/figure, an extended version, data "available
on request"). When that is all a claim has, the claim cannot be checked from the paper: the verdict rules make it
`not_checkable` instead of `untraced`. Supplementary material counts as outside only when the paper has no section
of that name (an appendix titled "Supplementary Material" is inside the PDF).
"""

from __future__ import annotations

import re
from bisect import bisect_right

from onus.claims.quotes import QuoteVerifier
from onus.ingest.lines import normalise
from onus.ingest.tables import render_table
from onus.schemas.document import Document, Paragraph
from onus.schemas.result import Location

from .models import Candidate

SNIPPET_CHARS = 500
TABLE_TEXT_CHARS = 1800


def clip(text: str, n: int) -> str:
    text = normalise(text) if "\n" not in text else text.strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


class ObjectIndex:
    """Candidates for every table, figure, section and paragraph of a Document, built once per paper."""

    def __init__(self, doc: Document, verifier: QuoteVerifier | None = None):
        self.doc = doc
        self.v = verifier or QuoteVerifier(doc)
        self._cache: dict[str, Candidate | None] = {}
        self.sentences: list[tuple[int, int]] = [s for p in doc.paragraphs for s in p.sentences]
        self._sent_starts = [s for s, _ in self.sentences]

    # ------------------------------------------------------------ objects

    def _caption_span(self, caption: str) -> tuple[Location | None, int, int]:
        if not caption.strip():
            return None, -1, -1
        m = self.v.find(caption[:240])
        if m is None:
            return None, -1, -1
        return self.v.locate(m.start, m.end), m.start, m.end

    def get(self, key: str) -> Candidate | None:
        if key not in self._cache:
            self._cache[key] = self._build(key)
        return self._cache[key]

    def _build(self, key: str) -> Candidate | None:
        doc = self.doc
        if key.startswith("para:"):
            start = int(key[5:])
            p = next((p for p in doc.paragraphs if p.start == start), None)
            return self.paragraph(p) if p else None
        if key.startswith("Table "):
            t = next((t for t in doc.tables if t.id == key), None)
            if t is None:
                return None
            loc, s, e = self._caption_span(t.caption)
            text = clip(render_table(t), TABLE_TEXT_CHARS)
            return Candidate(key=key, kind="table", label=t.id, text=text, snippet=clip(text, 700), location=loc,
                             start=s, end=e)
        if key.startswith("Figure "):
            f = next((f for f in doc.figures if f.id == key), None)
            if f is None:
                return None
            loc, s, e = self._caption_span(f.caption)
            cap = f.caption if f.caption.startswith(f.id) else f"{f.id}: {f.caption}"
            return Candidate(key=key, kind="figure", label=f.id, text=clip(cap, 1200), snippet=clip(cap, SNIPPET_CHARS),
                             location=loc, start=s, end=e)
        if key.startswith("sec:"):
            sec = next((s for s in doc.sections if s.id == key), None)
            if sec is None:
                return None
            paras = [p for p in doc.paragraphs if sec.start <= p.start < sec.end]
            body = " ".join(normalise(doc.text[p.start : p.end]) for p in paras[:3])
            name = f"{sec.number} {sec.title}" if sec.number else sec.title
            label = f"{'Appendix' if sec.role == 'appendix' else 'Section'} {name}"
            loc = self.v.locate(paras[0].start, paras[0].end) if paras else None
            return Candidate(key=key, kind="text", label=label, text=clip(f"{name}. {body}", 1500),
                             snippet=clip(body or name, SNIPPET_CHARS), location=loc,
                             start=paras[0].start if paras else -1, end=paras[0].end if paras else -1)
        if key.startswith("eq:"):
            tag = key[3:]
            m = re.search(rf"\({re.escape(tag)}\)\s*(?:\n|$)", doc.text)
            if m is None:
                return None
            ln = next((ln for ln in doc.lines if ln.start <= m.start() < ln.end + 1), None)
            if ln is None:
                return None
            loc = self.v.locate(ln.start, ln.end)
            return Candidate(key=key, kind="text", label=f"Equation ({tag})", text=ln.text, snippet=clip(ln.text, 300),
                             location=loc, start=ln.start, end=ln.end)
        return None

    def paragraph(self, p: Paragraph) -> Candidate:
        text = normalise(self.doc.text[p.start : p.end])
        sec = self.v.locator.section_at(p.start)
        where = f"p. {p.page}, ¶ {p.index}"
        label = f"{where} ({sec.number + ' ' if sec and sec.number else ''}{sec.title})" if sec else where
        return Candidate(key=f"para:{p.start}", kind="text", label=label, text=clip(text, 1500),
                         snippet=clip(text, SNIPPET_CHARS), location=self.v.locate(p.start, p.end), start=p.start,
                         end=p.end)

    # ------------------------------------------------------------ explicit references

    def context_span(self, start: int, end: int, k: int = 1) -> tuple[int, int]:
        """The claim span widened by k sentences on each side (document order)."""
        if not self.sentences:
            return start, end
        i = max(bisect_right(self._sent_starts, start) - 1, 0)
        j = max(bisect_right(self._sent_starts, max(end - 1, start)) - 1, i)
        lo = self.sentences[max(i - k, 0)][0]
        hi = self.sentences[min(j + k, len(self.sentences) - 1)][1]
        return min(lo, start), max(hi, end)


_SUPPLEMENT = r"(?:supplementary|supplemental)"
# (label, pattern, needs no in-paper supplement section)
EXTERNAL_POINTERS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("a video", re.compile(r"\b(?:see|watch|in|shown in|available (?:in|at|on)|provided (?:in|at)|included in) "
                           r"(?:the |our |a |an )?(?:accompanying |supplementary |supplemental |demo |demonstration |"
                           r"attached )?videos?\b|\b(?:accompanying|supplementary|supplemental|demo|demonstration|"
                           r"attached) videos?\b|\bvideos? (?:is|are) (?:available|provided|included|attached)\b|"
                           r"\b(?:youtube|vimeo)\b", re.IGNORECASE), False),
    ("a project page", re.compile(r"\bproject (?:page|website|site)\b", re.IGNORECASE), False),
    ("a web link", re.compile(r"(?:https?://|\bwww\.)\S+", re.IGNORECASE), False),
    ("an extended version", re.compile(r"\b(?:extended|full|longer|journal) version of (?:this|the|our) "
                                       r"(?:paper|work|article)\b", re.IGNORECASE), False),
    ("material available on request", re.compile(r"\b(?:available|provided|shared) (?:up)?on (?:reasonable )?"
                                                 r"request\b", re.IGNORECASE), False),
    ("supplementary material", re.compile(_SUPPLEMENT + r" (?:materials?|files?|information|data|documents?|"
                                          r"results|archive|zip)\b|\b(?:table|figure|fig\.) S\d+", re.IGNORECASE),
     True),
)
_SUPPLEMENT_SECTION = re.compile(r"\b" + _SUPPLEMENT + r"\b|\bsupplement\b", re.IGNORECASE)


def external_pointers(index: ObjectIndex, start: int, end: int) -> list[str]:
    """What the claim ±1 sentence points to outside the PDF, as short labels (empty when nothing)."""
    lo, hi = index.context_span(start, end)
    text = index.doc.text[lo:hi]
    has_supplement = any(_SUPPLEMENT_SECTION.search(s.title) for s in index.doc.sections)
    out: list[str] = []
    for label, pat, needs_no_section in EXTERNAL_POINTERS:
        if needs_no_section and has_supplement:
            continue
        if pat.search(text) and label not in out:
            out.append(label)
    return out


def explicit_refs(index: ObjectIndex, start: int, end: int) -> tuple[list[Candidate], list[str]]:
    """Resolved objects cited in [start, end) ±1 sentence, and labels of dangling references."""
    lo, hi = index.context_span(start, end)
    found: list[Candidate] = []
    dangling: list[str] = []
    for m in index.doc.mentions:
        if m.end <= lo or m.start >= hi:
            continue
        if m.resolved_id is None:
            if m.label not in dangling:
                dangling.append(m.label)
            continue
        c = index.get(m.resolved_id)
        if c is None:
            if m.label not in dangling:
                dangling.append(m.label)
        elif all(x.key != c.key for x in found):
            found.append(c)
    return found, dangling
