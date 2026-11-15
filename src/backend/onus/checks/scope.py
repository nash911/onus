"""Scope against the paper's own limitations (F7). (Phase 5)

1. Prompt `extract_limitations` (extract role) copies the stated limitations from the limitations text found by
   ingest (a Limitations section, or limitation paragraphs) and from the checklist's limitations justification.
   Every quote must verify inside those regions; others are dropped and counted.
2. For claims with strong or absolute strength or general or universal scope (hedged claims excepted), prompt
   `scope_conflict` (judge role) decides whether a stated limitation narrows what the claim asserts. Claims are
   sent in small batches with the numbered limitations; the model answers with ids (C1, L2), never free quotes.
3. A conflict citing a valid limitation id -> `fail`, severity "overstates", with both quotes.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from onus.claims.quotes import match_key
from onus.llm.client import CallMeta, LLMError
from onus.schemas.result import CheckResult

from .context import ClaimContext, PaperContext
from .models import Limitation, not_run

MAX_LIMITATIONS = 15
LIMITATIONS_CHARS = 12000
BATCH = 6
MAX_LIM_SENTENCES = 3


class LimitationItem(BaseModel):
    quote: str


class LimitationsOut(BaseModel):
    items: list[LimitationItem] = Field(default_factory=list)


class ConflictItem(BaseModel):
    claim_id: str
    conflict: bool = False
    limitation_id: str = ""
    explanation: str = ""


class ConflictOut(BaseModel):
    items: list[ConflictItem] = Field(default_factory=list)


def limitation_regions(pc: PaperContext) -> list[tuple[int, int]]:
    regions = list(pc.doc.limitations_spans)
    for item in pc.doc.checklist:
        if item.key == "limitations":
            regions.append((item.start, item.end))
    return regions


def limitations_input(pc: PaperContext) -> str:
    parts = [pc.text(s, e) for s, e in pc.doc.limitations_spans]
    for item in pc.doc.checklist:
        if item.key == "limitations" and item.justification.strip():
            parts.append(f"(Paper checklist, limitations item) {item.justification}")
    text = "\n\n".join(p for p in parts if p)
    return text if len(text) <= LIMITATIONS_CHARS else text[: LIMITATIONS_CHARS - 1] + "…"


async def extract_limitations(pc: PaperContext, llm, *, run_index: int, metas: list[CallMeta],
                              stats: dict) -> tuple[list[Limitation], str | None]:
    """Verified limitation quotes, or ([], reason) when there is nothing to check against."""
    text = limitations_input(pc)
    if not text.strip():
        return [], "no limitations stated in the paper"
    try:
        out, meta = await llm.complete_json("extract_limitations", {"limitations": text}, LimitationsOut,
                                            run_index=run_index, paper_id=pc.doc.paper_id)
        metas.append(meta)
    except LLMError as e:
        return [], f"limitations extraction failed: {str(e)[:120]}"
    regions = limitation_regions(pc)
    found: list[Limitation] = []
    for it in out.items:
        m = pc.v.find(it.quote, regions)
        if m is None or not any(s <= m.start < e for s, e in regions):
            stats["limitations_unverified"] += 1
            continue
        if pc.v.sentences_spanned(m.start, m.end) > MAX_LIM_SENTENCES:
            stats["limitations_too_long"] += 1
            continue
        quote = pc.v.text(m.start, m.end)
        loc = pc.v.locate(m.start, m.end)
        if loc is None or any(match_key(x.quote) == match_key(quote) for x in found):
            continue
        found.append(Limitation(id=f"L{len(found) + 1}", quote=quote, location=loc))
        if len(found) >= MAX_LIMITATIONS:
            break
    if not found:
        return [], "no verified limitation statements"
    return found, None


def _scrub_ids(text: str) -> str:
    """The judge sees batch ids (C1, L2); readers do not."""
    text = re.sub(r"\s*\(\s*[LC]\d{1,2}\s*\)", "", text.strip())
    text = re.sub(r"\b[Ll]imitation\s+L\d{1,2}\b|\bL\d{1,2}\b", "the limitation", text)
    return re.sub(r"\b[Cc]laim\s+C\d{1,2}\b|\bC\d{1,2}\b", "the claim", text)


def eligible(ctx: ClaimContext) -> bool:
    w = ctx.claim.wording
    return w.strength != "hedged" and (w.strength in ("strong", "absolute") or w.scope in ("general", "universal"))


def _render_lims(lims: list[Limitation]) -> str:
    return "\n".join(f'{x.id}: "{x.quote}"' for x in lims)


async def check_scope(ctxs: list[ClaimContext], lims: list[Limitation], lim_reason: str | None, llm, *,
                      run_index: int, paper_id: str, metas: list[CallMeta], stats: dict) -> dict[str, CheckResult]:
    out: dict[str, CheckResult] = {}
    targets = []
    for c in ctxs:
        if not eligible(c):
            out[c.claim.id] = not_run("scope", "claim wording is not strong, absolute, general or universal")
        elif lim_reason:
            out[c.claim.id] = not_run("scope", lim_reason)
        else:
            targets.append(c)
    by_id = {x.id: x for x in lims}
    for i in range(0, len(targets), BATCH):
        batch = targets[i : i + BATCH]
        claims_txt = "\n".join(f'C{k + 1}: "{c.claim.quote}"' for k, c in enumerate(batch))
        try:
            res, meta = await llm.complete_json("scope_conflict", {"claims": claims_txt, "limitations": _render_lims(lims)},
                                                ConflictOut, run_index=run_index, paper_id=paper_id)
            metas.append(meta)
        except LLMError as e:
            for c in batch:
                out[c.claim.id] = CheckResult(check="scope", outcome="inconclusive", detail="Scope judgement failed.",
                                              data={"error": str(e)[:200]})
            continue
        answers: dict[int, ConflictItem] = {}
        for it in res.items:
            cid = it.claim_id.strip().strip("[]").upper()
            if cid.startswith("C") and cid[1:].isdigit() and 1 <= int(cid[1:]) <= len(batch):
                answers.setdefault(int(cid[1:]) - 1, it)
            else:
                stats["scope_invalid_ids"] += 1
        for k, c in enumerate(batch):
            a = answers.get(k)
            if a is None:
                out[c.claim.id] = CheckResult(check="scope", outcome="inconclusive",
                                              detail="The scope judge gave no answer for this claim.")
                continue
            if not a.conflict:
                out[c.claim.id] = CheckResult(check="scope", outcome="pass",
                                              detail="No stated limitation narrows this claim.",
                                              data={"limitations": len(lims), "explanation": a.explanation.strip()})
                continue
            lim = by_id.get(a.limitation_id.strip().strip("[]").upper())
            if lim is None:
                stats["scope_invalid_ids"] += 1
                out[c.claim.id] = CheckResult(check="scope", outcome="inconclusive",
                                              detail="The scope judge reported a conflict without a valid limitation id.",
                                              data={"explanation": a.explanation.strip()})
                continue
            expl = _scrub_ids(a.explanation).rstrip(".")
            expl = expl[:1].upper() + expl[1:]
            out[c.claim.id] = CheckResult(
                check="scope", outcome="fail", severity="overstates",
                detail=f'The paper\'s own limitations say "{lim.quote}" ({lim.location.cite()}). {expl}.',
                data={"limitation": lim.model_dump(), "explanation": _scrub_ids(a.explanation)})
    return out
