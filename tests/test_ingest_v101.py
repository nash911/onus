"""v1.0.1 ingest fixes: "1. Introduction" headings, IEEE Roman headings and inline abstracts, an abstract with no
heading, a running label above the title, two columns under a full-width figure, and the opening-text fallback
for claim input. Synthetic layouts only (no real papers)."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from onus.claims.extract import select_input
from onus.ingest import lines as L
from onus.ingest import sections as S
from onus.ingest.document_builder import _mark_title, build_document
from onus.ingest.pdf import RawPage
from tests.test_ingest_lines import raw
from tests.test_ingest_parts import LAYOUT, _row

LONG = ("This sentence is long enough to count as body text for the layout estimator and it keeps going on for a "
        "while so that the paragraph is long.")


def test_numbered_heading_with_a_dot_is_a_heading() -> None:
    h = S.detect_heading(_row("1. Introduction"), LAYOUT, after_references=False)
    assert h and h.number == "1" and h.title == "Introduction" and h.level == 1
    h = S.detect_heading(_row("3.2. Main Results"), LAYOUT, after_references=False)
    assert h and h.number == "3.2" and h.level == 2
    assert S.detect_heading(_row("2. We show results. Then more text"), LAYOUT, after_references=False) is None
    assert S.detect_heading(_row("1. A bold list item that ends like a sentence."), LAYOUT,
                            after_references=False) is None
    assert S.detect_heading(_row("Figure 1. Overview of the method"), LAYOUT, after_references=False) is None


def test_ieee_roman_headings_at_body_size() -> None:
    for text, num, role in (("I. INTRODUCTION", "I", "introduction"), ("IV. EXPERIMENTAL RESULTS", "IV", "results"),
                            ("V. CONCLUSIONS", "V", "conclusion")):
        h = S.detect_heading(_row(text, size=10, bold=False), LAYOUT, after_references=False)
        assert h and h.number == num and h.roman
        sec = S.build_sections([(h, 0)], 100)[0]
        assert sec.role == role  # a Roman number is not an appendix letter
    assert S.detect_heading(_row("I. Introduction", size=10, bold=False), LAYOUT, after_references=False) is None
    assert S.detect_heading(_row("I. INTRODUCTION", size=10, bold=False), LAYOUT, after_references=True) is None
    assert S.detect_heading(_row("I. AM", size=10, bold=False), LAYOUT, after_references=False) is None


def test_two_columns_under_a_full_width_figure() -> None:
    wide = [raw(f"Full width figure caption line {i} that spans the whole page width", 50, 80 + 11 * i, width=510)
            for i in range(6)]
    left = [raw(f"Left column line number {i} with text", 50, 200 + 11 * i, width=240) for i in range(14)]
    right = [raw(f"Right column line number {i} with text", 320, 200 + 11 * i, width=240) for i in range(14)]
    page = RawPage(number=1, width=612, height=792, lines=wide + left + right)
    rows = L.merge_rows(page.lines, page)
    texts = [r.text for r in rows]
    assert not any("Left column" in t and "Right column" in t for t in texts)  # columns are not merged
    li = texts.index(next(t for t in texts if t.startswith("Left column line number 13")))
    ri = texts.index(next(t for t in texts if t.startswith("Right column line number 0")))
    assert li < ri


def test_running_label_is_not_the_title() -> None:
    rows = [L.Row(page=1, text="CHAPTER X", bbox=(108, 100, 300, 120), size=18.8, bold=False),
            L.Row(page=1, text="A Real Title About Robots", bbox=(108, 140, 400, 154), size=13.2, bold=True),
            L.Row(page=1, text="An Author", bbox=(108, 170, 300, 181), size=10.4, bold=False)]
    lay = L.Layout(body_left=108, body_right=504, body_size=10.4, pitch=11, line_height=11)
    _mark_title(rows, lay, 792)
    assert [r.kind for r in rows] == ["text", "title", "text"]


def _pdf(path: Path, items: list[tuple[str, str, float, str]]) -> Path:
    """(kind, text, size, font) per line; kind "gap" adds paragraph spacing."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    y = 80.0
    for kind, text, size, font in items:
        if kind == "gap":
            y += 8
            continue
        if y > 740:
            page = doc.new_page(width=612, height=792)
            y = 80.0
        page.insert_text((108, y), text, fontsize=size, fontname=font)
        y += size * 1.15
    doc.save(path)
    doc.close()
    return path


def _body(n: int) -> list[tuple[str, str, float, str]]:
    words = LONG.split()
    out = []
    for i in range(n):
        out.append(("text", " ".join(words[(i * 3) % 10 :][:14]) + " and more words here", 10.0, "tiro"))
    return out


def test_ieee_paper_gets_abstract_and_sections(tmp_path: Path) -> None:
    items = [("title", "Gait Generation for Synthetic Robots", 20.0, "tibo"), ("gap", "", 0, ""),
             ("text", "First Author and Second Author", 10.0, "tiro"), ("gap", "", 0, ""),
             ("text", "Abstract- By considering locomotion as a set of coordinated oscillations, a method", 9.0, "tibo"),
             *[("text", t, 9.0, "tiro") for _, t, _, _ in _body(5)], ("gap", "", 0, ""),
             ("text", "I. INTRODUCTION", 10.0, "tiro"), *_body(12), ("gap", "", 0, ""),
             ("text", "II. RESULTS", 10.0, "tiro"), *_body(12), ("gap", "", 0, ""),
             ("text", "III. CONCLUSIONS", 10.0, "tiro"), *_body(8), ("gap", "", 0, ""),
             ("text", "REFERENCES", 10.0, "tiro"), ("text", "[1] A. Author, A paper, 2010.", 8.0, "tiro")]
    doc = build_document(_pdf(tmp_path / "ieee.pdf", items), paper_id="ieee", source="upload", var_dir=tmp_path)
    roles = [s.role for s in doc.sections]
    assert roles[:4] == ["abstract", "introduction", "results", "conclusion"]
    assert doc.text[doc.sections[0].start :].startswith("Abstract")  # base-14 fonts print the dash as "·"
    assert any("inline 'Abstract'" in w for w in doc.warnings)
    inp = select_input(doc)
    assert not inp.fallback and "I INTRODUCTION" in " ".join(inp.sections).replace(".", "")


def test_unlabelled_abstract_and_fallback(tmp_path: Path) -> None:
    items = [("title", "A Distributed Controller for Synthetic Robots", 20.0, "tibo"), ("gap", "", 0, ""),
             ("text", "First Author, Second Author", 10.0, "tiro"), ("gap", "", 0, ""),
             *_body(6), ("gap", "", 0, ""),
             ("text", "1 Introduction", 12.0, "tibo"), *_body(10), ("gap", "", 0, "")]
    doc = build_document(_pdf(tmp_path / "ch.pdf", items), paper_id="ch", source="upload", var_dir=tmp_path)
    assert doc.sections[0].role == "abstract" and doc.sections[1].role == "introduction"
    assert any("opening paragraph" in w for w in doc.warnings)

    bare = [("title", "Notes Without Any Headings", 20.0, "tibo"), ("gap", "", 0, ""), *_body(10),
            ("gap", "", 0, ""), ("text", "Short line.", 10.0, "tiro"), ("gap", "", 0, ""), *_body(10)]
    doc = build_document(_pdf(tmp_path / "bare.pdf", bare), paper_id="bare", source="upload", var_dir=tmp_path)
    doc = doc.model_copy(update={"sections": []})
    inp = select_input(doc)
    assert inp.fallback and inp.text.strip()
