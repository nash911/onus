"""Table grids, cell parsing and captions. (Phase 2)

PyMuPDF's `find_tables()` is used only to *locate* grids. Its cell text is unreliable on booktabs tables
(rows merge, decimal points split off), so the grid is rebuilt from the words inside the located region:
rows by vertical centre, cells by horizontal gaps, columns by merging the cell intervals of the body rows.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from onus.ingest.lines import normalise
from onus.ingest.pdf import BBox, RawPage, Word
from onus.schemas.document import Cell, Table

# ---------------------------------------------------------------- cells

_NUM = re.compile(r"[-+−–]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+|\.\d+)(?:[eE][-+]?\d+)?")
_PREFIX = re.compile(r"^(?:[~≈<>≤≥$€£]|\\sim)?\s*")
_UNIT_TAIL = re.compile(r"^[%a-zA-Z×xµ°/]{0,4}\*{0,3}†?‡?$")


def _num(s: str) -> float:
    s = s.replace(",", "").replace("−", "-").replace("–", "-")
    return float(s)


def parse_cell(raw: str, bold: bool = False) -> Cell:
    """`"0.81 ± 0.02"` -> value 0.81, spread 0.02; `"12.3 (11.9–12.7)"` -> 12.3, spread 0.4 (CI half-width);
    `"0.81 (0.02)"` -> 0.81, spread 0.02; `"45.2%"` -> 45.2, percent. Text cells get value None."""
    t = normalise(raw)
    percent = "%" in t
    cell = Cell(raw=t, value=None, spread=None, percent=percent, bold=bold)
    rest = _PREFIX.sub("", t, count=1)
    m = _NUM.match(rest)
    if not m or not re.search(r"\d", m.group(0)):
        return cell
    value = _num(m.group(0))
    rest = rest[m.end() :].strip()
    rest = re.sub(r"^%", "", rest).strip()
    spread: float | None = None
    pm = re.match(r"^(?:±|\+/-|\+-|\\pm)\s*(" + _NUM.pattern + r")\s*%?", rest)
    if pm:
        spread = abs(_num(pm.group(1)))
        rest = rest[pm.end() :].strip()
    ci = re.match(r"^[(\[]\s*(" + _NUM.pattern + r")\s*(?:[–,;-]|to)\s*(" + _NUM.pattern + r")\s*%?\s*[)\]]", rest)
    if ci:
        lo, hi = _num(ci.group(1)), _num(ci.group(2))
        if lo <= value <= hi and spread is None:
            spread = (hi - lo) / 2
        rest = rest[ci.end() :].strip()
    else:
        sd = re.match(r"^[(\[]\s*(?:±\s*)?(" + _NUM.pattern + r")\s*%?\s*[)\]]", rest)
        if sd:
            if spread is None:
                spread = abs(_num(sd.group(1)))
            rest = rest[sd.end() :].strip()
    if not _UNIT_TAIL.match(rest):
        return cell  # "1 CommandA", "3 layers deep": a label that starts with a number
    cell.value = value
    cell.spread = round(spread, 10) if spread is not None else None
    return cell


# ---------------------------------------------------------------- captions


@dataclass
class Caption:
    kind: str  # table | figure
    id: str  # "Table 2", "Figure 3"
    text: str
    page: int
    bbox: BBox


_TABLE_CAP = re.compile(r"^(?:Table|TABLE|Tab\.)\s*(?P<n>\d{1,3}|[A-Z]\.?\d{1,3}|[IVX]{1,6})\s*[:.|]")
_FIG_CAP = re.compile(r"^(?:Figure|FIGURE|Fig\.)\s*(?P<n>\d{1,3}|[A-Z]\.?\d{1,3})[a-z]?\s*[:.|]")


def caption_of(text: str) -> tuple[str, str] | None:
    """(kind, id) if `text` starts like a table or figure caption."""
    if m := _TABLE_CAP.match(text):
        return "table", f"Table {m.group('n')}"
    if m := _FIG_CAP.match(text):
        return "figure", f"Figure {m.group('n')}"
    return None


# ---------------------------------------------------------------- grids


def _centre(b: BBox) -> tuple[float, float]:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def _inside(b: BBox, r: BBox, pad: float = 1.0) -> bool:
    cx, cy = _centre(b)
    return r[0] - pad <= cx <= r[2] + pad and r[1] - pad <= cy <= r[3] + pad


def _rows_of(words: list[Word]) -> list[list[Word]]:
    if not words:
        return []
    h = statistics.median(w.bbox[3] - w.bbox[1] for w in words) or 8.0
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: _centre(w.bbox)[1]):
        yc = _centre(w.bbox)[1]
        if rows and abs(yc - statistics.mean(_centre(x.bbox)[1] for x in rows[-1])) <= 0.45 * h:
            rows[-1].append(w)
        else:
            rows.append([w])
    for r in rows:
        r.sort(key=lambda w: w.bbox[0])
    return rows


@dataclass
class _Chunk:
    text: str
    x0: float
    x1: float
    words: list[Word]


def _chunks(row: list[Word], gap: float) -> list[_Chunk]:
    out: list[_Chunk] = []
    for w in row:
        if out and w.bbox[0] - out[-1].x1 <= gap:
            c = out[-1]
            c.text += " " + w.text
            c.x1 = max(c.x1, w.bbox[2])
            c.words.append(w)
        else:
            out.append(_Chunk(text=w.text, x0=w.bbox[0], x1=w.bbox[2], words=[w]))
    return out


def _columns(rows: list[list[_Chunk]]) -> list[tuple[float, float]]:
    """Column intervals by gutter voting: an x position separates columns when at most a quarter of the rows
    have a cell chunk covering it. One row whose cells run together cannot merge two columns."""
    chunks = [c for r in rows for c in r]
    if not chunks:
        return []
    lo, hi = min(c.x0 for c in chunks), max(c.x1 for c in chunks)
    step = 0.5
    n = int((hi - lo) / step) + 1
    count = [0] * n
    for r in rows:
        covered = [False] * n
        for c in r:
            for k in range(int((c.x0 - lo) / step), min(n, int((c.x1 - lo) / step) + 1)):
                covered[k] = True
        for k in range(n):
            count[k] += covered[k]
    thr = len(rows) // 4
    cols: list[tuple[float, float]] = []
    start: int | None = None
    for k in range(n + 1):
        filled = k < n and count[k] > thr
        if filled and start is None:
            start = k
        elif not filled and start is not None:
            cols.append((lo + start * step, lo + (k - 1) * step + step))
            start = None
    # gutters narrower than 1.5 pt are word spacing, not column breaks
    merged: list[tuple[float, float]] = []
    for a, b in cols:
        if merged and a - merged[-1][1] < 1.5:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def _assign_x(x0: float, x1: float, cols: list[tuple[float, float]]) -> int:
    best, best_ov = 0, -1e9
    for i, (a, b) in enumerate(cols):
        ov = min(b, x1) - max(a, x0)
        if ov <= 0:
            ov = -min(abs(x0 - b), abs(a - x1))
        if ov > best_ov:
            best, best_ov = i, ov
    return best


def grid_from_words(words: list[Word], bold_boxes: list[BBox] | None = None) -> tuple[list[list[Cell]], int]:
    """Rebuild a table grid from the words in its region. Returns (rows of cells, header_rows)."""
    rows = _rows_of(words)
    if not rows:
        return [], 0
    h = statistics.median(w.bbox[3] - w.bbox[1] for w in words) or 8.0
    chunked = [_chunks(r, gap=0.55 * h) for r in rows]
    body = chunked[1:] if len(chunked) > 2 else chunked
    cols = _columns(body)
    if not cols:
        return [], 0
    bold_boxes = bold_boxes or []
    grid: list[list[Cell]] = []
    for r in chunked:
        texts: list[list[Word]] = [[] for _ in cols]
        for c in r:
            spanned = {_assign_x(w.bbox[0], w.bbox[2], cols) for w in c.words}
            numeric = sum(1 for w in c.words if _NUM.fullmatch(w.text.strip("%()[],;")))
            if len(spanned) == 1 or numeric < 2:  # one cell (or a spanning header / long label)
                texts[_assign_x(c.x0, c.x1, cols)] += c.words
            else:  # a chunk running across a gutter: split it word by word
                for w in c.words:
                    texts[_assign_x(w.bbox[0], w.bbox[2], cols)].append(w)
        cells = []
        for ws in texts:
            raw = " ".join(w.text for w in ws)
            bold = bool(ws) and sum(1 for w in ws if any(_inside(w.bbox, b, 0.5) for b in bold_boxes)) > len(ws) / 2
            cells.append(parse_cell(raw, bold=bold))
        grid.append(cells)
    def data_cell(c: Cell) -> bool:  # "80kV" or "2019" style column labels do not end the header
        return c.value is not None and not re.search(r"[A-Za-z]", c.raw.replace("e-", "").replace("e+", ""))

    header_rows = 0
    for cells in grid:
        if any(data_cell(c) for c in cells[1:]) or (len(cells) == 1 and data_cell(cells[0])):
            break
        header_rows += 1
    if header_rows == len(grid):
        header_rows = 1 if len(grid) > 1 else 0
    return grid, min(header_rows, 3)


def _scan_region(page: RawPage, cap: Caption, direction: int, body: tuple[float, float]) -> BBox | None:
    """Region of tabular word rows directly below (direction=1) or above (-1) a caption, for grids that
    `find_tables()` missed (no ruling lines). Stops at prose (one wide chunk), another caption or a big gap."""
    left, right = body
    width = max(right - left, 1.0)
    in_body = [w for w in page.words if w.bbox[0] >= left - 3 and w.bbox[2] <= right + 3]  # no margin numbers
    if direction > 0:
        words = [w for w in in_body if w.bbox[1] >= cap.bbox[3] - 0.5]
    else:
        words = [w for w in in_body if w.bbox[3] <= cap.bbox[1] + 0.5]
    rows = _rows_of(words)
    if direction < 0:
        rows = rows[::-1]
    if not rows:
        return None
    h = statistics.median(w.bbox[3] - w.bbox[1] for w in words) or 8.0
    taken: list[list[Word]] = []
    edge = cap.bbox[3] if direction > 0 else cap.bbox[1]
    for r in rows:
        y0, y1 = min(w.bbox[1] for w in r), max(w.bbox[3] for w in r)
        gap = (y0 - edge) if direction > 0 else (edge - y1)
        if gap > 3.0 * h:
            break
        text = " ".join(w.text for w in r)
        chunks = _chunks(r, gap=0.55 * h)
        prose = len(chunks) == 1 and (chunks[0].x1 - chunks[0].x0) > 0.6 * width and len(text) > 50
        # a single chunk left of the table's first column after tabular rows: a heading or text, not a row
        outdented = (len(chunks) == 1 and len(taken) >= 2
                     and chunks[0].x0 < min(w.bbox[0] for t in taken for w in t) - 5)
        if prose or outdented or caption_of(text):
            break
        taken.append(r)
        edge = y1 if direction > 0 else y0
    if len(taken) < 2:
        return None
    ws = [w for r in taken for w in r]
    return (min(w.bbox[0] for w in ws), min(w.bbox[1] for w in ws), max(w.bbox[2] for w in ws),
            max(w.bbox[3] for w in ws))


def _looks_like_data(grid: list[list[Cell]]) -> bool:
    return (len(grid) >= 2 and max((len(r) for r in grid), default=0) >= 2
            and sum(1 for r in grid for c in r if c.value is not None) >= 2)


def extract_tables(pages: list[RawPage], captions: list[Caption],
                   body: tuple[float, float] = (72.0, 540.0)) -> tuple[list[Table], list[str]]:
    """Tables for the whole document: located grids matched to "Table N" captions within 80 pt.

    Captions with no grid become `Table(parsed=False)`. Uncaptioned grids are kept as "Table ?k" only if
    they look like data (>= 2 rows, >= 2 columns, a numeric cell)."""
    warnings: list[str] = []
    tables: list[Table] = []
    table_caps = [c for c in captions if c.kind == "table"]
    used_caps: set[int] = set()
    unc = 0
    for page in pages:
        page_caps = [(i, c) for i, c in enumerate(table_caps) if c.page == page.number]
        for gb in page.table_boxes:
            best: tuple[float, int, str] | None = None
            for i, c in page_caps:
                if i in used_caps:
                    continue
                above = gb[1] - c.bbox[3]  # caption above the grid
                below = c.bbox[1] - gb[3]  # caption below the grid
                for d, side in ((above, "above"), (below, "below")):
                    # a caption may sit inside the located grid's box (negative distance): still its caption
                    if -15 <= d <= 80 and (best is None or max(d, 0) < max(best[0], 0)):
                        best = (d, i, side)
            region = gb
            cap: Caption | None = None
            if best is not None:
                cap = table_caps[best[1]]
                used_caps.add(best[1])
                if best[2] == "above":
                    region = (min(gb[0], cap.bbox[0]), cap.bbox[3] + 0.5, max(gb[2], cap.bbox[2]), gb[3])
                else:
                    region = (gb[0], gb[1], gb[2], min(gb[3], cap.bbox[1] - 0.5))
            words = [w for w in page.words if _inside(w.bbox, region)]
            if cap is not None:
                words = [w for w in words if not _inside(w.bbox, cap.bbox, 0.0)]
            grid, header_rows = grid_from_words(words, page.bold_boxes)
            if cap is None:
                numeric = any(c.value is not None for r in grid for c in r)
                if len(grid) < 2 or max((len(r) for r in grid), default=0) < 2 or not numeric:
                    continue
                unc += 1
                tables.append(Table(id=f"Table ?{unc}", caption="", page=page.number, bbox=tuple(gb),
                                    header_rows=header_rows, rows=grid, parsed=bool(grid)))
                warnings.append(f"uncaptioned table grid on page {page.number}")
                continue
            tables.append(Table(id=cap.id, caption=cap.text, page=page.number, bbox=tuple(region),
                                header_rows=header_rows, rows=grid, parsed=bool(grid)))
    for i, c in enumerate(table_caps):
        if i in used_caps:
            continue
        page = pages[c.page - 1]
        best_grid: tuple[int, BBox, list[list[Cell]], int] | None = None
        for direction in (1, -1):
            region = _scan_region(page, c, direction, body)
            if region is None:
                continue
            grid, hdr = grid_from_words([w for w in page.words if _inside(w.bbox, region)], page.bold_boxes)
            score = sum(1 for r in grid for cell in r if cell.value is not None)
            if _looks_like_data(grid) and (best_grid is None or score > best_grid[0]):
                best_grid = (score, region, grid, hdr)
        if best_grid is not None:
            _, region, grid, hdr = best_grid
            tables.append(Table(id=c.id, caption=c.text, page=c.page, bbox=region, header_rows=hdr, rows=grid,
                                parsed=True))
            continue
        tables.append(Table(id=c.id, caption=c.text, page=c.page, bbox=c.bbox, header_rows=0, rows=[],
                            parsed=False))
        warnings.append(f"{c.id}: caption without a parsable grid (page {c.page})")
    seen: set[str] = set()
    out: list[Table] = []
    for t in sorted(tables, key=lambda t: (t.page, t.bbox[1])):
        if t.id in seen:
            continue
        seen.add(t.id)
        out.append(t)
    return out, warnings


def render_table(t: Table) -> str:
    """Plain-text rendering with row and column labels, for prompts and reports."""
    if not t.parsed:
        head = t.caption if t.caption.startswith(t.id) else f"{t.id}: {t.caption}"
        return f"{head}\n(grid not extracted)"
    lines = [t.caption if t.caption.startswith(t.id) else f"{t.id}: {t.caption}".strip()]
    for r in t.rows:
        lines.append(" | ".join(c.raw for c in r))
    return "\n".join(lines)
