"""Planted-error mutations on a parsed `Document`. (Phase 7)

Mutations edit the parsed Document, not the PDF, so they test checking rather than parsing. Each type has
preconditions; a paper that does not meet them is "not applicable" for that type. Every edit to
`Document.text` goes through `apply_edits`, which shifts all character offsets (lines, paragraphs, sentences,
sections, mentions, checklist items, limitation spans), so the mutated Document stays internally consistent.

| Type | Mutation | Detected when |
| abstract_number | an abstract number that also appears in a parsed table: x1.15, or +3 points for percentages | the claim holding it is `contradicted` by the number check |
| checklist_flip | a "No"/"NA" answer on error_bars, open_access, compute or experimental_details becomes "Yes" | that item is `unbacked` or `contradicted` |
| remove_table | a parsed table that >= 1 claim relies on is deleted (object, caption and grid text) | each such claim is `untraced` or `not_checkable` |
| widen_scope | a specific claim is rewritten to universal wording (prompt `widen_scope`, fast role) | that claim is `overstated` or `contradicted` |
| delete_errorbars | spreads, std/CI and significance-test mentions are removed; the error-bars "Yes" stays | the error_bars item is `unbacked` (or `contradicted`) |

A claim "relies on" a table when its sentence cites the table, or the table is its only supporting evidence.
Selection is seeded: `random.Random(f"{seed}:{paper_id}:{type}")`.
"""

from __future__ import annotations

import itertools
import random
import re
from bisect import bisect_right
from collections.abc import Callable

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from onus.checks.numbers import extract_numbers
from onus.claims.quotes import match_key
from onus.claims.wording import load_lexicon
from onus.ingest.tables import render_table
from onus.llm.client import LLMError
from onus.schemas.document import Cell, Document
from onus.schemas.result import RunResult

TYPES = ("abstract_number", "checklist_flip", "remove_table", "widen_scope", "delete_errorbars")
FLIP_KEYS = ("error_bars", "open_access", "compute", "experimental_details")
NO_ANSWERS = {"no", "na", "n/a", "not applicable"}
EXCLUDED_ROLES = {"checklist", "ai_checklist", "references"}

# what delete_errorbars removes (prose sentences) and strips inline (grid text, captions, cells)
ERR_SENTENCE = re.compile(
    r"±|\+/-|\bstandard (?:deviation|error)s?\b|\bstd\b|\bs\.d\.|\bSEM\b|\bconfidence intervals?\b|\bCIs?\b|"
    r"\berror bars?\b|\bp\s*[<=>≤≥]\s*\d|\bp-values?\b|\bq\s*[<=≤]\s*0?\.\d|\bt-tests?\b|\bsignifican(?:t|ce)\b|"
    r"\bbootstrap|\bchi-?squared?\b|χ2|χ²|\bBenjamini|\bBonferroni|\bWilcoxon|\bMann[- ]Whitney|\bANOVA\b|"
    r"\bpermutation tests?\b|\bFDR\b|\bvariance (?:across|over|between) (?:runs|seeds|trials)", re.IGNORECASE)
COMPUTE = re.compile(r"\b(?:GPUs?|CPUs?|TPUs?|A100|H100|V100|RTX|cores?|RAM|GB of memory|cluster|"
                     r"\d+(?:\.\d+)?\s*(?:GPU|CPU)?[- ]?hours?|wall[- ]clock|compute budget)\b", re.IGNORECASE)
ERR_INLINE = re.compile(r"\s*(?:±|\+/-)\s*\d*\.?\d+%?"
                        r"|(?:(?<=\d)|(?<=\d%))\s*[(\[]\s*-?\d*\.?\d+\s*%?\s*[–\-]\s*-?\d*\.?\d+\s*%?\s*[)\]]")


class Mutation(BaseModel):
    paper_id: str
    type: str
    seed: int
    applied: bool
    reason: str = ""  # why not applicable, or what was changed
    original: str = ""
    replacement: str = ""
    span: tuple[int, int] | None = None  # target span in the mutated Document
    table_id: str | None = None
    checklist_key: str | None = None
    target_claims: list[dict] = Field(default_factory=list)  # [{quote, span}] in mutated offsets
    error: bool = False  # could not be applied because a model call failed (counted as a failed run)

    @property
    def mutated_id(self) -> str:
        return f"{self.paper_id}~{self.type}"


# ---------------------------------------------------------------- offset-preserving edits

Edit = tuple[int, int, str]


def offset_mapper(edits: list[Edit]) -> Callable[[int], int]:
    """Map an offset in the old text to the new text. Offsets inside a replaced span move to its start."""
    eds = sorted(edits)
    starts = [s for s, _, _ in eds]
    shifts, acc = [], 0
    for s, e, new in eds:
        acc += len(new) - (e - s)
        shifts.append(acc)

    def m(p: int) -> int:
        i = bisect_right(starts, p) - 1
        while i >= 0 and eds[i][1] > p:  # inside (or at the start of) edit i
            s, _e, new = eds[i]
            base = shifts[i - 1] if i > 0 else 0
            return s + base + (min(p - s, len(new)) if p > s else 0)
        if i < 0:
            return p
        return p + shifts[i]

    return m


def apply_edits(doc: Document, edits: list[Edit]) -> tuple[Document, Callable[[int], int]]:
    """A copy of `doc` with the text edits applied and every offset remapped. Edits must not overlap."""
    eds = sorted(edits)
    for (s1, e1, _), (s2, _, _) in itertools.pairwise(eds):
        if s2 < e1:
            raise ValueError("overlapping edits")
    text = doc.text
    for s, e, new in reversed(eds):
        text = text[:s] + new + text[e:]
    m = offset_mapper(eds)
    d = doc.model_copy(deep=True)
    d.text = text

    lines = []
    for ln in d.lines:
        s, e = m(ln.start), m(ln.end)
        if e > s:
            ln.start, ln.end, ln.text = s, e, text[s:e]
            lines.append(ln)
    d.lines = lines
    paras = []
    for p in d.paragraphs:
        s, e = m(p.start), m(p.end)
        if e <= s or not text[s:e].strip():
            continue
        sents = [(m(a), m(b)) for a, b in p.sentences]
        p.start, p.end = s, e
        p.sentences = [(a, b) for a, b in sents if b > a and text[a:b].strip()]
        paras.append(p)
    d.paragraphs = paras
    for sec in d.sections:
        sec.start, sec.end = m(sec.start), m(sec.end)
    d.mentions = [mm for mm in d.mentions if m(mm.end) > m(mm.start)]
    for mm in d.mentions:
        mm.start, mm.end = m(mm.start), m(mm.end)
    for it in d.checklist:
        it.start, it.end = m(it.start), m(it.end)
    d.limitations_spans = [(m(a), m(b)) for a, b in d.limitations_spans if m(b) > m(a)]
    return d, m


# ---------------------------------------------------------------- helpers


def _role_at(doc: Document, offset: int) -> str | None:
    roles = [s.role for s in doc.sections if s.start <= offset < s.end]
    return roles[0] if roles else None


def _table_values(doc: Document) -> list[tuple[str, Cell]]:
    return [(t.id, c) for t in doc.tables if t.parsed for row in t.rows[t.header_rows:] or t.rows for c in row
            if c.value is not None]


def _in_table(value: float, cells: list[tuple[str, Cell]]) -> tuple[str, Cell] | None:
    for tid, c in cells:
        v = c.value
        if v is None:
            continue
        if abs(v - value) < 1e-9 or abs(v * 100 - value) < 1e-6:
            return tid, c
    return None


def _covering(result: RunResult, start: int, end: int) -> list:
    return [c for c in result.claims if c.location.start < end and start < c.location.end]


def _fmt(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}"


def _changed_number(raw: str, value: float, decimals: int, percentish: bool,
                    cells: list[tuple[str, Cell]] = ()) -> str:
    """x1.15 (or +3 points for percentages), moving further if the new value is printed in some table cell."""
    steps = ((3, -3, 4, -4, 5, -5, 6) if percentish else (1.15, 1.2, 0.85, 1.25, 0.8, 1.3))
    out = raw
    for st in steps:
        new = value + st if percentish else value * st
        if percentish and not 0 <= new <= 100:
            continue
        out = _fmt(new, decimals)
        if float(out) == value:  # too few decimals to show the change
            out = _fmt(value + 10 ** -decimals * max(1, round(abs(new - value) * 10 ** decimals)), decimals)
        if _in_table(float(out), list(cells)) is None:
            break
    if "," in raw and decimals == 0:
        out = f"{int(float(out)):,}"
    return out


def _body_has(doc: Document, pattern: re.Pattern[str]) -> bool:
    return any(pattern.search(doc.text[a:b]) for a, b in _sentences(doc, EXCLUDED_ROLES))


def _sentences(doc: Document, roles_excluded: set[str]) -> list[tuple[int, int]]:
    out = []
    for p in doc.paragraphs:
        if _role_at(doc, p.start) in roles_excluded:
            continue
        out += p.sentences or [(p.start, p.end)]
    return out


def _strip_inline(text: str) -> str:
    return ERR_INLINE.sub("", text)


def _clean_caption(caption: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", caption)
    keep = [s for s in parts if not ERR_SENTENCE.search(s)]
    return _strip_inline(" ".join(keep))


def match_claims(result: RunResult, span: tuple[int, int] | None, quote: str | None = None,
                 min_ratio: float = 85.0) -> list:
    """Claims of a (mutated) result at a target: overlapping the span, else matching the quote."""
    out = []
    if span is not None:
        out = [c for c in result.claims if c.location.start < span[1] and span[0] < c.location.end]
    if not out and quote:
        k = match_key(quote)
        out = [c for c in result.claims if fuzz.ratio(match_key(c.quote), k) >= min_ratio]
    return out


# ---------------------------------------------------------------- the five types


def _abstract_number(doc: Document, result: RunResult, rng: random.Random, mut: Mutation) -> Document | None:
    cells = _table_values(doc)
    cands = []
    for sec in doc.sections:
        if sec.role != "abstract":
            continue
        for mm in extract_numbers(doc.text[sec.start:sec.end]):
            hit = _in_table(mm.value, cells)
            if hit is None:
                continue
            s = sec.start + mm.start
            raw = re.match(r"[\d.,]+", doc.text[s:]).group(0)  # type: ignore[union-attr]
            covered = bool(_covering(result, s, s + len(raw)))
            cands.append((covered, s, raw, mm, hit))
    if not cands:
        mut.reason = "no abstract number appears in a parsed table"
        return None
    pool = [c for c in cands if c[0]] or cands
    _, s, raw, mm, (tid, cell) = rng.choice(pool)
    new = _changed_number(raw, mm.value, mm.decimals, mm.percentish or cell.percent, cells)
    d, m = apply_edits(doc, [(s, s + len(raw), new)])
    mut.original, mut.replacement, mut.table_id = raw, new, tid
    mut.span = (m(s), m(s) + len(new))
    mut.reason = f"abstract number {raw} -> {new} ({tid} holds {cell.raw})"
    return d


def _checklist_flip(doc: Document, result: RunResult, rng: random.Random, mut: Mutation) -> Document | None:
    items = [it for it in doc.checklist if it.key in FLIP_KEYS and it.answer.strip().lower() in NO_ANSWERS]
    # the paper must really lack what the item asks for
    lacks = {"open_access": not (doc.code.present or doc.code.repo_urls_in_paper),
             "error_bars": not _body_has(doc, ERR_SENTENCE)
             and not any(c.spread is not None for t in doc.tables for row in t.rows for c in row),
             "compute": not _body_has(doc, COMPUTE), "experimental_details": True}
    items = [it for it in items if lacks[it.key]]
    if not items:
        mut.reason = ("no No/NA answer on error_bars, open_access, compute or experimental_details that the paper "
                      "really lacks")
        return None
    it = rng.choice(sorted(items, key=lambda x: x.key))
    seg = doc.text[it.start:it.end]
    m_ans = re.search(r"\[\s*(No|NA|N/A)\s*\]", seg, re.IGNORECASE) or \
        re.search(r"(?<=Answer:)\s*(No|NA|N/A)\b", seg, re.IGNORECASE)
    edits: list[Edit] = []
    if m_ans:
        g = m_ans.span(1)
        edits.append((it.start + g[0], it.start + g[1], "Yes"))
    d, m = apply_edits(doc, edits)
    for x in d.checklist:
        if x.key == it.key:
            x.answer = "Yes"
    mut.checklist_key, mut.original, mut.replacement = it.key, it.answer, "Yes"
    mut.span = (m(it.start), m(it.end))
    mut.reason = f"checklist {it.key}: {it.answer} -> Yes"
    return d


def relying_claims(doc: Document, result: RunResult, table_id: str) -> list:
    out = []
    for c in result.claims:
        if c.type == "checklist":
            continue
        cites = any(mm.kind == "table" and mm.resolved_id == table_id and c.location.start <= mm.start < c.location.end
                    for mm in doc.mentions)
        sup = [e for e in c.evidence if e.relation == "supports"]
        only = bool(sup) and all(e.kind in ("table", "table_cell") and e.label.split(",")[0] == table_id for e in sup)
        if cites or only:
            out.append(c)
    return out


def _table_text_paragraphs(doc: Document, table) -> list[tuple[int, int]]:
    raws = {c.raw.strip() for row in table.rows for c in row if c.raw.strip()}
    cap = " ".join(table.caption.split())[:80]
    out = []
    for p in doc.paragraphs:
        if p.page != table.page:
            continue
        txt = " ".join(doc.text[p.start:p.end].split())
        if cap and txt.startswith(cap[:40]):
            out.append((p.start, p.end))
            continue
        toks = txt.split()
        if len(toks) >= 4:
            hit = sum(1 for t in toks if t in raws or t.strip("()%,") in raws)
            if hit / len(toks) >= 0.5:
                out.append((p.start, p.end))
    return out


def _remove_table(doc: Document, result: RunResult, rng: random.Random, mut: Mutation) -> Document | None:
    cands = [(t, relying_claims(doc, result, t.id)) for t in doc.tables if t.parsed]
    cands = [(t, cl) for t, cl in cands if cl]
    if not cands:
        mut.reason = "no parsed table that a claim relies on"
        return None
    t, claims = rng.choice(sorted(cands, key=lambda x: x[0].id))
    edits = [(s, e, "") for s, e in _table_text_paragraphs(doc, t)]
    d, m = apply_edits(doc, edits)
    d.tables = [x for x in d.tables if x.id != t.id]
    for mm in d.mentions:
        if mm.resolved_id == t.id:
            mm.resolved_id = None
    mut.table_id = t.id
    mut.target_claims = [{"quote": c.quote, "span": (m(c.location.start), m(c.location.end))} for c in claims]
    mut.reason = f"removed {t.id} ({len(edits)} text block(s)); {len(claims)} claim(s) rely on it"
    return d


class WidenOut(BaseModel):
    rewritten: str = ""


def widen_candidates(doc: Document, result: RunResult) -> list:
    return [c for c in result.claims
            if c.type in ("result", "comparison") and c.wording.scope == "specific" and c.wording.strength != "absolute"
            and c.verdict == "supported" and 40 <= len(c.quote) <= 400
            and _role_at(doc, c.location.start) not in EXCLUDED_ROLES]


async def _widen_scope(doc: Document, result: RunResult, rng: random.Random, mut: Mutation, llm) -> Document | None:
    cands = widen_candidates(doc, result)
    if not cands:
        mut.reason = "no specific, supported result or comparison claim"
        return None
    c = rng.choice(sorted(cands, key=lambda x: x.location.start))
    original = doc.text[c.location.start:c.location.end]
    if llm is None:
        mut.reason = "no LLM client for the rewrite"
        return None
    try:
        out, _ = await llm.complete_json("widen_scope", {"sentence": original}, WidenOut, run_index=0,
                                         paper_id=mut.mutated_id)
    except LLMError as e:
        mut.reason = f"rewrite failed: {str(e)[:120]}"
        mut.error = True
        return None
    new = " ".join(out.rewritten.split())
    lex = load_lexicon()
    wider = lex.scope_of(new)[0] in ("universal", "general") or lex.strength_of(new)[0] == "absolute"
    if not new or match_key(new) == match_key(original) or not wider \
            or not 0.6 <= len(new) / max(len(original), 1) <= 1.8:
        mut.reason = "rewrite failed verification (not universal, unchanged or out of length bounds)"
        return None
    s = c.location.start
    d, m = apply_edits(doc, [(s, c.location.end, new)])
    mut.original, mut.replacement = original, new
    mut.span = (m(s), m(s) + len(new))
    mut.target_claims = [{"quote": new, "span": mut.span}]
    mut.reason = "claim rewritten to universal wording"
    return d


def _delete_errorbars(doc: Document, result: RunResult, rng: random.Random, mut: Mutation) -> Document | None:
    item = next((it for it in doc.checklist if it.key == "error_bars"), None)
    if item is None or item.answer.strip().lower() != "yes":
        mut.reason = "error_bars is not answered Yes"
        return None
    sents = [(a, b) for a, b in _sentences(doc, EXCLUDED_ROLES) if ERR_SENTENCE.search(doc.text[a:b])]
    has_cells = any(c.spread is not None for t in doc.tables for row in t.rows for c in row)
    if not sents and not has_cells:
        mut.reason = "the paper reports no spreads or significance information"
        return None
    edits: list[Edit] = [(a, b, "") for a, b in sents]
    covered = sorted(edits)
    # inline spreads in the remaining body text (table grids, captions)
    for p in doc.paragraphs:
        if _role_at(doc, p.start) in EXCLUDED_ROLES:
            continue
        for mm in ERR_INLINE.finditer(doc.text, p.start, p.end):
            a, b = mm.span()
            if not any(s <= a < e or s < b <= e for s, e, _ in covered):
                edits.append((a, b, ""))
    edits = sorted(set(edits))
    merged: list[Edit] = []
    for e in edits:
        if merged and e[0] < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e[1]), "")
        else:
            merged.append(e)
    d, _ = apply_edits(doc, merged)
    for t in d.tables:
        t.caption = _clean_caption(t.caption)
        for row in t.rows:
            for c in row:
                if c.spread is not None or ERR_INLINE.search(c.raw):
                    c.spread = None
                    c.raw = _strip_inline(c.raw).strip()
    for f in d.figures:
        f.caption = _clean_caption(f.caption)
    mut.checklist_key = "error_bars"
    mut.reason = f"removed {len(sents)} sentence(s) and {len(merged) - len(sents)} inline spread(s)"
    return d


async def mutate(doc: Document, result: RunResult, mtype: str, seed: int, llm=None) -> tuple[Mutation, Document | None]:
    """Apply one mutation type. Returns the record and the mutated Document (None when not applicable)."""
    if mtype not in TYPES:
        raise ValueError(f"unknown mutation type {mtype!r}")
    rng = random.Random(f"{seed}:{doc.paper_id}:{mtype}")
    mut = Mutation(paper_id=doc.paper_id, type=mtype, seed=seed, applied=False)
    if mtype == "widen_scope":
        d = await _widen_scope(doc, result, rng, mut, llm)
    else:
        d = {"abstract_number": _abstract_number, "checklist_flip": _checklist_flip, "remove_table": _remove_table,
             "delete_errorbars": _delete_errorbars}[mtype](doc, result, rng, mut)
    if d is not None:
        mut.applied = True
        d.paper_id = mut.mutated_id
    return mut, d


def table_renderings(doc: Document, limit: int = 15000) -> str:
    out, n = [], 0
    for t in doc.tables:
        if not t.parsed:
            continue
        r = render_table(t)
        if n + len(r) > limit:
            break
        out.append(r)
        n += len(r)
    return "\n\n".join(out)
