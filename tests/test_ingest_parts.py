"""Cell parsing, table grids, captions, mentions, sections and checklist parsing (BUILD_PLAN Phase 2)."""

from __future__ import annotations

import pytest

from onus.ingest import checklist as C
from onus.ingest import lines as L
from onus.ingest import refs as R
from onus.ingest import sections as S
from onus.ingest import tables as T
from onus.ingest.pdf import Word
from onus.schemas.document import Figure, Section, Table

# ---------------------------------------------------------------- cells


@pytest.mark.parametrize(
    ("raw", "value", "spread", "percent"),
    [
        ("0.81 ± 0.02", 0.81, 0.02, False),
        ("0.81±0.02", 0.81, 0.02, False),
        ("45.2%", 45.2, None, True),
        ("45.2 ± 1.1%", 45.2, 1.1, True),
        ("12.3 (11.9–12.7)", 12.3, 0.4, False),
        ("0.81 (0.02)", 0.81, 0.02, False),
        ("−3.5", -3.5, None, False),
        ("1,476", 1476.0, None, False),
        ("1.57e-11", 1.57e-11, None, False),
        ("340M", 340.0, None, False),
        ("0.75*", 0.75, None, False),
        ("~12", 12.0, None, False),
        ("Ours", None, None, False),
        ("1 CommandA", None, None, False),
        ("–", None, None, False),
        ("NA", None, None, False),
    ],
)
def test_parse_cell(raw: str, value: float | None, spread: float | None, percent: bool) -> None:
    c = T.parse_cell(raw)
    assert c.value == (pytest.approx(value) if value is not None else None)
    assert c.spread == (pytest.approx(spread) if spread is not None else None)
    assert c.percent is percent
    assert c.raw == L.normalise(raw)


def _words(rows: list[list[tuple[str, float]]], y0: float = 100, pitch: float = 10) -> list[Word]:
    out = []
    for i, row in enumerate(rows):
        for text, x in row:
            w = 4.5 * len(text)
            out.append(Word(text=text, bbox=(x, y0 + i * pitch, x + w, y0 + i * pitch + 8)))
    return out


def test_grid_from_words_columns_header_and_bold() -> None:
    words = _words([
        [("Method", 100), ("Acc", 200), ("(%)", 216), ("F1", 300)],
        [("Baseline", 100), ("81.0", 200), ("±", 220), ("0.5", 227), ("0.70", 300)],
        [("Ours", 100), ("85.2", 200), ("±", 220), ("0.4", 227), ("0.74", 300)],
    ])
    bold = [w.bbox for w in words if w.text in ("85.2", "±", "0.4")][-3:]
    grid, header_rows = T.grid_from_words(words, bold)
    assert header_rows == 1
    assert [c.raw for c in grid[0]] == ["Method", "Acc (%)", "F1"]
    assert [c.raw for c in grid[2]] == ["Ours", "85.2 ± 0.4", "0.74"]
    assert grid[2][1].value == 85.2 and grid[2][1].spread == pytest.approx(0.4) and grid[2][1].bold
    assert not grid[1][1].bold


def test_grid_one_run_together_row_does_not_merge_columns() -> None:
    # PyMuPDF sometimes reports two adjacent cells of one row as one run of words with a small gap
    rows = [[("Row", 100), ("1.00", 200), ("2.00", 223)] for _ in range(5)]  # gap 5 pt: two cells
    rows.append([("Bad", 100), ("1.00", 200), ("2.00", 222)])  # gap 4 pt: one run of words
    grid, _ = T.grid_from_words(_words(rows))
    assert all(len(r) == 3 for r in grid)
    assert [c.raw for c in grid[-1]] == ["Bad", "1.00", "2.00"]


def test_caption_of() -> None:
    assert T.caption_of("Table 2: Results") == ("table", "Table 2")
    assert T.caption_of("TABLE IV. Results") == ("table", "Table IV")
    assert T.caption_of("Figure 3: A plot") == ("figure", "Figure 3")
    assert T.caption_of("Fig. 3. A plot") == ("figure", "Figure 3")
    assert T.caption_of("Table 2 shows that") is None
    assert T.caption_of("As shown in Figure 3:") is None


# ---------------------------------------------------------------- mentions


def _doc_objects() -> tuple[list[Table], list[Figure], list[Section]]:
    tables = [Table(id=f"Table {i}", caption="", page=1, bbox=(0, 0, 1, 1), header_rows=1, parsed=True)
              for i in (1, 2, 3, 4)]
    figures = [Figure(id="Figure 3", caption="", page=1), Figure(id="Figure 4", caption="", page=1)]
    sections = [Section(id="sec:3.1", title="X", number="3.1", role="method", start=0, end=1, level=2),
                Section(id="sec:B.2", title="Y", number="B.2", role="appendix", start=0, end=1, level=2)]
    return tables, figures, sections


def test_mentions_lists_ranges_and_resolution() -> None:
    text = ("See Tables 2 and 3, Tables 1–4, Fig. 3a and Figs. 4, 5. Section 3.1 and Sec. 9 and "
            "Appendix B.2 and Eq. (5).")
    tables, figures, sections = _doc_objects()
    ms = R.find_mentions(text, tables, figures, sections, equation_tags={"5"})
    got = [(m.kind, m.label, m.resolved_id) for m in ms]
    assert ("table", "Table 2", "Table 2") in got and ("table", "Table 3", "Table 3") in got
    assert [g for g in got if g[0] == "table"].count(("table", "Table 2", "Table 2")) == 2  # 2 and 3, and 1–4
    assert sum(1 for g in got if g[0] == "table") == 6
    assert ("figure", "Figure 3a", "Figure 3") in got
    assert ("figure", "Figure 5", None) in got  # dangling
    assert ("section", "Section 3.1", "sec:3.1") in got
    assert ("section", "Section 9", None) in got
    assert ("appendix", "Appendix B.2", "sec:B.2") in got
    assert ("equation", "Equation (5)", "eq:5") in got
    first = next(m for m in ms if m.label == "Table 2")
    assert text[first.start : first.end] == "Tables 2"


def test_caption_opening_is_not_a_mention() -> None:
    text = "Table 1: Main results. Table 1 shows it."
    tables, figures, sections = _doc_objects()
    ms = R.find_mentions(text, tables, figures, sections, caption_starts={0})
    assert len(ms) == 1 and ms[0].start == text.index("Table 1 shows")


# ---------------------------------------------------------------- sections


def _row(text: str, size: float = 12, bold: bool = True) -> L.Row:
    return L.Row(page=1, text=text, bbox=(108, 100, 300, 112), size=size, bold=bold)


LAYOUT = L.Layout(body_left=108, body_right=504, body_size=10, pitch=11, line_height=11)


def test_detect_heading() -> None:
    h = S.detect_heading(_row("3.2 Main Results"), LAYOUT, after_references=False)
    assert h and h.number == "3.2" and h.level == 2 and h.title == "Main Results"
    assert S.detect_heading(_row("Abstract"), LAYOUT, after_references=False).known_role == "abstract"
    assert S.detect_heading(_row("Agents4Science Paper Checklist"), LAYOUT, after_references=False).known_role == "checklist"
    assert S.detect_heading(_row("3.2 Main Results", size=10, bold=False), LAYOUT, after_references=False) is None  # not emphasised
    assert S.detect_heading(_row("We show results. Then more text"), LAYOUT, after_references=False) is None  # a sentence
    assert S.detect_heading(_row("A Proofs"), LAYOUT, after_references=False) is None  # letters only after refs
    h = S.detect_heading(_row("A Proofs"), LAYOUT, after_references=True)
    assert h and h.number == "A" and h.known_role == "appendix"
    assert S.detect_heading(_row("12 0.45 0.33"), LAYOUT, after_references=False) is None


def test_build_sections_roles_and_ends() -> None:
    hs = [
        (S.HeadingInfo(None, "Abstract", 1, "abstract"), 0),
        (S.HeadingInfo("1", "Introduction", 1), 10),
        (S.HeadingInfo("2", "Our Approach", 1), 20),
        (S.HeadingInfo("2.1", "Training details", 2), 25),
        (S.HeadingInfo("3", "Experiments", 1), 30),
        (S.HeadingInfo("3.1", "Main results", 2), 35),
        (S.HeadingInfo("4", "Discussion and Limitations", 1), 40),
        (S.HeadingInfo(None, "References", 1, "references"), 50),
        (S.HeadingInfo("A", "Extra Tables", 1, "appendix"), 60),
        (S.HeadingInfo("A.1", "More results", 2), 65),
        (S.HeadingInfo(None, "Agents4Science Paper Checklist", 1, "checklist"), 70),
    ]
    secs = S.build_sections(hs, 80)
    roles = {s.id: s.role for s in secs}
    assert roles == {
        "sec:abstract": "abstract", "sec:1": "introduction", "sec:2": "method", "sec:2.1": "method",
        "sec:3": "experiments", "sec:3.1": "results", "sec:4": "limitations", "sec:references": "references",
        "sec:A": "appendix", "sec:A.1": "appendix", "sec:agents4science-paper-checklist": "checklist",
    }
    ends = {s.id: s.end for s in secs}
    assert ends["sec:2"] == 30 and ends["sec:2.1"] == 30 and ends["sec:A"] == 70


# ---------------------------------------------------------------- checklist

CHECKLIST = (
    "Agents4Science Paper Checklist\n\n"
    "1. Claims Question: Do the main claims made in the abstract and introduction accurately reflect the paper? "
    "Answer: [Yes] Justification: The abstract matches the results. Guidelines: • The answer NA means that...\n\n"
    "2. Limitations Question: Does the paper discuss the limitations? Answer: [No] Justification: We do not. "
    "Guidelines: • blah\n\n"
    "3. Experiment statistical significance Question: Does the paper report error bars? Answer: [NA] "
    "Justification: Not applicable here.\n\n"
    "4. Open access to data and code Answer: [Yes] Justification: Code is in the supplement."
)


def _checklist_section(text: str) -> list[Section]:
    return [Section(id="sec:c", title="Agents4Science Paper Checklist", role="checklist", start=0, end=len(text),
                    level=1)]


def test_parse_checklist_items_keys_answers_and_drops_guidelines() -> None:
    items = C.parse_checklist(CHECKLIST, _checklist_section(CHECKLIST))
    assert [(i.key, i.answer) for i in items] == [
        ("claims", "Yes"), ("limitations", "No"), ("error_bars", "NA"), ("open_access", "Yes")]
    assert items[0].question.startswith("Do the main claims")
    assert items[0].justification == "The abstract matches the results."
    assert "Guidelines" not in items[1].justification
    assert items[3].question == ""  # variant without a Question line
    assert CHECKLIST[items[0].start : items[0].start + 9] == "1. Claims"


@pytest.mark.parametrize(
    ("title", "key"),
    [("Claims", "claims"), ("Theory assumptions and proofs", "theory"), ("Theory", "theory"),
     ("Experimental result reproducibility", "reproducibility"), ("Reproducibility", "reproducibility"),
     ("Experimental setting/details", "experimental_details"), ("Experiments compute resources", "compute"),
     ("Code of ethics", "ethics"), ("Broader impacts", "broader_impacts"), ("Safeguards", "safeguards"),
     ("Something New", "something_new")],
)
def test_checklist_keys_by_title(title: str, key: str) -> None:
    assert C.key_for(title) == key


def test_parse_ai_checklist_prefers_answered_copy() -> None:
    text = ("Agents4Science AI Involvement Checklist\n\n1. Hypothesis development: Hypothesis development "
            "includes the process. Answer: [C] Explanation: Mostly AI.\n\n2. Writing: This includes writing. "
            "Answer: [D] Explanation: All AI.")
    template = "Agents4Science AI Involvement Checklist\n\n1. Hypothesis development: what it means.\n\n"
    full = template + text
    secs = [Section(id="a", title="x", role="ai_checklist", start=0, end=len(template), level=1),
            Section(id="b", title="x", role="ai_checklist", start=len(template), end=len(full), level=1)]
    items = C.parse_ai_checklist(full, secs)
    assert [(i["index"], i["answer"]) for i in items] == [(1, "C"), (2, "D")]
    assert items[0]["explanation"] == "Mostly AI."
