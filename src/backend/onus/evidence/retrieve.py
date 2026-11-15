"""Retrieval of implicit evidence: BM25 top 10 ∪ embedding top 10, capped at 12. (Phase 4)

Candidate pool, per paper: body paragraphs from the method, experiments, results, discussion, "other" and
appendix sections (not abstract, introduction, related work, conclusion, limitations, references or
checklists), plus one candidate per table (caption + rendering with row/column labels) and per figure
(caption). Paragraphs that are themselves captions are covered by their table/figure, and paragraphs
overlapping the claim are skipped (a claim cannot be its own evidence).

Embeddings use e5-instruct: documents plain, queries with the instruction prefix. They are cached per text
by the LLM layer, so each paper is embedded once across claims and runs.
"""

from __future__ import annotations

import re

import numpy as np
from rank_bm25 import BM25Okapi

from onus.llm.client import LLMError
from onus.schemas.document import Document

from .explicit import ObjectIndex
from .models import Candidate

BM25_K = 10
EMBED_K = 10
MAX_CANDIDATES = 12
MIN_PARAGRAPH_CHARS = 60
EMBED_CHARS = 800  # the embedding model takes 512 tokens; dense tables tokenise at ~2 chars/token
EMBED_CHARS_RETRY = 350
QUERY_PREFIX = "Instruct: Retrieve evidence that supports or refutes this scientific claim\nQuery: "

POOL_ROLES = {"method", "experiments", "results", "discussion", "appendix", "other"}
_CAPTION = re.compile(r"^(?:Table|Figure|Fig\.)\s*[A-Z]?\d+[a-z]?\s*[:.|]", re.IGNORECASE)
_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_STOP = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "by", "from", "at", "as", "is",
    "are", "was", "were", "be", "been", "being", "this", "that", "these", "those", "it", "its", "we", "our",
    "us", "they", "their", "which", "who", "whom", "whose", "than", "then", "there", "here", "into", "over",
    "under", "via", "per", "also", "can", "not", "no", "yes", "such", "both", "each", "other", "more",
    "most", "less", "least", "very", "much", "many", "any", "all", "only", "so", "if", "but", "do", "does",
    "did", "done", "using", "use", "used", "based",
}


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class CandidatePool:
    def __init__(self, doc: Document, index: ObjectIndex):
        self.doc = doc
        self.index = index
        cands: list[Candidate] = []
        for p in doc.paragraphs:
            sec = index.v.locator.section_at(p.start)
            if sec is None or sec.role not in POOL_ROLES or p.end - p.start < MIN_PARAGRAPH_CHARS:
                continue
            if _CAPTION.match(doc.text[p.start : p.start + 40]):
                continue
            cands.append(index.paragraph(p))
        for t in doc.tables:
            c = index.get(t.id)
            if c is not None:
                cands.append(c)
        for f in doc.figures:
            c = index.get(f.id)
            if c is not None:
                cands.append(c)
        self.candidates = cands
        self._bm25 = BM25Okapi([tokens(c.text) or ["_"] for c in cands]) if cands else None
        self._vecs: np.ndarray | None = None

    async def embed_pool(self, llm) -> str | None:
        """Embed every candidate once. Returns an error string on failure (retrieval falls back to BM25)."""
        if not self.candidates:
            return None
        try:
            vecs = await llm.embed([c.text[:EMBED_CHARS] for c in self.candidates], paper_id=self.doc.paper_id)
        except LLMError:
            try:  # most often a context-window error on a dense table: retry shorter
                vecs = await llm.embed([c.text[:EMBED_CHARS_RETRY] for c in self.candidates],
                                       paper_id=self.doc.paper_id)
            except LLMError as e:
                return str(e)[:200]
        m = np.asarray(vecs, dtype=float)
        self._vecs = m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)
        return None

    def _eligible(self, start: int, end: int) -> np.ndarray:
        return np.array([not (c.start >= 0 and c.start < end and c.end > start) for c in self.candidates], dtype=bool)

    def bm25_rank(self, query: str, start: int, end: int, k: int = BM25_K) -> list[int]:
        if self._bm25 is None:
            return []
        q = tokens(query)
        if not q:
            return []
        scores = np.asarray(self._bm25.get_scores(q), dtype=float)
        scores[~self._eligible(start, end)] = -np.inf
        order = [int(i) for i in np.argsort(-scores, kind="stable") if scores[i] > 0]
        return order[:k]

    def embed_rank(self, qvec: list[float] | None, start: int, end: int, k: int = EMBED_K) -> list[int]:
        if self._vecs is None or qvec is None:
            return []
        q = np.asarray(qvec, dtype=float)
        q = q / max(float(np.linalg.norm(q)), 1e-12)
        sims = self._vecs @ q
        sims[~self._eligible(start, end)] = -np.inf
        return [int(i) for i in np.argsort(-sims, kind="stable")[:k] if np.isfinite(sims[i])]

    def retrieve(self, query: str, qvec: list[float] | None, start: int, end: int,
                 cap: int = MAX_CANDIDATES) -> tuple[list[Candidate], dict[str, list[str]]]:
        """Union of the two rankings, interleaved (BM25 first), capped. Also returns which ranker found what."""
        b = self.bm25_rank(query, start, end)
        e = self.embed_rank(qvec, start, end)
        out: list[int] = []
        for i in range(max(len(b), len(e))):
            for lst in (b, e):
                if i < len(lst) and lst[i] not in out and len(out) < cap:
                    out.append(lst[i])
        found = {"bm25": [self.candidates[i].key for i in b], "embed": [self.candidates[i].key for i in e]}
        return [self.candidates[i] for i in out], found


async def embed_queries(llm, quotes: list[str], paper_id: str) -> tuple[list[list[float]] | None, str | None]:
    if not quotes:
        return [], None
    try:
        return await llm.embed([QUERY_PREFIX + q for q in quotes], paper_id=paper_id), None
    except LLMError as e:
        return None, str(e)[:200]
