"""LLM claim extraction plus checklist claims built from the parse. (Phase 3, F2)

`extract_claims(doc)`:
1. `select_input`: abstract + introduction + conclusion (and sections titled as contributions or summary)
   + the first paragraph of each Discussion, about 12k tokens at most. Text comes only from section
   paragraphs, never the front matter (author names vs "Anonymous" would leak the decision), and review
   line numbers are already stripped by ingest.
2. Prompt `extract_claims` (extract role) returns `{quote, type, is_main}` items.
3. Each quote is verified in the paper text (`quotes.QuoteVerifier`); unverified quotes are dropped and
   counted. The stored quote is the paper's own text.
4. Every checklist answer becomes a `checklist` claim with no LLM; sentences of a justification that hold
   a number become extra `result` claims, except for the `claims` item, whose justification restates the
   abstract's claims (already extracted from the abstract) as statements about the paper itself.
5. Wording: strength from the lexicon, scope from lexicon markers or prompt `classify_scope`.
6. Deduplicate (`rapidfuzz.ratio` >= 90, or one span inside another).

A model error never crashes a paper: extraction failure leaves the checklist claims and a warning.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from onus.claims.models import LLM_TYPES, ClaimSet, DraftClaim, DroppedQuote
from onus.claims.quotes import QuoteMatch, QuoteVerifier, match_key
from onus.claims.wording import Lexicon, WordingResult, load_lexicon, wording_for
from onus.ingest.lines import normalise
from onus.llm.client import CallMeta, LLMError
from onus.schemas.document import Document, Paragraph, Section
from onus.schemas.result import Wording

MAX_INPUT_CHARS = 48_000  # about 12k tokens
MAX_LLM_CLAIMS = 25
MAX_QUOTE_SENTENCES = 3  # the prompt asks for at most 2; one extra tolerates splitter differences
MAX_NUMBER_CLAIMS_PER_ITEM = 2
MAX_NUMBER_CLAIMS = 8
NO_NUMBER_CLAIMS_ITEMS = frozenset({"claims"})  # justification restates the abstract: no new claims
DEDUP_RATIO = 90.0

_EXCLUDED_ROLES = {"references", "appendix", "checklist", "ai_checklist", "related_work", "limitations"}
_SUMMARY_TITLE = re.compile(r"\b(?:contributions?|summary|concluding|conclusions?)\b", re.IGNORECASE)
_TYPE_ALIASES = {"results": "result", "comparative": "comparison", "general": "generality",
                 "generalization": "generality", "cause": "causal", "methods": "method",
                 "methodological": "method", "contribution": "method", "data": "availability",
                 "code": "availability", "release": "availability"}


# ---------------------------------------------------------------- input selection


@dataclass
class InputText:
    text: str
    paragraphs: list[Paragraph]
    regions: list[tuple[int, int]]
    sections: list[str] = field(default_factory=list)
    truncated: bool = False
    fallback: bool = False  # no abstract, introduction or conclusion: the opening body text was used


def _paras_in(doc: Document, start: int, end: int) -> list[Paragraph]:
    return [p for p in doc.paragraphs if start <= p.start < end]


def _deepest(doc: Document, offset: int) -> Section | None:
    best = None
    for s in doc.sections:
        if s.start <= offset < s.end and (best is None or s.level >= best.level):
            best = s
    return best


def _label(s: Section) -> str:
    return f"{s.number} {s.title}" if s.number else s.title


FALLBACK_PARAGRAPHS = 40
FALLBACK_CHARS = 8000


def select_input(doc: Document, max_chars: int = MAX_INPUT_CHARS) -> InputText:
    """The text the extraction prompt sees, by priority: abstract, conclusion, contribution/summary
    sections, first Discussion paragraph, closing paragraphs when there is no conclusion, introduction."""
    body_end = min((s.start for s in doc.sections if s.role in ("references", "appendix", "checklist",
                                                                   "ai_checklist")), default=len(doc.text))
    secs = [s for s in doc.sections if s.start < body_end]

    def of_role(role: str) -> list[Paragraph]:
        out: list[Paragraph] = []
        for s in secs:
            if s.role == role:
                out += _paras_in(doc, s.start, s.end)
        return out

    groups: list[list[Paragraph]] = [of_role("abstract"), of_role("conclusion")]
    groups.append([p for s in secs if s.role not in _EXCLUDED_ROLES | {"abstract", "conclusion"}
                   and _SUMMARY_TITLE.search(s.title) for p in _paras_in(doc, s.start, s.end)])
    groups.append([ps[0] for s in secs if s.role == "discussion" and s.level == 1
                   and (ps := _paras_in(doc, s.start, s.end))])
    if not any(s.role == "conclusion" for s in secs):
        tail = [s for s in secs if s.level == 1 and s.role not in _EXCLUDED_ROLES | {"abstract"}]
        if tail:
            groups.append(_paras_in(doc, tail[-1].start, tail[-1].end)[-2:])
    groups.append(of_role("introduction"))
    fallback = not any(groups)
    if fallback:  # no recognised section: the opening body text (before references and appendices)
        groups.append([p for p in doc.paragraphs if p.end <= body_end][:FALLBACK_PARAGRAPHS])

    chosen: dict[int, Paragraph] = {}
    used = 0
    truncated = False
    for group in groups:
        for p in group:
            if p.start in chosen:
                continue
            n = p.end - p.start + 2
            if used + n > max_chars:
                truncated = True
                continue
            chosen[p.start] = p
            used += n
        if fallback and used >= FALLBACK_CHARS:
            break

    paras = [chosen[k] for k in sorted(chosen)]
    parts: list[str] = []
    labels: list[str] = []
    current = None
    for p in paras:
        sec = _deepest(doc, p.start)
        label = _label(sec) if sec is not None else "Text"
        if label != current:
            parts.append(f"### {label}")
            labels.append(label)
            current = label
        parts.append(normalise(doc.text[p.start : p.end]))
    regions = [(p.start, p.end) for p in paras]
    return InputText(text="\n\n".join(parts), paragraphs=paras, regions=regions, sections=labels,
                     truncated=truncated, fallback=fallback and bool(paras))


# ---------------------------------------------------------------- model output


class ExtractedItem(BaseModel):
    quote: str
    type: str = "result"
    is_main: bool = False


class ExtractOut(BaseModel):
    items: list[ExtractedItem] = Field(default_factory=list)


def normalise_type(t: str) -> str:
    t = t.strip().lower()
    t = _TYPE_ALIASES.get(t, t)
    return t if t in LLM_TYPES else "result"


def claim_id(quote: str, salt: str = "") -> str:
    return hashlib.sha1((salt + match_key(quote)).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- numbers in checklist justifications

_REF = re.compile(
    r"(?:\b(?:sections?|secs?\.|tables?|tabs?\.|figures?|figs?\.|appendi(?:x|ces)|app\.|equations?|eqs?\.|"
    r"lines?|pages?|pp?\.|algorithms?|alg\.|theorems?|lemmas?|steps?|items?|questions?|chapters?)\s*"
    r"\(?[A-Z]?\d+(?:\.\d+)*[a-z]?\)?(?:\s*(?:,|and|&|–|-|to)\s*\(?[A-Z]?\d+(?:\.\d+)*[a-z]?\)?)*"
    r"|§\s*\d+(?:\.\d+)*|\[[\d,\s–-]+\]|\(\s*\d{1,2}\s*\)|\b(?:19|20)\d{2}\b"
    r"|\b[A-Za-z]+[-_]?\d+[A-Za-z0-9.]*\b|^\s*\d+[.)]\s)",
    re.IGNORECASE,
)
_DIGIT = re.compile(r"\d")


def has_quantity(sentence: str) -> bool:
    """True when a sentence holds a number that is not a cross-reference, citation, year, list marker or
    part of a name (GPT-4, A100, e5)."""
    return bool(_DIGIT.search(_REF.sub(" ", sentence)))


# ---------------------------------------------------------------- extraction


def _cost(metas: list[CallMeta]) -> dict:
    return {
        "eur": round(sum(m.eur for m in metas), 6),
        "calls": sum(m.calls for m in metas),
        "cache_hits": sum(m.cache_hits for m in metas),
        "tokens_in": sum(m.tokens_in for m in metas),
        "tokens_out": sum(m.tokens_out for m in metas),
    }


def _overlap_inside(a: DraftClaim, b: DraftClaim) -> bool:
    la, lb = a.location, b.location
    return (la.start <= lb.start and lb.end <= la.end) or (lb.start <= la.start and la.end <= lb.end)


def dedupe(claims: list[DraftClaim]) -> tuple[list[DraftClaim], list[DraftClaim]]:
    """Merge near-duplicates (ratio >= 90 on match keys, or one span inside another). The longer quote
    is kept; `is_main` is kept if either was main. Returns (kept, removed)."""
    kept: list[DraftClaim] = []
    removed: list[DraftClaim] = []
    keys: list[str] = []
    for c in claims:
        k = match_key(c.quote)
        hit = None
        for i, other in enumerate(kept):
            if _overlap_inside(c, other) or fuzz.ratio(k, keys[i]) >= DEDUP_RATIO:
                hit = i
                break
        if hit is None:
            kept.append(c)
            keys.append(k)
            continue
        other = kept[hit]
        main = c.is_main or other.is_main
        if len(c.quote) > len(other.quote):
            kept[hit], keys[hit] = c.model_copy(update={"is_main": main}), k
            removed.append(other)
        else:
            kept[hit] = other.model_copy(update={"is_main": main})
            removed.append(c)
    return kept, removed


def _section_role(doc: Document, offset: int) -> str | None:
    s = _deepest(doc, offset)
    return s.role if s is not None else None


def checklist_claims(doc: Document, verifier: QuoteVerifier, lexicon: Lexicon) -> tuple[list[DraftClaim],
                                                                                          list[str]]:
    """One `checklist` claim per parsed checklist item (no LLM), plus `result` claims for justification
    sentences that hold a number."""
    out: list[DraftClaim] = []
    numbers: list[DraftClaim] = []
    warnings: list[str] = []
    for item in doc.checklist:
        region = [(item.start, item.end)]
        quote_src = item.justification.strip() or f"Answer: [{item.answer}]"
        m = verifier.find(quote_src, region)
        if m is not None and not (item.start <= m.start < item.end):
            m = None
        if m is None:
            paras = _paras_in(doc, item.start, item.end)
            ans = [p for p in paras if "answer" in doc.text[p.start : p.end].lower()]
            p = (ans or paras or [None])[0]
            if p is None:
                warnings.append(f"checklist item {item.key!r} could not be located")
                continue
            m = QuoteMatch(p.start, p.end, "exact", 100.0)
        loc = verifier.locate(m.start, m.end)
        if loc is None:
            warnings.append(f"checklist item {item.key!r} could not be located")
            continue
        quote = verifier.text(m.start, m.end)
        strength, s_markers = lexicon.strength_of(quote)
        scope, c_markers = lexicon.scope_of(quote)
        wording = Wording(strength=strength, scope=scope or "specific",
                          markers=list(dict.fromkeys(s_markers + c_markers)))
        out.append(DraftClaim(
            id=claim_id(quote, f"checklist|{item.key}|"), type="checklist", quote=quote, location=loc,
            wording=wording, origin="checklist", section_role="checklist", checklist_key=item.key,
            match="parse", scope_source="markers" if scope else "default",
        ))

        if item.justification.strip() and item.key not in NO_NUMBER_CLAIMS_ITEMS:
            per_item = 0
            for p in [p for p in doc.paragraphs if p.start < m.end and p.end > m.start]:
                for s0, e0 in p.sentences:
                    s, e = max(s0, m.start), min(e0, m.end)
                    if e - s < 15 or per_item >= MAX_NUMBER_CLAIMS_PER_ITEM:
                        continue
                    sent = verifier.text(s, e)
                    if match_key(sent) == match_key(quote):  # the whole justification: already a claim
                        continue
                    if not has_quantity(sent) or len(numbers) >= MAX_NUMBER_CLAIMS:
                        continue
                    sloc = verifier.locate(s, e)
                    if sloc is None:
                        continue
                    st, sm = lexicon.strength_of(sent)
                    sc, cm = lexicon.scope_of(sent)
                    numbers.append(DraftClaim(
                        id=claim_id(sent), type="result", quote=sent, location=sloc,
                        wording=Wording(strength=st, scope=sc or "specific", markers=list(dict.fromkeys(sm + cm))),
                        origin="checklist_number", section_role="checklist", checklist_key=item.key,
                        match="parse", scope_source="markers" if sc else "default",
                    ))
                    per_item += 1
    return out + numbers, warnings


async def extract_claims(doc: Document, llm=None, *, run_index: int = 0, lexicon: Lexicon | None = None,
                         fingerprint: str | None = None) -> ClaimSet:
    """Phase 3 for one Document. `llm` defaults to the process client (`onus.llm.client.get_client`)."""
    if llm is None:
        from onus.llm.client import get_client

        llm = get_client()
    if fingerprint is None:
        from onus.version import fingerprint as _fp

        fingerprint = _fp()
    lex = lexicon or load_lexicon()
    verifier = QuoteVerifier(doc)
    inp = select_input(doc)
    metas: list[CallMeta] = []
    warnings: list[str] = []
    dropped: list[DroppedQuote] = []
    by_method = {"exact": 0, "exact_nonum": 0, "fuzzy": 0}
    stats: dict = {"input_chars": len(inp.text), "input_sections": inp.sections, "input_truncated": inp.truncated,
                   "llm_items": 0, "extract_failed": False}

    items: list[ExtractedItem] = []
    if inp.text.strip():
        try:
            out, meta = await llm.complete_json("extract_claims", {"title": doc.title or "(untitled)",
                                                                   "text": inp.text},
                                                ExtractOut, run_index=run_index, paper_id=doc.paper_id)
            metas.append(meta)
            items = out.items
        except LLMError as e:
            stats["extract_failed"] = True
            warnings.append(f"extract_claims failed: {str(e)[:200]}")
    else:
        warnings.append("no abstract, introduction or conclusion text to extract claims from")
    if inp.fallback:
        warnings.append("no abstract, introduction or conclusion found: claims were taken from the opening body "
                        "text")
    stats["llm_items"] = len(items)

    drafts: list[DraftClaim] = []
    for item in items:
        quote = item.quote.strip()
        if len(match_key(quote)) < 20:
            dropped.append(DroppedQuote(quote=quote, reason="too_short"))
            continue
        m = verifier.find(quote, inp.regions)
        if m is None:
            dropped.append(DroppedQuote(quote=quote, reason="unverified"))
            continue
        if verifier.sentences_spanned(m.start, m.end) > MAX_QUOTE_SENTENCES:
            dropped.append(DroppedQuote(quote=quote, reason="too_long"))
            continue
        loc = verifier.locate(m.start, m.end)
        if loc is None:
            dropped.append(DroppedQuote(quote=quote, reason="unlocatable"))
            continue
        text = verifier.text(m.start, m.end)
        by_method[m.method] += 1
        drafts.append(DraftClaim(
            id=claim_id(text), type=normalise_type(item.type), quote=text, location=loc, is_main=item.is_main,
            wording=Wording(strength="plain", scope="specific"), origin="llm",
            section_role=_section_role(doc, m.start), match=m.method, match_score=m.score,
        ))

    drafts.sort(key=lambda c: c.location.start)
    drafts, dups = dedupe(drafts)
    dropped += [DroppedQuote(quote=c.quote, reason="duplicate") for c in dups]
    if len(drafts) > MAX_LLM_CLAIMS:
        ranked = sorted(drafts, key=lambda c: (not c.is_main, c.location.start))
        keep = {c.id for c in ranked[:MAX_LLM_CLAIMS]}
        dropped += [DroppedQuote(quote=c.quote, reason="over_cap") for c in drafts if c.id not in keep]
        drafts = [c for c in drafts if c.id in keep]

    results: list[WordingResult] = await asyncio.gather(*[
        wording_for(c.quote, llm=llm, lexicon=lex, run_index=run_index, paper_id=doc.paper_id) for c in drafts
    ])
    scope_sources = {"markers": 0, "model": 0, "default": 0}
    disagreements = 0
    failures = 0
    for i, r in enumerate(results):
        drafts[i] = drafts[i].model_copy(update={"wording": r.wording, "scope_source": r.scope_source,
                                                 "model_scope": r.model_scope})
        scope_sources[r.scope_source] += 1
        if r.meta is not None:
            metas.append(r.meta)
        if r.error:
            failures += 1
        if r.scope_source == "markers" and r.model_scope and r.model_scope != r.wording.scope:
            disagreements += 1
    if failures:
        warnings.append(f"classify_scope failed on {failures} claim(s); scope from markers or 'specific'")

    cl, cl_warn = checklist_claims(doc, verifier, lex)
    warnings += cl_warn
    claims = drafts + cl

    stats |= {
        "verified": sum(by_method.values()),
        "by_method": by_method,
        "dropped": {r: sum(1 for d in dropped if d.reason == r)
                    for r in ("unverified", "too_short", "too_long", "unlocatable", "duplicate", "over_cap")},
        "llm_claims": len(drafts),
        "main_claims": sum(1 for c in drafts if c.is_main),
        "checklist_claims": sum(1 for c in cl if c.origin == "checklist"),
        "checklist_number_claims": sum(1 for c in cl if c.origin == "checklist_number"),
        "scope_source": scope_sources,
        "scope_disagreements": disagreements,
        "types": {t: sum(1 for c in claims if c.type == t) for t in (*LLM_TYPES, "checklist")},
        "strength": {s: sum(1 for c in drafts if c.wording.strength == s)
                     for s in ("hedged", "plain", "strong", "absolute")},
    }
    return ClaimSet(paper_id=doc.paper_id, run_index=run_index, fingerprint=fingerprint, claims=claims,
                    dropped=dropped, stats=stats, cost=_cost(metas), warnings=warnings)


# ---------------------------------------------------------------- persistence


def claims_path(var_dir: Path, paper_id: str, run_index: int = 0) -> Path:
    from onus.ingest.document_builder import safe_id

    return var_dir / "claims" / f"{safe_id(paper_id)}.r{run_index}.json"


def save_claims(cs: ClaimSet, var_dir: Path) -> Path:
    p = claims_path(var_dir, cs.paper_id, cs.run_index)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cs.model_dump_json(indent=1))
    return p


def load_claims(path: Path) -> ClaimSet:
    return ClaimSet.model_validate_json(path.read_text())
