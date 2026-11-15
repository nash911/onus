"""PyMuPDF text extraction: spans with font size, bold flag and bbox, grouped into lines. (Phase 2)

This is the only module that talks to PyMuPDF's text API. Everything downstream works on `RawPage` /
`RawLine` objects, so unit tests can build pages by hand without a PDF.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

BBox = tuple[float, float, float, float]

_BOLD_FLAG = 16  # PyMuPDF span flag bit 4


@dataclass
class Span:
    text: str
    bbox: BBox
    size: float
    bold: bool
    font: str = ""


@dataclass
class RawLine:
    """One PyMuPDF line (a run of spans on one baseline). Several may later merge into one visual row."""

    spans: list[Span]
    bbox: BBox

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def size(self) -> float:
        """Font size of the line, weighted by characters (so a superscript does not count)."""
        total = sum(len(s.text.strip()) for s in self.spans)
        if not total:
            return max((s.size for s in self.spans), default=0.0)
        return sum(s.size * len(s.text.strip()) for s in self.spans) / total

    @property
    def bold(self) -> bool:
        chars = [(len(s.text.strip()), s.bold) for s in self.spans if s.text.strip()]
        if not chars:
            return False
        return sum(n for n, b in chars if b) >= 0.6 * sum(n for n, _ in chars)

    @property
    def y_centre(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


@dataclass
class Word:
    text: str
    bbox: BBox


@dataclass
class RawPage:
    number: int  # 1-based
    width: float
    height: float
    lines: list[RawLine]
    words: list[Word] = field(default_factory=list)
    bold_boxes: list[BBox] = field(default_factory=list)  # bboxes of bold spans (for table cells)
    table_boxes: list[BBox] = field(default_factory=list)  # grids PyMuPDF's find_tables() located


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _span(s: dict) -> Span:
    font = s.get("font", "") or ""
    bold = bool(s.get("flags", 0) & _BOLD_FLAG) or "bold" in font.lower() or font.endswith(("-Medi", "-Bold"))
    return Span(text=s["text"], bbox=tuple(s["bbox"]), size=float(s["size"]), bold=bold, font=font)


def _page(page: pymupdf.Page, find_tables: bool) -> RawPage:
    data = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    lines: list[RawLine] = []
    bold_boxes: list[BBox] = []
    for block in data["blocks"]:
        if block.get("type", 0) != 0:
            continue
        for ln in block["lines"]:
            spans = [_span(s) for s in ln["spans"] if s["text"]]
            if not spans or not "".join(s.text for s in spans).strip():
                continue
            lines.append(RawLine(spans=spans, bbox=tuple(ln["bbox"])))
            bold_boxes += [s.bbox for s in spans if s.bold and s.text.strip()]
    words = [Word(text=w[4], bbox=(w[0], w[1], w[2], w[3])) for w in page.get_text("words")]
    table_boxes: list[BBox] = []
    if find_tables:
        try:
            table_boxes = [tuple(t.bbox) for t in page.find_tables().tables]
        except Exception:  # noqa: BLE001 - table finding is best effort
            table_boxes = []
    return RawPage(number=page.number + 1, width=page.rect.width, height=page.rect.height, lines=lines,
                   words=words, bold_boxes=bold_boxes, table_boxes=table_boxes)


def extract_pages(pdf_path: Path, *, find_tables: bool = True) -> list[RawPage]:
    with pymupdf.open(pdf_path) as doc:
        return [_page(p, find_tables) for p in doc]


def pdf_metadata_title(pdf_path: Path) -> str:
    with pymupdf.open(pdf_path) as doc:
        return (doc.metadata or {}).get("title", "") or ""
