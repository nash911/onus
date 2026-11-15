"""Agents4Science paper checklist and AI-involvement checklist parsing. (Phase 2)

Paper checklist: each item is "N. Title  Question: ...  Answer: [Yes|No|NA]  Justification: ...  Guidelines: ...".
Items are keyed by matching their titles (not their positions), so the 10-item and 7-item variants both
work. The Guidelines text is dropped. The AI-involvement checklist ([A]-[D] answers) is parsed for display
only; nothing in the pipeline scores it.
"""

from __future__ import annotations

import re

from onus.schemas.document import ChecklistItem, Section

_KEYS: list[tuple[str, str]] = [
    (r"^claims?\b", "claims"),
    (r"limitation", "limitations"),
    (r"theor|proof", "theory"),
    (r"reproducib", "reproducibility"),
    (r"open access|data and code|code and data|open-access", "open_access"),
    (r"setting|details", "experimental_details"),
    (r"statistical significance|error bar|significance", "error_bars"),
    (r"compute", "compute"),
    (r"ethic", "ethics"),
    (r"broader|societal", "broader_impacts"),
    (r"safeguard", "safeguards"),
    (r"licen[cs]e", "licenses"),
    (r"new assets?", "new_assets"),
    (r"crowdsourc|human subjects", "human_subjects"),
    (r"\birb\b|institutional review", "irb"),
    (r"\bllm\b|language model", "llm_usage"),
]


def key_for(title: str) -> str:
    t = title.lower().strip()
    for pat, key in _KEYS:
        if re.search(pat, t):
            return key
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")[:40] or "item"


# Some papers drop the Question line ("1. Claims Answer: [Yes] Justification: ..."); both forms are items.
_ITEM = re.compile(r"(?:(?<=\s)|^)(?P<n>\d{1,2})\.\s+(?P<title>[A-Z][^\n?:]{1,80}?)\s+(?:Question:\s*|(?=Answer:))")
_ANSWER = re.compile(r"\s*Answer:\s*(?:\[\s*(?P<b>[^\]\n]{0,20}?)\s*\]|(?P<a>[A-Za-z/.]{1,15}))")
_JUST = re.compile(r"Justification:\s*")
_GUIDE = re.compile(r"\s*Guidelines:")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _normalise_answer(a: str) -> str:
    a = _clean(a).strip("[]").strip()
    low = a.lower().replace("/", "")
    return {"yes": "Yes", "no": "No", "na": "NA", "n.a.": "NA", "not applicable": "NA"}.get(low, a)


def parse_checklist(text: str, sections: list[Section]) -> list[ChecklistItem]:
    secs = [s for s in sections if s.role == "checklist"]
    items: list[ChecklistItem] = []
    for sec in secs:
        body = text[sec.start : sec.end]
        heads = list(_ITEM.finditer(body))
        for i, h in enumerate(heads):
            seg_end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
            seg = body[h.end() : seg_end]
            am = re.search(r"Answer:", seg)
            question = _clean(seg[: am.start()]) if am else _clean(seg)
            answer, justification = "", ""
            end_rel = seg_end
            if am:
                rest = seg[am.start() :]
                a = _ANSWER.match(rest)
                answer = _normalise_answer(a.group("b") if a.group("b") is not None else a.group("a")) if a else ""
                jm = _JUST.search(rest)
                if jm:
                    jtext = rest[jm.end() :]
                    g = _GUIDE.search(jtext)
                    # Without a Guidelines block (often the last item), the justification ends with its own
                    # paragraph: whatever follows the checklist (author lists, affiliations, notes) is not
                    # part of it, and front matter of that kind must never reach the pipeline.
                    cut = g.start() if g else (jtext.find("\n\n") if "\n\n" in jtext else len(jtext))
                    justification = _clean(jtext[:cut])
                    end_rel = h.end() + am.start() + jm.end() + cut
            title = _clean(h.group("title"))
            items.append(ChecklistItem(key=key_for(title), title=title, question=question, answer=answer,
                                       justification=justification, start=sec.start + h.start(),
                                       end=sec.start + end_rel))
    # keys unique: a repeated key keeps the first occurrence, later ones get a suffix
    seen: dict[str, int] = {}
    for it in items:
        if it.key in seen:
            seen[it.key] += 1
            it.key = f"{it.key}_{seen[it.key]}"
        else:
            seen[it.key] = 1
    return items


_AI_ITEM = re.compile(r"(?:(?<=\s)|^)(?P<n>\d{1,2})\.\s+(?P<title>[A-Z][^:\n]{2,80}):\s*")


def parse_ai_checklist(text: str, sections: list[Section]) -> list[dict]:
    out: list[dict] = []
    for sec in (s for s in sections if s.role == "ai_checklist"):
        body = text[sec.start : sec.end]
        heads = list(_AI_ITEM.finditer(body))
        for i, h in enumerate(heads):
            seg = body[h.end() : heads[i + 1].start() if i + 1 < len(heads) else len(body)]
            am = re.search(r"Answer:\s*\[?\s*([A-D])\s*\]?", seg)
            em = re.search(r"(?:Explanation|Description):\s*", seg)
            description = _clean(seg[: (am or em).start()] if (am or em) else seg)
            out.append({
                "index": int(h.group("n")),
                "title": _clean(h.group("title")),
                "description": description,
                "answer": am.group(1) if am else None,
                "explanation": _clean(seg[em.end() :]) if em else "",
                "start": sec.start + h.start(),
                "end": sec.start + h.end() + len(seg.rstrip()),
            })
    # The template text may precede the answered checklist (a second heading): per item index, keep the
    # entry that carries an answer, else the last one.
    best: dict[int, dict] = {}
    for item in out:
        prev = best.get(item["index"])
        if prev is None or item["answer"] or not prev["answer"]:
            best[item["index"]] = item
    return sorted(best.values(), key=lambda x: x["index"])
