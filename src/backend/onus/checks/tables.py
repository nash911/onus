"""Tables as the checks see them: row and column ids, labels, rendering for prompts, and label resolution.

Prompts see each table with row ids (R1, R2, ...) and column ids (C1, C2, ...) next to the labels. A model may
answer with an id or a label. Ids are bounds-checked; labels are matched with rapidfuzz (ratio >= 85, BUILD_PLAN
§7.5) against each row's text cells (and their join) or each column's header cells (and their join). Two rows
or columns that match equally well are *ambiguous*: the caller treats that as inconclusive, never as a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from onus.ingest.lines import normalise
from onus.schemas.document import Cell, Table

LABEL_MIN = 85.0
MAX_PROMPT_ROWS = 45
MAX_PROMPT_CHARS = 5000
_ID = re.compile(r"^\s*([RC])\s*(\d{1,3})\s*$", re.IGNORECASE)
_TABLE_ID = re.compile(r"(?:table|tab\.?)?\s*([A-Z]?\.?\d{1,3}|[IVX]{1,6})\s*$", re.IGNORECASE)


def fmt(x: float) -> str:
    return f"{x:.4g}" if abs(x) < 1e4 else f"{x:,.0f}"


def norm_label(s: str) -> str:
    s = normalise(s).casefold().replace("−", "-").replace("–", "-")
    s = re.sub(r"\[\d+(?:[,–-]\s*\d+)*\]", "", s)  # citation markers: "TAPAS [12]"
    return re.sub(r"\s+", " ", s).strip(" .:*†‡")


def _compact(s: str) -> str:
    return re.sub(r"[^\w]+", "", s)


def label_score(q: str, c: str) -> float:
    """rapidfuzz ratio on normalised labels, also on their punctuation-free forms ("F-1" = "F1")."""
    return max(fuzz.ratio(q, c), fuzz.ratio(_compact(q), _compact(c)) if _compact(q) and _compact(c) else 0.0)


@dataclass
class Resolved:
    index: int | None  # 0-based body row or column index
    status: str  # "ok" | "missing" | "ambiguous"
    score: float = 0.0
    label: str = ""


class TableView:
    def __init__(self, table: Table):
        self.table = table
        self.id = table.id
        rows = table.rows
        self.ncols = max((len(r) for r in rows), default=0)
        h = min(max(table.header_rows, 0), len(rows))
        if h == 0 and rows and all(c.value is None for c in rows[0]) and len(rows) > 1:
            h = 1  # an unlabelled header row of text cells
        self.header = rows[:h]
        self.body = rows[h:]
        self.col_parts: list[list[str]] = []
        for j in range(self.ncols):
            parts = [r[j].raw.strip() for r in self.header if j < len(r) and r[j].raw.strip()]
            self.col_parts.append(list(dict.fromkeys(parts)))
        self.col_labels = [" / ".join(p) if p else f"C{j + 1}" for j, p in enumerate(self.col_parts)]
        self.row_parts: list[list[str]] = []
        for r in self.body:
            texts = [c.raw.strip() for c in r if c.value is None and c.raw.strip()]
            if not texts and r and r[0].raw.strip():
                texts = [r[0].raw.strip()]
            self.row_parts.append(texts)
        self.row_labels = [" ".join(p) if p else f"R{i + 1}" for i, p in enumerate(self.row_parts)]

    # ------------------------------------------------------------ facts

    @property
    def numeric(self) -> bool:
        return any(c.value is not None for r in self.body for c in r)

    def cells(self):
        for i, r in enumerate(self.body):
            for j, c in enumerate(r):
                yield i, j, c

    def cell(self, row: int, col: int) -> Cell | None:
        if 0 <= row < len(self.body) and 0 <= col < len(self.body[row]):
            return self.body[row][col]
        return None

    def has_spread(self) -> bool:
        return any(c.spread is not None for _, _, c in self.cells())

    def all_labels(self) -> list[str]:
        out = [p for ps in self.col_parts for p in ps] + [p for ps in self.row_parts for p in ps]
        out += [c.raw for _, _, c in self.cells() if c.value is None and c.raw.strip()]
        return [x for x in dict.fromkeys(out) if x]

    def where(self, row: int, col: int) -> str:
        return f"{self.id}, row '{self.row_labels[row]}', col '{self.col_labels[col]}'"

    # ------------------------------------------------------------ rendering

    def render(self, max_rows: int = MAX_PROMPT_ROWS, max_chars: int = MAX_PROMPT_CHARS) -> str:
        cap = normalise(self.table.caption)
        head = cap if cap.startswith(self.id) else f"{self.id}: {cap}".strip()
        if len(head) > 400:
            head = head[:399] + "…"
        lines = [head]
        if not self.table.parsed or not self.body:
            return head + "\n(grid not extracted)"
        lines.append("| row | " + " | ".join(f"C{j + 1}: {lab}" for j, lab in enumerate(self.col_labels)) + " |")
        for i, r in enumerate(self.body[:max_rows]):
            cells = [r[j].raw if j < len(r) else "" for j in range(self.ncols)]
            lines.append(f"| R{i + 1} | " + " | ".join(cells) + " |")
        if len(self.body) > max_rows:
            lines.append(f"(… {len(self.body) - max_rows} more rows not shown)")
        out = "\n".join(lines)
        return out if len(out) <= max_chars else out[: max_chars - 1] + "…"

    # ------------------------------------------------------------ resolution

    def _resolve(self, ref: str, axis: str, parts: list[list[str]], labels: list[str], n: int) -> Resolved:
        ref = (ref or "").strip()
        m = _ID.match(ref)
        if m and m.group(1).upper() == axis:
            i = int(m.group(2)) - 1
            return Resolved(i, "ok", 100.0, labels[i]) if 0 <= i < n else Resolved(None, "missing")
        q = norm_label(re.sub(rf"^{axis}\d+\s*[:=]\s*", "", ref, flags=re.IGNORECASE))
        if not q:
            return Resolved(None, "missing")
        scores: list[float] = []
        for i in range(n):
            cands = [norm_label(p) for p in parts[i]] + [norm_label(labels[i])]
            scores.append(max((label_score(q, c) for c in cands if c), default=0.0))
        if not scores:
            return Resolved(None, "missing")
        best = max(scores)
        if best < LABEL_MIN:
            return Resolved(None, "missing", best)
        top = [i for i, s in enumerate(scores) if s >= best - 0.5]
        if len(top) > 1:
            return Resolved(None, "ambiguous", best)
        return Resolved(top[0], "ok", best, labels[top[0]])

    def row(self, ref: str) -> Resolved:
        return self._resolve(ref, "R", self.row_parts, self.row_labels, len(self.body))

    def col(self, ref: str) -> Resolved:
        return self._resolve(ref, "C", self.col_parts, self.col_labels, self.ncols)

    def label_exists(self, label: str) -> bool:
        """For rubric counts: a label the model copied must be a row/column label or a text cell."""
        q = norm_label(label)
        if not q:
            return False
        return any(label_score(q, norm_label(x)) >= LABEL_MIN for x in self.all_labels())


def table_key(ref: str) -> str | None:
    """"Table 2", "table 2", "Tab. 2", "2" -> "2" (the comparable part of a table id)."""
    m = _TABLE_ID.search((ref or "").strip())
    return m.group(1).upper().replace(".", "") if m else None


def find_table(views: list[TableView], ref: str) -> TableView | None:
    k = table_key(ref)
    if k is None:
        return None
    hits = [v for v in views if table_key(v.id) == k]
    return hits[0] if len(hits) == 1 else None
