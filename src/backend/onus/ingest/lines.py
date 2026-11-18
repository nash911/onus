"""Line-number detection and removal, header/footer removal, de-hyphenation, paragraphs and sentences. (Phase 2)

Pipeline inside this module (all pure functions on `RawPage`/`Row`, no PyMuPDF):

1. `strip_line_numbers`: digit-only fragments left of the text column (or right of it) whose values rise down
   the page are review line numbers. They are removed from the text and kept only as `Row.line_no`.
2. `merge_rows`: PyMuPDF fragments on the same baseline become one visual row (column-aware).
3. `remove_headers_footers`: rows repeated in the top/bottom band of >= 50% of pages, and page numbers.
4. `group_blocks`: rows -> paragraphs by vertical gap, first-line indent, font change, bullets, captions
   and headings (heading detection itself lives in `sections.py`).
5. `build_text`: `Document.text` with char offsets per line, paragraph and sentence. Line-end hyphens before
   a lowercase continuation are removed.

Line numbers are citation metadata only: every paper goes through the same path and gets paragraphs and
sentences, and nothing here changes based on whether a paper has line numbers.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import pairwise

from onus.ingest.pdf import BBox, RawLine, RawPage

# ---------------------------------------------------------------- text normalisation

_INVISIBLE = dict.fromkeys(map(ord, "­\u200b‌‍⁠﻿"), None)
_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    """NFKC (also expands ligatures such as "ﬁ"), drop soft hyphens and zero-width marks, collapse spaces."""
    text = unicodedata.normalize("NFKC", text).translate(_INVISIBLE)
    return _WS.sub(" ", text).strip()


def dehyphenate_join(left: str, right: str) -> tuple[str, str]:
    """How to join two consecutive lines: returns (left_text, separator).

    A hyphen at the end of `left`, after a letter, followed by a lowercase continuation, is a line-break
    hyphen: drop it and join with no space. Otherwise join with one space.
    """
    if len(left) >= 2 and left.endswith("-") and left[-2].isalpha() and right[:1].islower():
        return left[:-1], ""
    return left, " "


# ---------------------------------------------------------------- rows


@dataclass
class Row:
    page: int
    text: str
    bbox: BBox
    size: float
    bold: bool
    line_no: int | None = None
    kind: str = "text"  # text | heading | title (set by the document builder)
    heading: object | None = None  # sections.HeadingInfo when kind == "heading"

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def y_centre(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2


@dataclass
class Layout:
    """Document-level geometry used by several stages."""

    body_left: float
    body_right: float
    body_size: float
    pitch: float  # median baseline-to-baseline distance of body text
    line_height: float


_DIGITS = re.compile(r"^\d{1,4}$")


def estimate_layout(pages: list[RawPage]) -> Layout:
    long_lines = [ln for p in pages for ln in p.lines if len(ln.text.strip()) >= 30]
    if not long_lines:
        long_lines = [ln for p in pages for ln in p.lines] or [RawLine(spans=[], bbox=(72, 72, 540, 84))]
    x0s = sorted(round(ln.bbox[0]) for ln in long_lines)
    x1s = sorted(round(ln.bbox[2]) for ln in long_lines)
    body_left = float(statistics.mode(x0s))
    body_right = float(x1s[int(0.9 * (len(x1s) - 1))])
    sizes = [round(ln.size * 2) / 2 for ln in long_lines if ln.spans]
    body_size = float(statistics.mode(sizes)) if sizes else 10.0
    heights = [ln.height for ln in long_lines if abs(ln.size - body_size) < 0.6]
    line_height = statistics.median(heights) if heights else body_size * 1.2
    pitches: list[float] = []
    for p in pages:
        body = sorted((ln for ln in p.lines if len(ln.text.strip()) >= 30 and abs(ln.size - body_size) < 0.6),
                      key=lambda ln: ln.bbox[1])
        for a, b in pairwise(body):
            d = b.bbox[1] - a.bbox[1]
            if 0.5 * line_height < d < 2.5 * line_height:
                pitches.append(d)
    pitch = statistics.median(pitches) if pitches else line_height * 1.1
    return Layout(body_left=body_left, body_right=body_right, body_size=body_size, pitch=pitch,
                  line_height=line_height)


# ---------------------------------------------------------------- 1. line numbers


@dataclass
class LineNumber:
    value: int
    y_centre: float
    raw: RawLine | None = None


def strip_line_numbers(page: RawPage, layout: Layout) -> tuple[list[RawLine], list[LineNumber]]:
    """Split a page's lines into (text lines, line numbers).

    Candidates are digit-only fragments entirely left of the text column (or right of it), within the outer
    20% of the page. They count as line numbers only if there are at least 3 and their values rise down the
    page (>= 80% of consecutive pairs increase).
    """
    cands: list[tuple[RawLine, int]] = []
    rest: list[RawLine] = []
    for ln in page.lines:
        t = ln.text.strip()
        x0, _, x1, _ = ln.bbox
        in_left = x1 < min(layout.body_left - 2, 0.2 * page.width)
        in_right = x0 > max(layout.body_right + 2, 0.8 * page.width)
        if _DIGITS.match(t) and (in_left or in_right):
            cands.append((ln, int(t)))
        else:
            rest.append(ln)
    if len(cands) < 3:
        return page.lines, []
    cands.sort(key=lambda c: c[0].y_centre)
    rising = sum(1 for a, b in pairwise(cands) if b[1] > a[1])
    if rising < 0.8 * (len(cands) - 1):
        return page.lines, []
    return rest, [LineNumber(value=n, y_centre=ln.y_centre, raw=ln) for ln, n in cands]


def validate_numbering(per_page: list[list[LineNumber]]) -> list[bool]:
    """Which pages' detected numbers are review line numbers. Review numbering is one sequence for the whole
    document: it starts near 1 within the first three pages and every later numbered page continues from the
    previous one. Anything else (e.g. a code listing's own line numbers in an appendix) is page text."""
    keep = [False] * len(per_page)
    first = next((i for i, nums in enumerate(per_page) if nums), None)
    if first is None or first > 2 or min(n.value for n in per_page[first]) > 5:
        return keep
    last = 0
    for i, nums in enumerate(per_page):
        if not nums:
            continue
        lo, hi = min(n.value for n in nums), max(n.value for n in nums)
        if last - 1 <= lo <= last + 80:
            keep[i] = True
            last = hi
    return keep


def assign_line_numbers(rows: list[Row], numbers: list[LineNumber], layout: Layout) -> None:
    """Each row takes the nearest number by vertical centre within 0.6 x line height; one number per row."""
    if not numbers:
        return
    tol = 0.6 * layout.line_height
    best: dict[int, tuple[float, int]] = {}  # number index -> (distance, row index)
    for ri, row in enumerate(rows):
        if row.kind == "title":
            continue
        d, ni = min((abs(n.y_centre - row.y_centre), i) for i, n in enumerate(numbers))
        if d <= max(tol, 0.6 * row.height) and (ni not in best or d < best[ni][0]):
            best[ni] = (d, ri)
    for ni, (_, ri) in best.items():
        rows[ri].line_no = numbers[ni].value


# ---------------------------------------------------------------- 2. rows


def _two_columns(lines: list[RawLine], width: float) -> bool:
    mid = width / 2
    long_ = [ln for ln in lines if len(ln.text.strip()) >= 25]
    if len(long_) < 10:
        return False
    lefts = [ln for ln in long_ if ln.bbox[2] < mid + 5]
    rights = [ln for ln in long_ if ln.bbox[0] > mid - 5]
    spanning = [ln for ln in long_ if ln not in lefts and ln not in rights]
    if len(lefts) < 5 or len(rights) < 5:
        return False
    if len(spanning) < 0.15 * len(long_):
        return True
    # Full-width material (a title block, a wide figure or its caption) above or below the columns does not make
    # the page one column: only full-width lines inside the band where both columns run count against it.
    top = max(min(ln.bbox[1] for ln in lefts), min(ln.bbox[1] for ln in rights))
    bottom = min(max(ln.bbox[3] for ln in lefts), max(ln.bbox[3] for ln in rights))
    inside = [ln for ln in spanning if ln.bbox[3] > top and ln.bbox[1] < bottom]
    in_band = [ln for ln in long_ if ln.bbox[3] > top and ln.bbox[1] < bottom]
    return bottom - top > 0 and len(inside) < 0.15 * max(len(in_band), 1)


def _merge(lines: list[RawLine], page: int) -> list[Row]:
    lines = sorted(lines, key=lambda ln: (ln.y_centre, ln.bbox[0]))
    groups: list[list[RawLine]] = []
    for ln in lines:
        placed = False
        for g in reversed(groups[-3:]):
            gy0 = min(m.bbox[1] for m in g)
            gy1 = max(m.bbox[3] for m in g)
            overlap = min(gy1, ln.bbox[3]) - max(gy0, ln.bbox[1])
            if overlap >= 0.5 * min(ln.height, gy1 - gy0) and not any(
                min(m.bbox[2], ln.bbox[2]) - max(m.bbox[0], ln.bbox[0]) > 1.0 for m in g
            ):
                g.append(ln)
                placed = True
                break
        if not placed:
            groups.append([ln])
    rows: list[Row] = []
    for g in groups:
        g.sort(key=lambda m: m.bbox[0])
        text = ""
        for i, m in enumerate(g):
            t = m.text
            if i and text and not text.endswith(" ") and not t.startswith(" "):
                gap = m.bbox[0] - g[i - 1].bbox[2]
                if gap > 0.15 * max(m.size, 1.0):
                    text += " "
            text += t
        text = normalise(text)
        if not text:
            continue
        main = max(g, key=lambda m: len(m.text.strip()))
        bbox = (min(m.bbox[0] for m in g), min(m.bbox[1] for m in g), max(m.bbox[2] for m in g),
                max(m.bbox[3] for m in g))
        # a row's size/bold is taken from its content, ignoring a leading section number fragment
        nchars = sum(len(m.text.strip()) for m in g) or 1
        bold = sum(len(m.text.strip()) for m in g if m.bold) >= 0.6 * nchars
        rows.append(Row(page=page, text=text, bbox=bbox, size=main.size, bold=bold))
    rows.sort(key=lambda r: (r.y0, r.x0))
    return rows


def merge_rows(lines: list[RawLine], page: RawPage) -> list[Row]:
    """Fragments on one baseline -> one row, in reading order. Two-column pages are read left then right."""
    if _two_columns(lines, page.width):
        mid = page.width / 2
        left = [ln for ln in lines if ln.bbox[0] < mid - 5 or ln.bbox[2] < mid + 5]
        right = [ln for ln in lines if ln not in left]
        spanning_top = [ln for ln in left if ln.bbox[2] > mid + 5]
        left = [ln for ln in left if ln not in spanning_top]
        return _merge(spanning_top, page.number) + _merge(left, page.number) + _merge(right, page.number)
    return _merge(lines, page.number)


# ---------------------------------------------------------------- 3. headers and footers

_PAGE_NUMBER = re.compile(r"^(?:page\s+)?(?:\d{1,4}|[ivxlc]{1,6})(?:\s*(?:/|of)\s*\d{1,4})?$", re.IGNORECASE)


def _band(row: Row, height: float) -> bool:
    return row.y1 < 0.08 * height or row.y0 > 0.92 * height


# Venue / submission notices in the header or footer band ("Submitted to ... Do not distribute.", "Preprint.
# Under review.", "Published at ... conference"). They differ between review copies and camera-ready versions,
# so they would act as a hidden decision marker, like line numbers: they never enter Document.text.
_VENUE_NOTICE = re.compile(
    r"\b(?:submitted to|under review|do not distribute|preprint|camera[- ]ready|accepted (?:at|to|for)|"
    r"published (?:at|in|as)|proceedings of|conference|workshop|symposium)\b", re.IGNORECASE)


def remove_headers_footers(pages_rows: list[list[Row]], heights: list[float]) -> list[list[Row]]:
    n = len(pages_rows)
    counts: dict[str, set[int]] = defaultdict(set)
    for pi, rows in enumerate(pages_rows):
        for r in rows:
            if _band(r, heights[pi]):
                counts[re.sub(r"\d+", "#", r.text.lower())].add(pi)
    repeated = {k for k, v in counts.items() if n >= 2 and len(v) >= max(2, 0.5 * n)}
    out: list[list[Row]] = []
    for pi, rows in enumerate(pages_rows):
        keep = []
        for r in rows:
            if _band(r, heights[pi]) and (
                _PAGE_NUMBER.match(r.text) or re.sub(r"\d+", "#", r.text.lower()) in repeated
                or (len(r.text) < 200 and _VENUE_NOTICE.search(r.text))
            ):
                continue
            keep.append(r)
        out.append(keep)
    return out


# ---------------------------------------------------------------- 4. paragraphs

_BULLET = re.compile(r"^[•◦▪▸►‣●○■□–·∙]\s?")
_CAPTION = re.compile(r"^(?:Table|Figure|Fig\.|Algorithm)\s*[A-Z]?\d+[a-z]?\s*[:.|]", re.IGNORECASE)


@dataclass
class Block:
    kind: str  # text | heading | title
    rows: list[Row] = field(default_factory=list)

    @property
    def page(self) -> int:
        return self.rows[0].page


def group_blocks(pages_rows: list[list[Row]], layout: Layout) -> list[Block]:
    """Rows -> blocks. A heading or title row is a block of its own; text rows split into paragraphs."""
    blocks: list[Block] = []
    for rows in pages_rows:
        cur: Block | None = None
        prev: Row | None = None
        for r in rows:
            if r.kind != "text":
                if cur and cur.kind == r.kind == "title":
                    cur.rows.append(r)
                else:
                    cur = Block(kind=r.kind, rows=[r])
                    blocks.append(cur)
                prev = r
                continue
            new = cur is None or cur.kind != "text" or prev is None
            if not new and prev is not None:
                pitch = r.y0 - prev.y0
                gap = r.y0 - prev.y1
                new = (
                    pitch > 1.2 * layout.pitch * max(1.0, max(r.size, prev.size) / layout.body_size)
                    or gap > 0.6 * layout.line_height
                    or pitch < -2  # moved up: next column or a float
                    or abs(r.size - prev.size) > 1.0
                    or bool(_BULLET.match(r.text))
                    or bool(_CAPTION.match(r.text))
                    or (8 < r.x0 - layout.body_left < 40 and abs(prev.x0 - layout.body_left) < 3
                        and prev.x1 < layout.body_right - 0.15 * (layout.body_right - layout.body_left))
                )
            if new:
                cur = Block(kind="text", rows=[r])
                blocks.append(cur)
            else:
                assert cur is not None
                cur.rows.append(r)
            prev = r
    return blocks


# ---------------------------------------------------------------- 5. sentences

_ABBREV = {
    "e.g", "i.e", "fig", "figs", "eq", "eqs", "sec", "secs", "tab", "al", "vs", "cf", "approx", "resp", "no",
    "nos", "ref", "refs", "dr", "mr", "mrs", "ms", "prof", "inc", "ltd", "co", "jr", "sr", "st", "vol", "pp",
    "ch", "chap", "ed", "eds", "est", "max", "min", "avg", "std", "var", "viz", "etc", "u.s", "e.t.c", "wrt",
    "w.r.t", "a.k.a", "ca", "thm", "lem", "def", "prop", "cor", "alg", "app", "appx", "dept", "univ",
}
_SENT_END = re.compile(r"[.!?]+[\"'”’)\]]*(?=\s+[\"'“‘(\[]?[A-Z0-9])")


def _is_abbreviation(text: str, dot: int) -> bool:
    """True if the "." at `dot` ends an abbreviation or an initial rather than a sentence."""
    m = re.search(r"(\S+)$", text[:dot])
    word = (m.group(1) if m else "").lower().lstrip("([\"'“‘").rstrip(".")
    if not word:
        return False
    return (
        word in _ABBREV
        or (len(word) == 1 and word.isalpha())
        or bool(re.fullmatch(r"(?:[a-z]\.)+[a-z]", word))  # e.g, i.e, u.s.a
    )


def split_sentences(text: str, base: int = 0) -> list[tuple[int, int]]:
    """Abbreviation-aware sentence spans (absolute offsets: `base` + index into `text`).

    A sentence ends at ".", "!" or "?" (plus closing quotes/brackets) followed by whitespace and an uppercase
    letter, digit or opening quote/bracket, unless the "." ends a known abbreviation ("e.g.", "Fig.",
    "et al.") or a single-letter initial. Decimals never split because they have no whitespace.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENT_END.finditer(text):
        if text[m.start()] == "." and m.group(0).rstrip("\"'”’)]") == "." and _is_abbreviation(text, m.start()):
            continue
        s = start + (len(text[start:]) - len(text[start:].lstrip()))
        if m.end() > s:
            spans.append((base + s, base + m.end()))
        start = m.end()
    tail = text[start:]
    if tail.strip():
        s = start + (len(tail) - len(tail.lstrip()))
        spans.append((base + s, base + len(text.rstrip())))
    return spans


# ---------------------------------------------------------------- text assembly


@dataclass
class BuiltLine:
    page: int
    line_no: int | None
    text: str
    bbox: BBox
    start: int
    end: int


@dataclass
class BuiltParagraph:
    page: int
    index: int
    start: int
    end: int
    sentences: list[tuple[int, int]]


@dataclass
class BuiltHeading:
    row: Row
    start: int
    end: int


@dataclass
class BuiltText:
    text: str
    lines: list[BuiltLine]
    paragraphs: list[BuiltParagraph]
    headings: list[BuiltHeading]
    title: str


def build_text(blocks: list[Block]) -> BuiltText:
    """Concatenate blocks into `Document.text` ("\\n\\n" between blocks, de-hyphenated line joins)."""
    parts: list[str] = []
    pos = 0
    lines: list[BuiltLine] = []
    paragraphs: list[BuiltParagraph] = []
    headings: list[BuiltHeading] = []
    title_rows: list[str] = []
    para_index: dict[int, int] = defaultdict(int)

    def emit(s: str) -> None:
        nonlocal pos
        parts.append(s)
        pos += len(s)

    for bi, block in enumerate(blocks):
        if bi:
            emit("\n\n")
        b_start = pos
        texts = [r.text for r in block.rows]
        for i, row in enumerate(block.rows):
            t = texts[i]
            sep = ""
            if i + 1 < len(block.rows):
                t, sep = dehyphenate_join(t, texts[i + 1])
            l_start = pos
            emit(t)
            lines.append(BuiltLine(page=row.page, line_no=row.line_no, text=t, bbox=row.bbox, start=l_start,
                                   end=pos))
            if sep:
                emit(sep)
        if block.kind == "text":
            para_index[block.page] += 1
            paragraphs.append(BuiltParagraph(page=block.page, index=para_index[block.page], start=b_start, end=pos,
                                             sentences=[]))
        elif block.kind == "heading":
            headings.append(BuiltHeading(row=block.rows[0], start=b_start, end=pos))
        elif block.kind == "title":
            title_rows += texts
    text = "".join(parts)
    for p in paragraphs:
        p.sentences = split_sentences(text[p.start : p.end], p.start)
    return BuiltText(text=text, lines=lines, paragraphs=paragraphs, headings=headings,
                     title=normalise(" ".join(title_rows)))


# ---------------------------------------------------------------- locating spans


class Locator:
    """Map char offsets of `Document.text` to page / paragraph / sentence / review lines / section."""

    def __init__(self, doc) -> None:  # doc: onus.schemas.document.Document
        self.doc = doc
        self._p_starts = [p.start for p in doc.paragraphs]
        self._l_starts = [ln.start for ln in doc.lines]

    def paragraph_at(self, offset: int):
        """The paragraph containing `offset` (None between paragraphs, e.g. inside a heading)."""
        i = bisect_right(self._p_starts, offset) - 1
        if i >= 0 and offset <= self.doc.paragraphs[i].end:
            return self.doc.paragraphs[i]
        return None

    def section_at(self, offset: int):
        best = None
        for s in self.doc.sections:
            if s.start <= offset < s.end and (best is None or s.level >= best.level):
                best = s
        return best

    def locate(self, start: int, end: int):
        """`Location` for [start, end), or None when the span is not inside a paragraph (e.g. a heading)."""
        from onus.schemas.result import Location

        p = self.paragraph_at(start)
        if p is None:
            return None
        sent = 1
        for k, (s, e) in enumerate(p.sentences, start=1):
            if s <= start < e or start < s:
                sent = k
                break
            sent = k
        nums: list[int] = []
        i = max(bisect_right(self._l_starts, start) - 1, 0)
        while i < len(self.doc.lines) and self.doc.lines[i].start < max(end, start + 1):
            ln = self.doc.lines[i]
            if ln.end > start and ln.line_no is not None:
                nums.append(ln.line_no)
            i += 1
        sec = self.section_at(start)
        label = None
        if sec is not None:
            label = f"{sec.number} {sec.title}" if sec.number else sec.title
        return Location(page=p.page, paragraph=p.index, sentence=sent, start=start, end=end,
                        line_start=min(nums) if nums else None, line_end=max(nums) if nums else None,
                        section=label)
