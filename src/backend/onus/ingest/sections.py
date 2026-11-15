"""Section headings and role assignment. (Phase 2)

A row is a heading when it is short, set in bold or a larger font than body text, and either
- starts with a section number (`^\\d+(\\.\\d+)*\\s+[A-Z]`; letter numbers such as "A" or "B.2" only after the
  references, where appendices live), or
- is one of the known unnumbered headings (Abstract, References, Acknowledgements, Appendix, Limitations,
  the Agents4Science checklists, ...).
Roles come from title keywords; a subsection with no keyword inherits its parent's role.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from onus.ingest.lines import Layout, Row
from onus.schemas.document import Section

_NUMBERED = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+(?P<title>[A-Z][^\n]{1,118})$")
_LETTERED = re.compile(r"^(?P<num>[A-Z](?:\.\d{1,2}){0,3})\.?\s+(?P<title>[A-Z][^\n]{1,118})$")
_APPENDIX_HEAD = re.compile(r"^(?:Appendix|Appendices)\b(?:\s+(?P<num>[A-Z](?:\.\d+)*))?[:.]?\s*(?P<title>.*)$", re.IGNORECASE)

_KNOWN = {
    "abstract": "abstract",
    "references": "references",
    "bibliography": "references",
    "acknowledgments": "other",
    "acknowledgements": "other",
    "acknowledgment": "other",
    "acknowledgement": "other",
    "limitations": "limitations",
    "limitation": "limitations",
    "introduction": "introduction",
    "conclusion": "conclusion",
    "conclusions": "conclusion",
    "discussion": "discussion",
    "related work": "related_work",
    "broader impact": "other",
    "broader impacts": "other",
    "agents4science paper checklist": "checklist",
    "neurips paper checklist": "checklist",
    "paper checklist": "checklist",
    "agents4science ai involvement checklist": "ai_checklist",
    "ai involvement checklist": "ai_checklist",
}

# order matters: first match wins
_ROLE_KEYWORDS: list[tuple[str, str]] = [
    (r"\bai involvement checklist\b", "ai_checklist"),
    (r"\bchecklist\b", "checklist"),
    (r"\babstract\b", "abstract"),
    (r"\breferences\b|\bbibliography\b", "references"),
    (r"\bappendi(?:x|ces)\b|\bsupplementary\b", "appendix"),
    (r"\blimitation", "limitations"),
    (r"\bintroduction\b", "introduction"),
    (r"\brelated work\b|\bprior work\b|\bliterature\b|\bbackground\b", "related_work"),
    (r"\bconclu|\bsummary\b|\bfuture work\b|\bconcluding\b", "conclusion"),
    (r"\bdiscussion\b", "discussion"),
    (r"\bresults?\b|\bfindings\b|\bablation|\banalysis\b|\bcomparison\b|\bcase stud", "results"),
    (r"\bexperiment|\bevaluation\b|\bsetup\b|\bset-up\b|\bbenchmark|\bimplementation\b|\bdatasets?\b", "experiments"),
    ((r"\bmethod|\bapproach\b|\bframework\b|\bmodel\b|\balgorithm|\bdesign\b|\barchitecture\b|\bformulation\b"
      r"|\bpreliminar|\bproblem\b|\bsystem\b|\bpipeline\b|\btheory\b|\btheoretical\b"), "method"),
]


@dataclass
class HeadingInfo:
    number: str | None
    title: str
    level: int
    known_role: str | None = None


def _clean_title(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip().rstrip(".:").strip()


def detect_heading(row: Row, layout: Layout, *, after_references: bool) -> HeadingInfo | None:
    text = row.text.strip()
    if len(text) > 120 or len(text) < 3:
        return None
    emphasised = row.bold or row.size >= layout.body_size + 0.8
    if not emphasised:
        return None
    if text.endswith((",", ";")) or re.search(r"[.!?]\s+\S", text):  # a sentence, not a heading
        return None
    key = _clean_title(text).lower()
    if key in _KNOWN:
        return HeadingInfo(number=None, title=_clean_title(text), level=1, known_role=_KNOWN[key])
    m = _APPENDIX_HEAD.match(text)
    if m and (m.group("num") or not m.group("title") or after_references):
        title = _clean_title(m.group("title") or "Appendix") or "Appendix"
        num = m.group("num")
        return HeadingInfo(number=num, title=title, level=1 + (num.count(".") if num else 0), known_role="appendix")
    m = _NUMBERED.match(text)
    if m and int(m.group("num").split(".")[0]) <= 30:
        title = _clean_title(m.group("title"))
        if not re.search(r"[A-Za-z]{2}", title) or re.fullmatch(r"[\d.,%±\s()-]+", title):
            return None
        num = m.group("num")
        return HeadingInfo(number=num, title=title, level=1 + num.count("."))
    if after_references:
        m = _LETTERED.match(text)
        if m:
            num = m.group("num")
            return HeadingInfo(number=num, title=_clean_title(m.group("title")), level=1 + num.count("."),
                               known_role="appendix" if "." not in num else None)
    return None


def role_for(title: str) -> str | None:
    t = title.lower()
    for pat, role in _ROLE_KEYWORDS:
        if re.search(pat, t):
            return role
    return None


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "section"


def build_sections(headings: list[tuple[HeadingInfo, int]], text_len: int) -> list[Section]:
    """Sections from (heading, start offset) in document order. A section ends where the next heading of the
    same or a higher level starts. Roles: known role, else title keywords, else the parent's role."""
    sections: list[Section] = []
    used: set[str] = set()
    stack: list[Section] = []
    in_appendix = False
    for i, (h, start) in enumerate(headings):
        end = text_len
        for h2, s2 in headings[i + 1 :]:
            if h2.level <= h.level:
                end = s2
                break
        while stack and stack[-1].level >= h.level:
            stack.pop()
        parent = stack[-1] if stack else None
        role = h.known_role or role_for(h.title)
        if h.known_role == "appendix" or (h.number and h.number[0].isalpha()):
            in_appendix = True
        if in_appendix and role not in ("checklist", "ai_checklist", "appendix", "references", "limitations"):
            role = "appendix" if h.level == 1 else (parent.role if parent else "appendix")
        if role is None:
            role = parent.role if parent else "other"
        base = f"sec:{h.number}" if h.number else f"sec:{_slug(h.title)}"
        sid, k = base, 2
        while sid in used:
            sid, k = f"{base}-{k}", k + 1
        used.add(sid)
        sec = Section(id=sid, title=h.title, number=h.number, role=role, start=start, end=end, level=h.level)
        sections.append(sec)
        stack.append(sec)
    return sections
