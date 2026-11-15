"""Table/figure/section/appendix/equation mentions and their resolution. (Phase 2)

Handles lists and ranges: "Tables 2 and 3", "Tables 2–4", "Figs. 3a, 4", "Sections 3.1 and 3.2",
"Appendix B.2", "Eq. (5)". Each referenced object becomes one `RefMention` whose span covers that number.
A caption's own "Table 2:" opening is not a mention. Unresolved -> `resolved_id=None`.
"""

from __future__ import annotations

import re

from onus.schemas.document import Figure, RefMention, Section, Table

_SEP = r"\s*(?:,|and|&|or|to|–|—|-)\s*"
_ITEM = r"(?:[A-Z]\.?)?\d{1,3}(?:\.\d{1,2})*[a-z]?"
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("table", re.compile(r"\b(?:Tables?|Tabs?\.|Tab\b)\s*(?P<items>" + _ITEM + r"(?:" + _SEP + _ITEM + r")*)")),
    ("figure", re.compile(r"\b(?:Figures?|Figs?\.|Fig\b)\s*(?P<items>" + _ITEM + r"(?:" + _SEP + _ITEM + r")*)")),
    ("section", re.compile(r"(?:\b(?:Sections?|Secs?\.|Sec\b)|§§?)\s*(?P<items>\d{1,2}(?:\.\d{1,2})*(?:" + _SEP
                           + r"\d{1,2}(?:\.\d{1,2})*)*)")),
    ("appendix", re.compile(r"\b(?:Appendix|Appendices|App\.)\s*(?P<items>[A-Z](?:\.\d{1,2})*(?:" + _SEP
                            + r"[A-Z](?:\.\d{1,2})*)*)\b")),
    ("equation", re.compile(r"\b(?:Equations?|Eqs?\.|Eqn\.?)\s*(?P<items>\(?\d{1,3}\)?(?:" + _SEP
                            + r"\(?\d{1,3}\)?)*)")),
]
_ITEM_RE = re.compile(r"\(?(?P<v>(?:[A-Z]\.?)?\d{1,3}(?:\.\d{1,2})*[a-z]?|[A-Z](?:\.\d{1,2})*)\)?")


def _expand(items: str, base: int) -> list[tuple[str, int, int, bool]]:
    """(value, start, end, in_range) for each referenced number; "2–4" expands to 2, 3, 4."""
    found = [(m.group("v"), base + m.start(), base + m.end()) for m in _ITEM_RE.finditer(items)]
    out: list[tuple[str, int, int, bool]] = []
    for i, (v, s, e) in enumerate(found):
        out.append((v, s, e, False))
        if i + 1 < len(found):
            between = items[e - base : found[i + 1][1] - base]
            if re.fullmatch(r"\s*(?:–|—|-|to)\s*", between) and v.isdigit() and found[i + 1][0].isdigit():
                lo, hi = int(v), int(found[i + 1][0])
                if 0 < hi - lo <= 10:
                    out += [(str(k), s, found[i + 1][2], True) for k in range(lo + 1, hi)]
    return out


def find_mentions(text: str, tables: list[Table], figures: list[Figure], sections: list[Section],
                  caption_starts: set[int] | None = None, equation_tags: set[str] | None = None) -> list[RefMention]:
    """`equation_tags`: numbers that appear as "(N)" at the end of a line (display-equation tags)."""
    caption_starts = caption_starts or set()
    table_ids = {t.id for t in tables}
    fig_ids = {f.id for f in figures}
    sec_by_num = {s.number: s.id for s in sections if s.number}
    eq_tags = equation_tags if equation_tags is not None else set(re.findall(r"\((\d{1,3})\)\s*(?:\n|$)", text))
    out: list[RefMention] = []
    for kind, pat in _PATTERNS:
        for m in pat.finditer(text):
            if m.start() in caption_starts:
                continue
            for idx, (v, s, e, _) in enumerate(_expand(m.group("items"), m.start("items"))):
                start = m.start() if idx == 0 else s  # the first mention includes the keyword
                resolved: str | None = None
                if kind == "table":
                    label = f"Table {re.sub(r'(?<=\d)[a-z]$', '', v)}"
                    resolved = label if label in table_ids else None
                elif kind == "figure":
                    base = re.sub(r"[a-z]$", "", v)
                    label = f"Figure {base}"
                    resolved = label if label in fig_ids else None
                    label = f"Figure {v}"
                elif kind == "section":
                    label = f"Section {v}"
                    resolved = sec_by_num.get(v)
                elif kind == "appendix":
                    label = f"Appendix {v}"
                    resolved = sec_by_num.get(v)
                else:
                    label = f"Equation ({v})"
                    resolved = f"eq:{v}" if v in eq_tags else None
                out.append(RefMention(kind=kind, label=label, start=start, end=e, resolved_id=resolved))
    out.sort(key=lambda r: (r.start, r.end))
    return out
