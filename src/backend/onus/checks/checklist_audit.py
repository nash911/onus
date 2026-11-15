"""Checklist audit (F6). (Phase 5)

Every paper-checklist item gets a status:
- "No" / "NA" answers -> not_applicable.
- "Yes" on an item in `rubrics/checklist_map.yaml` (Appendix B.4) -> a deterministic check on the paper body
  (sections other than the checklists and references, plus table renderings and figure captions) and the
  submitted code index. A deterministic negative where the map says so (open-code "Yes" with no source files
  and no repository link) -> contradicted; otherwise unbacked.
- "Yes" on any other item -> prompt `checklist_item` (extract role) over candidate passages (objects the
  justification cites, then BM25 on the question and justification). A "backed" answer must point at a
  passage and quote it; the quote is verified inside that passage, or the status becomes not_checkable.
- The "claims" item is derived after verdicts (Phase 6); here it is not_checkable and listed in
  `derived_checklist`.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel
from rank_bm25 import BM25Okapi

from onus.evidence.models import Candidate
from onus.evidence.retrieve import tokens
from onus.llm.client import CallMeta, LLMError
from onus.schemas.document import ChecklistItem
from onus.schemas.result import ChecklistAudit, Evidence

from .context import PaperContext
from .rubrics import ChecklistRule, default_checklist_map

MAX_PASSAGES = 6
MIN_PASSAGE_CHARS = 60
SNIP = 300

Status = Literal["backed", "unbacked", "contradicted", "not_applicable", "not_checkable"]


class ItemOut(BaseModel):
    status: str = "not_checkable"
    passage_id: str = ""
    quote: str = ""
    reason: str = ""


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _clip(s: str, n: int = SNIP) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


class Auditor:
    def __init__(self, pc: PaperContext, rules: dict[str, ChecklistRule] | None = None):
        self.pc = pc
        self.doc = pc.doc
        self.rules = rules if rules is not None else default_checklist_map()
        self._cond: dict[str, tuple[bool, list[Evidence], str]] = {}
        self._n = 0
        self.body = [(p, pc.text(p.start, p.end), pc.role_at(p.start)) for p in pc.body_paragraphs()]

    def _ev_id(self, key: str) -> str:
        self._n += 1
        return f"ck-{key}-{self._n}"

    def _from_candidate(self, key: str, c: Candidate | None, relation: str = "supports") -> list[Evidence]:
        return [c.evidence(self._ev_id(key), "retrieval", relation)] if c is not None else []

    # ------------------------------------------------------------ deterministic conditions

    def _pattern_hit(self, rule: ChecklistRule) -> tuple[bool, list[Evidence], str]:
        assert rule.pattern is not None
        for p, text, role in self.body:
            if rule.roles is not None and role not in rule.roles:
                continue
            m = rule.pattern.search(text)
            if m:
                c = self.pc.index.paragraph(p)
                return True, self._from_candidate(rule.key, c), f"Found \"{m.group(0).strip()}\" in {c.label}."
        for v in self.pc.views.values():
            m = rule.pattern.search(v.render(max_rows=200, max_chars=20000))
            if m:
                return True, self._from_candidate(rule.key, self.pc.index.get(v.id)), \
                    f"Found \"{m.group(0).strip()}\" in {v.id}."
        for f in self.doc.figures:
            m = rule.pattern.search(f.caption)
            if m:
                return True, self._from_candidate(rule.key, self.pc.index.get(f.id)), \
                    f"Found \"{m.group(0).strip()}\" in the caption of {f.id}."
        return False, [], ""

    def condition(self, key: str) -> tuple[bool, list[Evidence], str]:
        if key in self._cond:
            return self._cond[key]
        rule = self.rules[key]
        res: tuple[bool, list[Evidence], str] = (False, [], "")
        if rule.check == "spread_or_pattern":
            spread = [v for v in self.pc.views.values() if v.has_spread()]
            if spread:
                v = spread[0]
                res = (True, self._from_candidate(key, self.pc.index.get(v.id)),
                       f"{v.id} reports spreads (±, CI or std) in its cells.")
            else:
                res = self._pattern_hit(rule)
        elif rule.check == "pattern":
            res = self._pattern_hit(rule)
        elif rule.check == "code_or_repo":
            src = [f for f in self.doc.code.files if f.get("is_source")]
            if src:
                ev = Evidence(id=self._ev_id(key), kind="code", label=f"{len(src)} source file(s) in the submitted code",
                              snippet=", ".join(f["path"] for f in src[:6]), location=None, link_method="code_search",
                              relation="supports")
                res = (True, [ev], f"The submission includes {len(src)} source file(s).")
            elif self.doc.code.repo_urls_in_paper:
                url = self.doc.code.repo_urls_in_paper[0]
                pos = self.doc.text.find(url)
                loc = self.pc.v.locate(pos, pos + len(url)) if pos >= 0 else None
                ev = Evidence(id=self._ev_id(key), kind="text", label=url, snippet=url, location=loc,
                              link_method="retrieval", relation="supports")
                res = (True, [ev], f"The paper links a code repository ({url}).")
        elif rule.check == "limitations_present":
            spans = self.doc.limitations_spans
            if spans:
                p = self.pc.paragraph_at(spans[0][0]) or next(
                    (p for p in self.doc.paragraphs if spans[0][0] <= p.start < spans[0][1]), None)
                c = self.pc.index.paragraph(p) if p else None
                res = (True, self._from_candidate(key, c),
                       f"Limitations are discussed ({c.label if c else 'limitations section'}).")
        elif rule.check == "any_of":
            for k in rule.of:
                ok, ev, why = self.condition(k)
                if ok:
                    res = (True, ev, why)
                    break
        self._cond[key] = res
        return res

    # ------------------------------------------------------------ one item

    def deterministic(self, item: ChecklistItem, rule: ChecklistRule) -> ChecklistAudit:
        base = {"key": item.key, "title": item.title, "answer": item.answer, "justification": item.justification}
        if rule.check == "derived":
            return ChecklistAudit(**base, status="not_checkable", reason=rule.messages.get("note", "Derived later."))
        ok, ev, why = self.condition(item.key)
        # backing comes from the paper body only; the checklist justification never backs itself
        if ok:
            return ChecklistAudit(**base, status="backed", reason=why, evidence=ev)
        if "contradicted" in rule.messages:
            return ChecklistAudit(**base, status="contradicted",
                                  reason=f"Answered Yes, but {rule.messages['contradicted']}.")
        return ChecklistAudit(**base, status="unbacked",
                              reason=f"Answered Yes, but {rule.messages.get('unbacked', 'no backing was found')}.")

    def passages(self, item: ChecklistItem) -> list[Candidate]:
        out: list[Candidate] = []
        for m in self.doc.mentions:
            if item.start <= m.start < item.end and m.resolved_id:
                c = self.pc.index.get(m.resolved_id)
                if c is not None and all(x.key != c.key for x in out):
                    out.append(c)
        title_toks = set(tokens(item.title)) - {"paper", "statement", "section"}
        for sec in self.doc.sections:  # a section named after the item ("Ethics Statement", "Broader Impact")
            if sec.role in ("checklist", "ai_checklist", "references") or not title_toks & set(tokens(sec.title)):
                continue
            for p in [p for p in self.doc.paragraphs if sec.start <= p.start < sec.end][:2]:
                c = self.pc.index.paragraph(p)
                if all(x.key != c.key for x in out):
                    out.append(c)
        pool = [(p, t) for p, t, _ in self.body if p.end - p.start >= MIN_PASSAGE_CHARS]
        q = tokens(f"{item.title} {item.question} {item.justification}")
        if pool and q:
            bm = BM25Okapi([tokens(t) or ["_"] for _, t in pool])
            scores = bm.get_scores(q)
            for i in sorted(range(len(pool)), key=lambda i: -scores[i]):
                if len(out) >= MAX_PASSAGES or scores[i] <= 0:
                    break
                c = self.pc.index.paragraph(pool[i][0])
                if all(x.key != c.key for x in out):
                    out.append(c)
        return out[:MAX_PASSAGES]

    async def by_model(self, item: ChecklistItem, llm, *, run_index: int, metas: list[CallMeta],
                       stats: dict) -> ChecklistAudit:
        base = {"key": item.key, "title": item.title, "answer": item.answer, "justification": item.justification}
        cands = self.passages(item)
        if not cands:
            return ChecklistAudit(**base, status="not_checkable", reason="No passage in the paper body to check against.")
        rendered = "\n\n".join(f"[P{i}] {c.label}\n{c.text}" for i, c in enumerate(cands, 1))
        try:
            out, meta = await llm.complete_json(
                "checklist_item", {"title": item.title, "question": item.question or item.title,
                                   "answer": item.answer, "justification": item.justification or "(none)",
                                   "passages": rendered},
                ItemOut, run_index=run_index, paper_id=self.doc.paper_id)
            metas.append(meta)
        except LLMError:
            stats["checklist_llm_failed"] += 1
            return ChecklistAudit(**base, status="not_checkable", reason="The checklist check failed to run.")
        status = out.status.strip().lower().replace(" ", "_")
        reason = out.reason.strip()
        if status == "backed":
            pid = out.passage_id.strip().strip("[]").upper()
            idx = int(pid[1:]) - 1 if pid.startswith("P") and pid[1:].isdigit() else -1
            if not 0 <= idx < len(cands):
                stats["checklist_invalid_ids"] += 1
                return ChecklistAudit(**base, status="not_checkable", reason="Backing passage not identified.")
            c = cands[idx]
            m = self.pc.v.find(out.quote, [(c.start, c.end)]) if c.start >= 0 else None
            if m is None or not (c.start <= m.start < c.end):
                stats["checklist_unverified_quotes"] += 1
                return ChecklistAudit(**base, status="not_checkable",
                                      reason="The backing quote could not be verified in the paper.")
            ev = self._from_candidate(item.key, c)
            quote = self.pc.v.text(m.start, m.end)
            return ChecklistAudit(**base, status="backed", reason=f"{_cap(reason)} (\"{_clip(quote, 200)}\", {c.label})."
                                  if reason else f"\"{_clip(quote, 200)}\" ({c.label}).", evidence=ev)
        if status == "unbacked":
            return ChecklistAudit(**base, status="unbacked", reason=_cap(reason) or "Not backed by the paper body.")
        return ChecklistAudit(**base, status="not_checkable", reason=_cap(reason) or "Cannot be judged from the text.")


async def audit_checklist(pc: PaperContext, llm, *, run_index: int, metas: list[CallMeta], stats: dict,
                          rules: dict[str, ChecklistRule] | None = None) -> tuple[list[ChecklistAudit], list[str]]:
    a = Auditor(pc, rules)
    derived: list[str] = []

    async def one(item: ChecklistItem) -> ChecklistAudit:
        base = {"key": item.key, "title": item.title, "answer": item.answer, "justification": item.justification}
        ans = item.answer.strip()
        if ans in ("No", "NA"):
            return ChecklistAudit(**base, status="not_applicable", reason=f"Answered {ans}.")
        if ans != "Yes":
            return ChecklistAudit(**base, status="not_checkable", reason=f"Unrecognised answer {ans!r}.")
        rule = a.rules.get(item.key)
        if rule is not None:
            if rule.check == "derived":
                derived.append(item.key)
            return a.deterministic(item, rule)
        return await a.by_model(item, llm, run_index=run_index, metas=metas, stats=stats)

    audits = await asyncio.gather(*[one(i) for i in pc.doc.checklist])
    return list(audits), derived

