"""`Document`: the output of ingest (BUILD_PLAN §5.2).

Review line numbers are citation metadata only (`Line.line_no`). They are stripped from `text`, and
nothing downstream may read whether a paper has them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

BBox = tuple[float, float, float, float]

SectionRole = Literal[
    "abstract", "introduction", "method", "experiments", "results", "discussion", "limitations",
    "conclusion", "related_work", "references", "appendix", "checklist", "ai_checklist", "other",
]


class Line(BaseModel):
    page: int  # 1-based
    line_no: int | None = None  # review-template line number, if present
    text: str  # normalised, line numbers removed
    bbox: BBox
    start: int  # char offsets into Document.text
    end: int


class Paragraph(BaseModel):
    page: int  # 1-based
    index: int  # 1-based within the page, in reading order
    start: int  # char offsets into Document.text
    end: int
    sentences: list[tuple[int, int]] = Field(default_factory=list)  # char offsets of each sentence


class Section(BaseModel):
    id: str
    title: str
    number: str | None = None
    role: SectionRole
    start: int
    end: int
    level: int


class Cell(BaseModel):
    raw: str
    value: float | None = None  # "0.81 ± 0.02" -> 0.81, 0.02
    spread: float | None = None
    percent: bool = False
    bold: bool = False


class Table(BaseModel):
    id: str  # "Table 2" or "Table ?3" when uncaptioned
    caption: str
    page: int
    bbox: BBox
    header_rows: int
    rows: list[list[Cell]] = Field(default_factory=list)
    parsed: bool  # False = caption only, grid not extracted


class Figure(BaseModel):
    id: str
    caption: str
    page: int


class RefMention(BaseModel):
    kind: Literal["table", "figure", "section", "appendix", "equation"]
    label: str
    start: int
    end: int
    resolved_id: str | None = None


class ChecklistItem(BaseModel):
    # normalised key: claims, limitations, theory, reproducibility, open_access,
    # experimental_details, error_bars, compute, ethics, broader_impacts, ...
    key: str
    title: str
    question: str
    answer: str
    justification: str
    start: int
    end: int


class CodeIndex(BaseModel):
    present: bool = False
    root: str | None = None
    files: list[dict] = Field(default_factory=list)  # {path, ext, lines, bytes, is_source}
    languages: dict[str, int] = Field(default_factory=dict)
    entry_candidates: list[str] = Field(default_factory=list)
    dependency_files: list[str] = Field(default_factory=list)
    repo_urls_in_paper: list[str] = Field(default_factory=list)


class Document(BaseModel):
    paper_id: str
    source: str
    pdf_sha256: str
    title: str
    pages: int
    text: str
    lines: list[Line] = Field(default_factory=list)
    paragraphs: list[Paragraph] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    figures: list[Figure] = Field(default_factory=list)
    mentions: list[RefMention] = Field(default_factory=list)
    checklist: list[ChecklistItem] = Field(default_factory=list)
    ai_checklist: list[dict] = Field(default_factory=list)
    limitations_spans: list[tuple[int, int]] = Field(default_factory=list)
    code: CodeIndex = Field(default_factory=CodeIndex)
    warnings: list[str] = Field(default_factory=list)
