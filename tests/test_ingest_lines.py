"""Line numbers, rows, headers/footers, de-hyphenation, paragraphs and sentences (BUILD_PLAN Phase 2)."""

from __future__ import annotations

from onus.ingest import lines as L
from onus.ingest.pdf import RawLine, RawPage, Span


def raw(text: str, x0: float, y0: float, size: float = 10.0, bold: bool = False, width: float | None = None) -> RawLine:
    w = width if width is not None else 4.8 * len(text)
    bbox = (x0, y0, x0 + w, y0 + size * 1.1)
    return RawLine(spans=[Span(text=text, bbox=bbox, size=size, bold=bold)], bbox=bbox)


BODY = "x" * 60


def numbered_page(number: int, first: int, n: int, x_num: float = 92.0, pitch: float = 11.0) -> RawPage:
    lines = []
    for i in range(n):
        y = 100 + i * pitch
        lines.append(raw(f"Body line {i} " + BODY, 108, y, width=396))
        lines.append(raw(str(first + i), x_num, y + 1.5, size=6.0, width=6))
    return RawPage(number=number, width=612, height=792, lines=lines)


def layout() -> L.Layout:
    return L.Layout(body_left=108, body_right=504, body_size=10, pitch=11, line_height=11)


def test_normalise_ligatures_and_soft_hyphen() -> None:
    assert L.normalise("e\ufb03cient  \ufb01t\u00adted") == "efficient fitted"


def test_dehyphenate_only_before_lowercase() -> None:
    assert L.dehyphenate_join("effi-", "cient") == ("effi", "")
    assert L.dehyphenate_join("Widget-", "A works") == ("Widget-", " ")
    assert L.dehyphenate_join("x 2-", "3") == ("x 2-", " ")
    assert L.dehyphenate_join("plain", "next") == ("plain", " ")


def test_line_numbers_detected_and_mapped() -> None:
    page = numbered_page(1, 1, 8)
    rest, nums = L.strip_line_numbers(page, layout())
    assert [n.value for n in nums] == list(range(1, 9))
    assert all(not ln.text.strip().isdigit() for ln in rest)
    rows = L.merge_rows(rest, page)
    L.assign_line_numbers(rows, nums, layout())
    assert [r.line_no for r in rows] == list(range(1, 9))
    assert all(not r.text[0].isdigit() for r in rows)  # numbers removed from the text


def test_line_number_mapping_tolerates_offset_but_not_far_rows() -> None:
    page = numbered_page(1, 1, 3)
    rest, nums = L.strip_line_numbers(page, layout())
    rows = L.merge_rows(rest, page)
    rows.append(L.Row(page=1, text="far away caption", bbox=(108, 600, 300, 611), size=9, bold=False))
    L.assign_line_numbers(rows, nums, layout())
    assert rows[-1].line_no is None
    assert [r.line_no for r in rows[:3]] == [1, 2, 3]


def test_section_number_in_body_column_is_not_a_line_number() -> None:
    lines = [raw("1", 108, 100, size=12, bold=True, width=6), raw("Introduction", 126, 100, size=12, bold=True)]
    lines += [raw(f"Body {i} " + BODY, 108, 120 + 11 * i, width=396) for i in range(5)]
    page = RawPage(number=1, width=612, height=792, lines=lines)
    rest, nums = L.strip_line_numbers(page, layout())
    assert nums == []
    rows = L.merge_rows(rest, page)
    assert rows[0].text == "1 Introduction"


def test_numbers_not_rising_are_not_line_numbers() -> None:
    page = numbered_page(1, 1, 6)
    for k, ln in enumerate([ln for ln in page.lines if ln.text.isdigit()]):
        ln.spans[0].text = str([5, 1, 4, 2, 6, 3][k])
    _, nums = L.strip_line_numbers(page, layout())
    assert nums == []


def test_validate_numbering_rejects_listing_numbers_late_in_document() -> None:
    def nums(*vals: int) -> list[L.LineNumber]:
        return [L.LineNumber(value=v, y_centre=100 + i) for i, v in enumerate(vals)]

    # review numbering: starts at 1 on page 1 and continues
    assert L.validate_numbering([nums(1, 2, 3), nums(4, 5), [], nums(6, 7)]) == [True, True, False, True]
    # a code listing on page 9 numbered 10..: not review numbering
    assert L.validate_numbering([[]] * 8 + [nums(10, 11, 12)]) == [False] * 9
    # a page whose numbers jump backwards is dropped
    assert L.validate_numbering([nums(1, 2, 3), nums(1, 2, 3)]) == [True, False]


def test_merge_rows_joins_fragments_on_one_baseline() -> None:
    page = RawPage(number=1, width=612, height=792, lines=[raw("Left part", 108, 100), raw("right part", 200, 100.5)])
    rows = L.merge_rows(page.lines, page)
    assert len(rows) == 1 and rows[0].text == "Left part right part"


def test_two_column_page_reads_left_column_first() -> None:
    left = [raw(f"Left column line number {i} with text", 50, 100 + 11 * i, width=240) for i in range(12)]
    right = [raw(f"Right column line number {i} with text", 320, 100 + 11 * i, width=240) for i in range(12)]
    page = RawPage(number=1, width=612, height=792, lines=left + right)
    rows = L.merge_rows(page.lines, page)
    assert rows[0].text.startswith("Left column line number 0")
    assert rows[11].text.startswith("Left column line number 11")
    assert rows[12].text.startswith("Right column line number 0")


def test_headers_footers_and_page_numbers_removed() -> None:
    pages = []
    for p in range(4):
        pages.append([
            L.Row(page=p + 1, text="Running header of the paper", bbox=(108, 30, 400, 40), size=9, bold=False),
            L.Row(page=p + 1, text="Body text " + BODY, bbox=(108, 100, 504, 111), size=10, bold=False),
            L.Row(page=p + 1, text=str(p + 1), bbox=(300, 750, 306, 760), size=10, bold=False),
        ])
    pages[0].append(L.Row(page=1, text="Submitted to a Conference. Do not distribute.", bbox=(108, 735, 400, 745),
                          size=9, bold=False))
    out = L.remove_headers_footers(pages, [792.0] * 4)
    assert all([r.text[:4] for r in rows] == ["Body"] for rows in out)


def _rows(texts: list[tuple[str, float]], x0: float = 108) -> list[L.Row]:
    return [L.Row(page=1, text=t, bbox=(x0, y, 504, y + 10), size=10, bold=False) for t, y in texts]


def test_paragraphs_split_on_gap_bullets_and_headings() -> None:
    rows = _rows([("First paragraph line one", 100), ("line two.", 111), ("Second paragraph", 130),
                  ("• a bullet item", 141), ("• another bullet", 152)])
    head = L.Row(page=1, text="2 Method", bbox=(108, 170, 200, 182), size=12, bold=True, kind="heading")
    rows.append(head)
    rows += _rows([("After heading.", 190)])
    blocks = L.group_blocks([rows], layout())
    kinds = [(b.kind, len(b.rows)) for b in blocks]
    assert kinds == [("text", 2), ("text", 1), ("text", 1), ("text", 1), ("heading", 1), ("text", 1)]
    built = L.build_text(blocks)
    assert [p.index for p in built.paragraphs] == [1, 2, 3, 4, 5]  # headings are not paragraphs
    assert built.text[built.paragraphs[0].start : built.paragraphs[0].end] == "First paragraph line one line two."


def test_build_text_dehyphenates_and_keeps_offsets() -> None:
    rows = _rows([("The method is effi-", 100), ("cient and simple.", 111)])
    built = L.build_text(L.group_blocks([rows], layout()))
    assert built.text == "The method is efficient and simple."
    for ln in built.lines:
        assert built.text[ln.start : ln.end] == ln.text
    assert built.lines[0].text == "The method is effi"


def test_sentence_splitter_abbreviations_and_decimals() -> None:
    t = ("We use e.g. Fig. 3 and Smith et al. (2023) reported 0.5 gains. It works! "
         "Does it? A. B. Jones agrees. See Tab. 2 and Eq. 4 for i.e. details. Done")
    sents = [t[a:b] for a, b in L.split_sentences(t)]
    assert sents == [
        "We use e.g. Fig. 3 and Smith et al. (2023) reported 0.5 gains.",
        "It works!",
        "Does it?",
        "A. B. Jones agrees.",
        "See Tab. 2 and Eq. 4 for i.e. details.",
        "Done",
    ]


def test_sentence_offsets_are_absolute() -> None:
    t = "One. Two."
    assert L.split_sentences(t, base=100) == [(100, 104), (105, 109)]
