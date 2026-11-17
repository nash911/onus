"""PDF anchors for the report page (Phase 13a): where each claim and evidence item sits in the paper's PDF.

`compute_anchors(doc, pdf_path, result)` returns the JSON served at GET /api/analyses/{id}/anchors:

    {"pdf": true,
     "page_sizes": [[w, h], ...],                       # PDF points, index 0 = page 1
     "items": {"<claim_id>": [{"page": 3, "rects": [[x0, y0, x1, y1], ...], "method": "offsets"}],
               "<claim_id>:<ev_id>": [{"page": 5, "rects": [...], "method": "table"}]},
     "missing": ["<key>", ...]}

Coordinates are PDF points with a **top-left origin** (PyMuPDF's convention: y grows downwards), measured on the
page as displayed (`page.rect`: crop box, rotation applied; rects on a rotated page are rotated to match). The viewer scales them by its viewport scale. One item per
page the text touches, in reading order; one rect per text line.

Methods, tried in this order:
1. `offsets`: the Document lines that overlap the item's `[start, end)` character span, the first and last line
   trimmed to the words the span covers (PyMuPDF words on that page, inside the line box). Used only when
   `Document.text[start:end]` agrees with the claim quote or evidence snippet (same words after normalisation).
   A clipped evidence snippet ("…") highlights only the part of the span the snippet shows; a location that
   covers only the start of a longer snippet (a section's first paragraph, a long caption) is extended along the
   text to the snippet's end when the text goes on with the snippet's words.
2. `table`: table and table-cell evidence: the cell's words when the cell can be found inside the table, else the
   table's box.
3. `search`: `page.search_for` on the quote (whitespace, ligatures and line-break hyphens normalised) on
   `location.page`, then the pages before and after; the first 12 words when the full text is not found.
4. `paragraph`: every line of the paragraph at `location.page` / `location.paragraph`.

Keys: the claim id for the claim quote, `claim_id:ev_id` for each evidence item (evidence ids repeat across
claims). Code and re-run evidence has no place in the PDF and is left out; an item that has a PDF location but could
not be placed is listed in `missing`.

The Document comes from the ingest cache (`doc_path`) when its PDF sha matches, else from `ingest_pdf` on the stored
file (deterministic). Results are cached at `$ONUS_VAR_DIR/anchors/<analysis_id>.json`. Nothing here changes a
result or a verdict; it only reads them.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from onus.schemas.document import Document, Line

ANCHORS_VERSION = 2
SEARCH_FLAGS = pymupdf.TEXT_DEHYPHENATE | pymupdf.TEXT_MEDIABOX_CLIP  # no TEXT_PRESERVE_LIGATURES: "ﬁ" -> "fi"
NO_PDF_PLACE = {"code", "rerun_output"}
TABLE_KINDS = {"table", "table_cell"}
SEARCH_WORDS = 12
MIN_PREFIX_WORDS = 8  # a location that covers only the start of its snippet must still show this many words
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_INVISIBLE = dict.fromkeys(map(ord, "\u00ad\u200b\u200c\u200d\ufeff"), None)
_WORD = re.compile(r"\w+")
_CELL_LABEL = re.compile(r"row '(?P<row>[^']*)'.*?col '(?P<col>[^']*)'")

Rect = list[float]


def empty() -> dict[str, Any]:
    return {"pdf": False, "page_sizes": [], "items": {}, "missing": []}


# ---------------------------------------------------------------- text normalisation


def norm(text: str) -> str:
    """NFKC (ligatures), no soft hyphens or zero-width marks, single spaces."""
    return " ".join(unicodedata.normalize("NFKC", text).translate(_INVISIBLE).split())


def words_of(text: str) -> list[str]:
    return _WORD.findall(norm(text).lower())


def snippet_core(snippet: str) -> tuple[str, bool]:
    """(text, clipped): a snippet clipped with "…" loses its last, possibly cut, word."""
    s = snippet.strip()
    if s.endswith("…"):
        s = s[:-1].rstrip()
        return (s.rsplit(" ", 1)[0] if " " in s else s), True
    return s, False


# ---------------------------------------------------------------- the PDF


@dataclass
class Pdf:
    doc: pymupdf.Document
    _words: dict[int, list] = field(default_factory=dict)

    @classmethod
    def open(cls, path: Path) -> Pdf:
        return cls(pymupdf.open(path))

    @property
    def pages(self) -> int:
        return self.doc.page_count

    def page(self, n: int) -> pymupdf.Page:  # 1-based
        return self.doc[n - 1]

    def words(self, n: int) -> list:
        if n not in self._words:
            self._words[n] = self.page(n).get_text("words", flags=pymupdf.TEXT_MEDIABOX_CLIP)
        return self._words[n]

    def sizes(self) -> list[list[float]]:
        return [[round(p.rect.width, 2), round(p.rect.height, 2)] for p in self.doc]

    def displayed(self, n: int, rects: list[Rect]) -> list[Rect]:
        """Text coordinates are in the unrotated page; a rotated page is shown (and sized in `page_sizes`) rotated."""
        page = self.page(n)
        if not page.rotation:
            return rects
        m = page.rotation_matrix
        return [_r((pymupdf.Rect(r) * m).normalize()) for r in rects]

    def close(self) -> None:
        self.doc.close()


def _r(r) -> Rect:
    return [round(float(r[0]), 2), round(float(r[1]), 2), round(float(r[2]), 2), round(float(r[3]), 2)]


def _line_words(pdf: Pdf, line: Line) -> list:
    x0, y0, x1, y1 = line.bbox
    ws = [w for w in pdf.words(line.page)
          if x0 - 1 <= (w[0] + w[2]) / 2 <= x1 + 1 and y0 - 1 <= (w[1] + w[3]) / 2 <= y1 + 1]
    return sorted(ws, key=lambda w: w[0])


def _trim(pdf: Pdf, line: Line, a: int, b: int) -> Rect | None:
    """The part of `line` that covers characters [a, b) of its text."""
    t = line.text
    a, b = max(0, a), min(len(t), b)
    if not t[a:b].strip():
        return None
    x0, y0, x1, y1 = line.bbox
    if a == 0 and b >= len(t):
        return _r(line.bbox)
    ws = _line_words(pdf, line)
    compact = "".join(norm(t).split())
    wtext = ["".join(norm(w[4]).split()) for w in ws]
    if ws and sum(map(len, wtext)) == len(compact):
        na = len("".join(norm(t[:a]).split()))  # non-space characters before the span
        nb = len("".join(norm(t[:b]).split()))
        pos, i0, i1 = 0, None, None
        for i, w in enumerate(wtext):
            lo, hi = pos, pos + len(w)
            if i0 is None and hi > na:
                i0 = i
            if lo < nb:
                i1 = i
            pos = hi
        if i0 is not None and i1 is not None and i1 >= i0:
            return _r((ws[i0][0], y0, ws[i1][2], y1))
    n = max(1, len(t))  # words do not line up with the text: interpolate along the line
    return _r((x0 + (x1 - x0) * a / n, y0, x0 + (x1 - x0) * b / n, y1))


def _group(rects: list[tuple[int, Rect]], method: str) -> list[dict]:
    out: list[dict] = []
    for page, r in rects:
        if out and out[-1]["page"] == page:
            out[-1]["rects"].append(r)
        else:
            out.append({"page": page, "rects": [r], "method": method})
    return out


# ---------------------------------------------------------------- methods


def by_offsets(doc: Document, pdf: Pdf, start: int, end: int) -> list[dict]:
    rects = []
    for ln in doc.lines:
        if ln.start < end and ln.end > start and 1 <= ln.page <= pdf.pages:
            r = _trim(pdf, ln, start - ln.start, end - ln.start)
            if r is not None:
                rects.append((ln.page, r))
    return _group(rects, "offsets")


def by_search(pdf: Pdf, page: int | None, text: str) -> list[dict]:
    q = norm(text)
    if not q:
        return []
    head = " ".join(q.split()[:SEARCH_WORDS])
    pages = [p for p in ([page, page - 1, page + 1] if page else range(1, pdf.pages + 1)) if 1 <= p <= pdf.pages]
    for cand in dict.fromkeys((q, head)):
        for p in pages:
            hits = pdf.page(p).search_for(cand, flags=SEARCH_FLAGS)
            if hits:
                return _group([(p, _r(h)) for h in hits], "search")
    return []


def by_paragraph(doc: Document, pdf: Pdf, page: int, index: int) -> list[dict]:
    para = next((p for p in doc.paragraphs if p.page == page and p.index == index), None)
    if para is None:
        return []
    rects = [(ln.page, _r(ln.bbox)) for ln in doc.lines
             if ln.start < para.end and ln.end > para.start and 1 <= ln.page <= pdf.pages]
    return _group(rects, "paragraph")


def find_table(doc: Document, label: str):
    tid = label.split(",")[0].strip()
    return next((t for t in doc.tables if t.id == tid), None)


def by_table(doc: Document, pdf: Pdf, kind: str, label: str) -> list[dict]:
    t = find_table(doc, label)
    if t is None or not 1 <= t.page <= pdf.pages:
        return []
    if kind == "table_cell" and t.rows and (m := _CELL_LABEL.search(label)):
        raw = _cell_raw(t, m["row"], m["col"])
        if raw:
            hits = pdf.page(t.page).search_for(norm(raw), clip=pymupdf.Rect(t.bbox), flags=SEARCH_FLAGS)
            if len(hits) == 1:
                return [{"page": t.page, "rects": [_r(hits[0])], "method": "table"}]
    return [{"page": t.page, "rects": [_r(t.bbox)], "method": "table"}]


def _cell_raw(t, row: str, col: str) -> str | None:
    header = t.rows[max(0, t.header_rows - 1)] if t.header_rows else t.rows[0]
    cols = [i for i, c in enumerate(header) if norm(c.raw) == norm(col)]
    for r in t.rows[t.header_rows:]:
        if r and norm(r[0].raw) == norm(row) and cols and cols[0] < len(r):
            return r[cols[0]].raw
    return None


# ---------------------------------------------------------------- one item


def _end_at(ms: list, chars: int) -> int | None:
    """End offset of the word where the concatenated words reach `chars` characters."""
    n = 0
    for m in ms:
        n += len(m.group(0))
        if n >= chars:
            return m.end()
    return None


def checked_span(doc: Document, start: int, end: int, ref: str) -> tuple[int, int] | None:
    """The span to highlight when `Document.text[start:end]` agrees with `ref` (else None).

    Words are compared as one string with the word boundaries removed, so a line-end hyphen that ingest joined
    ("riskbased") still matches the snippet's "risk-based".
    - the same words: the span;
    - the span starts with the reference's words and goes on (a clipped evidence snippet): the span up to the last
      word the reference shows;
    - the span's words are the start of the reference (a section's first paragraph, a caption found by its first
      240 characters; its last word may be cut): the span extended along Document.text to the reference's last
      word when the text there goes on with the same words, else the span as stored."""
    if not (0 <= start < end <= len(doc.text)):
        return None
    want = "".join(words_of(ref))
    if not want:
        return None
    ms = list(_WORD.finditer(doc.text, start, end))  # Document.text is already NFKC-normalised by ingest
    have = "".join(m.group(0) for m in ms).lower()
    if have == want:
        return start, end
    if have.startswith(want):
        cut = _end_at(ms, len(want))
        return (start, cut) if cut is not None else (start, end)
    head = have if want.startswith(have) else "".join(m.group(0) for m in ms[:-1]).lower()
    if len(ms) - (head != have) >= MIN_PREFIX_WORDS and want.startswith(head):
        ext, n = [], 0
        for m in _WORD.finditer(doc.text, start):
            ext.append(m)
            n += len(m.group(0))
            if n >= len(want):
                break
        if "".join(m.group(0) for m in ext).lower() == want:
            return start, ext[-1].end()
        return start, end
    return None


def place(doc: Document, pdf: Pdf, *, text: str, loc: dict | None, kind: str = "claim",
          label: str = "") -> list[dict]:
    if kind in TABLE_KINDS:
        got = by_table(doc, pdf, kind, label)
        if got:
            return got
    if loc:
        span = checked_span(doc, int(loc["start"]), int(loc["end"]), text)
        if span is not None:
            got = by_offsets(doc, pdf, *span)
            if got:
                return got
    got = by_search(pdf, int(loc["page"]) if loc else None, text)
    if got:
        return got
    if loc:
        return by_paragraph(doc, pdf, int(loc["page"]), int(loc["paragraph"]))
    return []


def compute_anchors(doc: Document, pdf_path: Path, result: dict) -> dict[str, Any]:
    pdf = Pdf.open(pdf_path)
    try:
        items: dict[str, list[dict]] = {}
        missing: list[str] = []
        for c in result.get("claims") or []:
            cid = c["id"]
            got = place(doc, pdf, text=c.get("quote") or "", loc=c.get("location"))
            (items.__setitem__(cid, got) if got else missing.append(cid))
            for e in c.get("evidence") or []:
                kind = e.get("kind")
                if kind in NO_PDF_PLACE or (e.get("location") is None and kind not in TABLE_KINDS):
                    continue
                key = f"{cid}:{e['id']}"
                core, _ = snippet_core(e.get("snippet") or "")
                got = place(doc, pdf, text=core, loc=e.get("location"), kind=kind, label=e.get("label") or "")
                (items.__setitem__(key, got) if got else missing.append(key))
        for its in items.values():
            for it in its:
                it["rects"] = pdf.displayed(it["page"], it["rects"])
        return {"pdf": True, "page_sizes": pdf.sizes(), "items": items, "missing": missing}
    finally:
        pdf.close()


# ---------------------------------------------------------------- serving: stored PDF, Document, cache


def stored_pdf(var_dir: Path, sha: str | None) -> Path | None:
    """`$ONUS_VAR_DIR/uploads/<sha256>.pdf` when it exists. The sha comes from the database, never the URL."""
    if not sha or not SHA_RE.match(sha):
        return None
    p = var_dir / "uploads" / f"{sha}.pdf"
    return p if p.is_file() else None


def load_doc(var_dir: Path, paper_id: str, source: str, pdf: Path, sha: str) -> Document:
    from onus.ingest.document_builder import doc_path, ingest_pdf, load_document

    p = doc_path(var_dir, paper_id)
    if p.is_file():
        try:
            d = load_document(p)
            if d.pdf_sha256 == sha:
                return d
        except (OSError, ValueError):
            pass
    return ingest_pdf(pdf, var_dir, source=source or "upload", paper_id=paper_id)


def cache_path(var_dir: Path, analysis_id: str) -> Path:
    return var_dir / "anchors" / f"{analysis_id}.json"


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def anchors_for(var_dir: Path, analysis_id: str, result: dict, sha: str | None) -> dict[str, Any]:
    """Cached anchors for one analysis (`pdf: false` when no PDF is stored). Blocking: call it in a thread."""
    pdf = stored_pdf(var_dir, sha)
    if pdf is None:
        return empty()
    stamp = {"version": ANCHORS_VERSION, "pdf_sha256": sha, "result_created_at": result.get("created_at")}
    cp = cache_path(var_dir, analysis_id)
    with _lock(analysis_id):
        if cp.is_file():
            try:
                cached = json.loads(cp.read_text())
                if cached.get("stamp") == stamp:
                    return cached["anchors"]
            except (OSError, ValueError):
                pass
        paper = result.get("paper") or {}
        assert sha is not None
        doc = load_doc(var_dir, str(paper.get("id") or f"upload:{sha[:12]}"), str(paper.get("source") or ""),
                       pdf, sha)
        anchors = compute_anchors(doc, pdf, result)
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp = cp.with_suffix(".tmp")
        tmp.write_text(json.dumps({"stamp": stamp, "anchors": anchors}))
        tmp.replace(cp)
        return anchors
