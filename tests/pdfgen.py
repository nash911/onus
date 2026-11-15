"""Build small synthetic PDFs with PyMuPDF for ingest tests (no real papers involved)."""

from __future__ import annotations

from pathlib import Path

import pymupdf

BODY_X = 108.0
LINE_PITCH = 11.0


def make_paper(path: Path, pages: list[list[tuple[str, str]]], *, line_numbers: bool,
               footer: str | None = None) -> Path:
    """`pages`: per page, a list of (kind, text) where kind is "title", "heading", "text", "gap",
    "caption" or "row" (a table row; cells separated by "|"). Text lines are placed one per line; "gap"
    adds paragraph spacing. With `line_numbers`, every non-gap line gets a review line number in the left
    margin (numbers continue across pages)."""
    doc = pymupdf.open()
    n = 0
    for items in pages:
        page = doc.new_page(width=612, height=792)
        y = 90.0
        for kind, text in items:
            if kind == "gap":
                y += 6
                continue
            size, font = 10.0, "tiro"
            if kind == "title":
                size, font = 17.0, "tibo"
            elif kind == "heading":
                size, font = 12.0, "tibo"
            elif kind == "caption":
                size = 9.0
            if kind == "row":
                for i, cell in enumerate(text.split("|")):
                    page.insert_text((BODY_X + 20 + 90 * i, y), cell.strip(), fontsize=9.0, fontname="tiro")
            else:
                page.insert_text((BODY_X, y), text, fontsize=size, fontname=font)
            if line_numbers and kind != "title":
                n += 1
                page.insert_text((92.0, y + 1), str(n), fontsize=6.0, fontname="helv")
            y += LINE_PITCH if kind not in ("title", "heading") else 18
        if footer:
            page.insert_text((BODY_X, 760), footer, fontsize=9.0, fontname="tiro")
        page.insert_text((303, 775), str(page.number + 1), fontsize=10.0, fontname="tiro")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


LONG = "This sentence is long enough to count as a body line for the layout estimator here."


def sample_pages() -> list[list[tuple[str, str]]]:
    return [
        [
            ("title", "A Synthetic Paper About Widgets"),
            ("gap", ""),
            ("heading", "Abstract"),
            ("text", "We study widgets and show they improve accuracy by 4.2 points on two"),
            ("text", "benchmarks, e.g. Widget-A and Widget-B. Results are in Table 1 and Fig. 2."),
            ("text", "Our method is effi-"),
            ("text", "cient and simple. " + "It needs no tuning at all, see Sec. 3 for details."),
            ("gap", ""),
            ("heading", "1 Introduction"),
            ("text", LONG),
            ("text", "Widgets are everywhere. Smith et al. (2020) studied them in 0.5 s per item."),
            ("gap", ""),
            ("text", LONG),
            ("text", "A second paragraph starts here and ends with a question?"),
        ],
        [
            ("heading", "2 Method"),
            ("text", LONG),
            ("text", "We describe the method in detail; Tables 1 and 2 summarise it, and Eq. 3 too."),
            ("gap", ""),
            ("caption", "Table 1: Main results on two benchmarks."),
            ("gap", ""),
            ("row", "Method|Acc (%)|F1"),
            ("row", "Baseline|81.0 ± 0.5|0.70"),
            ("row", "Ours|85.2 ± 0.4|0.74"),
            ("gap", ""),
            ("heading", "3 Limitations"),
            ("text", "Our main limitation is that we only test two benchmarks and one seed."),
            ("gap", ""),
            ("heading", "References"),
            ("text", "[1] A. Smith. Widgets. 2020."),
        ],
    ]
