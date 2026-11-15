"""Limitations section or limitation paragraphs. (Phase 2)

1. Every section whose role is `limitations` (outside the checklists).
2. If there is none: paragraphs in Discussion or Conclusion sections that mention "limitation".
3. Always: body paragraphs that open with "Limitation(s)" (an inline run-in heading).
"""

from __future__ import annotations

import re

from onus.schemas.document import Paragraph, Section

_EXCLUDED = ("checklist", "ai_checklist", "references")


def _section_of(offset: int, sections: list[Section]) -> Section | None:
    best = None
    for s in sections:
        if s.start <= offset < s.end and (best is None or s.level >= best.level):
            best = s
    return best


def _top_role(offset: int, sections: list[Section]) -> str | None:
    roles = [s.role for s in sections if s.start <= offset < s.end]
    return roles[0] if roles else None


def find_limitations(text: str, sections: list[Section], paragraphs: list[Paragraph]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = [(s.start, s.end) for s in sections if s.role == "limitations"]
    if not spans:
        for p in paragraphs:
            sec = _section_of(p.start, sections)
            if sec and sec.role in ("discussion", "conclusion") and re.search(r"\blimitation", text[p.start : p.end],
                                                                               re.IGNORECASE):
                spans.append((p.start, p.end))
    for p in paragraphs:
        if _top_role(p.start, sections) in _EXCLUDED:
            continue
        if re.match(r"\s*limitations?\b", text[p.start : p.end], re.IGNORECASE):
            spans.append((p.start, p.end))
    merged: list[tuple[int, int]] = []
    for s, e in sorted(set(spans)):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged
