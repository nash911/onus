"""v1.1 ingest: IEEE lettered subsections ("A. Gait features") under Roman sections. Synthetic layouts only."""

from __future__ import annotations

from pathlib import Path

from onus.ingest import sections as S
from onus.ingest.document_builder import build_document
from tests.test_ingest_v101 import _body, _pdf


def test_lettered_subsection_needs_the_next_letter_and_a_short_title() -> None:
    h = S.ieee_subheading("A. Gait features", "A", "II")
    assert h and h.number == "II.A" and h.title == "Gait features" and h.level == 2 and h.roman
    assert S.ieee_subheading("B. Gait features", "A", "II") is None  # out of sequence
    assert S.ieee_subheading("A. Smith showed that this works.", "A", "II") is None  # a sentence
    assert S.ieee_subheading("A. Smith and colleagues trained the controller on many robots", "A", "II") is None
    assert S.ieee_subheading("A. 2010 results", "A", "II") is None


def test_ieee_paper_gets_lettered_subsections(tmp_path: Path) -> None:
    items = [("title", "Gait Generation for Synthetic Robots", 20.0, "tibo"), ("gap", "", 0, ""),
             ("text", "First Author and Second Author", 10.0, "tiro"), ("gap", "", 0, ""),
             ("text", "Abstract- By considering locomotion as a set of coordinated oscillations, a method", 9.0, "tibo"),
             *[("text", t, 9.0, "tiro") for _, t, _, _ in _body(5)], ("gap", "", 0, ""),
             ("text", "I. INTRODUCTION", 10.0, "tiro"), *_body(8), ("gap", "", 0, ""),
             ("text", "II. METHOD", 10.0, "tiro"), ("text", "A. Gait features", 10.0, "tiit"), *_body(6),
             ("gap", "", 0, ""), ("text", "B. Controller design", 10.0, "tiit"), *_body(6), ("gap", "", 0, ""),
             ("text", "III. RESULTS", 10.0, "tiro"), ("text", "A. Simulation results", 10.0, "tiit"), *_body(6),
             ("text", "C. Not in sequence", 10.0, "tiit"), *_body(4), ("gap", "", 0, ""),
             ("text", "IV. CONCLUSIONS", 10.0, "tiro"), *_body(6), ("gap", "", 0, ""),
             ("text", "REFERENCES", 10.0, "tiro"), ("text", "A. Author, A paper, 2010", 8.0, "tiro")]
    doc = build_document(_pdf(tmp_path / "ieee.pdf", items), paper_id="ieee", source="upload", var_dir=tmp_path)
    by_num = {s.number: s for s in doc.sections if s.number}
    assert by_num["II.A"].title == "Gait features" and by_num["II.A"].level == 2
    assert by_num["II.B"].title == "Controller design" and by_num["II.B"].role == "method"
    assert by_num["III.A"].role == "results"
    assert "III.C" not in by_num and not any(s.role == "appendix" for s in doc.sections)
    assert [s.number for s in doc.sections if s.level == 1 and s.number] == ["I", "II", "III", "IV"]
