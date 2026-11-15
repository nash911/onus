"""Evidence selection and the Phase 4 orchestrator. (F3)

Per non-checklist claim:
1. Explicit references (code): objects cited in the claim ±1 sentence; unresolved ones are dangling.
2. Retrieval (code): BM25 top 10 ∪ embedding top 10, capped at 12, explicit objects excluded.
3. Prompt `select_evidence` (extract role) gives `{evidence_id, relation, note}` for the candidates only.
   Ids outside the list are dropped and counted. Explicit links are always kept and get the model's
   relation (None if it gave none). Retrieved candidates are kept when the model gives a relation other than
   "unrelated" (`link_method="llm_selected"`).
4. If selection fails, the explicit links and the top 3 retrieved candidates are kept with no relation
   (`link_method="retrieval"`), and the `link` check is `inconclusive`.

Checklist claims are not linked here: the checklist audit (Phase 5) handles them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from onus.claims.models import ClaimSet, DraftClaim
from onus.claims.quotes import QuoteVerifier
from onus.llm.client import CallMeta, LLMError
from onus.schemas.document import Document
from onus.schemas.result import CheckResult, Evidence, LedgerEntry

from .explicit import ObjectIndex, explicit_refs
from .models import Candidate, ClaimEvidence, EvidenceSet
from .retrieve import CandidatePool, embed_queries

FALLBACK_KEEP = 3
MAX_EVIDENCE = 6  # kept per claim, cited objects always included
RELATIONS = ("supports", "partial", "contradicts", "unrelated")
_REL_ORDER = {"supports": 0, "partial": 1, "contradicts": 2, "unrelated": 3, None: 4}


class SelectedItem(BaseModel):
    evidence_id: str
    relation: str = "unrelated"
    note: str = ""


class SelectOut(BaseModel):
    items: list[SelectedItem] = Field(default_factory=list)


def render_candidates(cands: list[Candidate], cited: set[str]) -> str:
    blocks = []
    for i, c in enumerate(cands, 1):
        tag = " [cited]" if c.key in cited else ""
        blocks.append(f"[E{i}]{tag} {c.label}\n{c.text}")
    return "\n\n".join(blocks)


def _norm_relation(r: str) -> str | None:
    r = r.strip().lower()
    r = {"support": "supports", "supported": "supports", "contradict": "contradicts",
         "contradicted": "contradicts", "partially": "partial", "partly": "partial", "none": "unrelated",
         "irrelevant": "unrelated", "not_related": "unrelated"}.get(r, r)
    return r if r in RELATIONS else None


def _link_check(ev: list[Evidence], dangling: list[str], n_cands: int, failed: bool) -> CheckResult:
    sup = [e for e in ev if e.relation == "supports"]
    par = [e for e in ev if e.relation == "partial"]
    con = [e for e in ev if e.relation == "contradicts"]
    data = {"supports": len(sup), "partial": len(par), "contradicts": len(con), "candidates": n_cands,
            "dangling": dangling}
    ids = [e.id for e in sup + par + con]
    if failed:
        return CheckResult(check="link", outcome="inconclusive", detail="Evidence selection failed; candidates "
                           "were found but not judged.", data=data, evidence_ids=[e.id for e in ev])
    if sup or par:
        names = ", ".join(e.label for e in (sup + par)[:3])
        return CheckResult(check="link", outcome="pass", detail=f"{len(sup)} supporting and {len(par)} partial "
                           f"evidence item(s): {names}.", data=data, evidence_ids=ids)
    if dangling:
        return CheckResult(check="link", outcome="fail", detail=f"Cites {', '.join(dangling)}, which is not in "
                           "the paper; no other evidence located.", data=data, evidence_ids=ids)
    if con:
        return CheckResult(check="link", outcome="fail", detail="Only contradicting evidence located: "
                           + ", ".join(e.label for e in con[:3]) + ".", data=data, evidence_ids=ids)
    what = f"none of {n_cands} candidates" if n_cands else "no candidates in the paper's body"
    return CheckResult(check="link", outcome="fail", detail=f"No supporting evidence located ({what}).",
                       data=data, evidence_ids=ids)


async def _link_one(claim: DraftClaim, index: ObjectIndex, pool: CandidatePool, qvec: list[float] | None, llm,
                    run_index: int, paper_id: str, stats: dict, metas: list[CallMeta]) -> tuple[ClaimEvidence,
                                                                                            list[Evidence]]:
    start, end = claim.location.start, claim.location.end
    explicit, dangling = explicit_refs(index, start, end)
    cited = {c.key for c in explicit}
    retrieved, _ = pool.retrieve(claim.quote, qvec, start, end)
    retrieved = [c for c in retrieved if c.key not in cited]
    cands = explicit + retrieved

    picked: list[tuple[Candidate, str, str | None, str]] = []  # (candidate, link_method, relation, note)
    failed = False
    if cands:
        try:
            out, meta = await llm.complete_json(
                "select_evidence",
                {"claim": claim.quote, "section": claim.location.section or "the paper",
                 "candidates": render_candidates(cands, cited)},
                SelectOut, run_index=run_index, paper_id=paper_id)
            metas.append(meta)
            seen: dict[str, tuple[str | None, str]] = {}
            for it in out.items:
                eid = it.evidence_id.strip().strip("[]").upper()
                if not eid.startswith("E") or not eid[1:].isdigit() or not 1 <= int(eid[1:]) <= len(cands):
                    stats["invalid_ids"] += 1
                    continue
                key = cands[int(eid[1:]) - 1].key
                if key not in seen:
                    seen[key] = (_norm_relation(it.relation), it.note.strip())
            for c in explicit:
                rel, note = seen.get(c.key, (None, ""))
                picked.append((c, "explicit_ref", rel, note))
            for c in retrieved:
                if c.key in seen and seen[c.key][0] not in (None, "unrelated"):
                    picked.append((c, "llm_selected", *seen[c.key]))
        except LLMError:
            failed = True
            stats["select_failed"] += 1
            picked = [(c, "explicit_ref", None, "") for c in explicit]
            picked += [(c, "retrieval", None, "") for c in retrieved[:FALLBACK_KEEP]]

    picked.sort(key=lambda x: (_REL_ORDER[x[2]], x[1] != "explicit_ref"))
    n_explicit = sum(1 for x in picked if x[1] == "explicit_ref")
    room = max(MAX_EVIDENCE - n_explicit, 0)
    kept_other = 0
    capped = []
    for x in picked:
        if x[1] == "explicit_ref" or kept_other < room:
            capped.append(x)
            kept_other += x[1] != "explicit_ref"
        else:
            stats["over_cap"] += 1
    picked = capped
    evidence = [c.evidence("", method, rel) for c, method, rel, _ in picked]
    ce = ClaimEvidence(claim_id=claim.id, explicit=[c.label for c in explicit], dangling=dangling,
                       candidates=[c.key for c in cands], select_failed=failed,
                       link=CheckResult(check="link", outcome="not_run", detail=""))
    ce.notes = {str(i): note for i, (_, _, _, note) in enumerate(picked) if note}  # re-keyed after ids
    return ce, evidence


def _cost(metas: list[CallMeta]) -> dict:
    return {"eur": round(sum(m.eur for m in metas), 6), "calls": sum(m.calls for m in metas),
            "cache_hits": sum(m.cache_hits for m in metas), "tokens_in": sum(m.tokens_in for m in metas),
            "tokens_out": sum(m.tokens_out for m in metas), "note": "chat calls only; embeddings in the ledger"}


async def link_evidence(doc: Document, claims: ClaimSet, llm=None, *, run_index: int | None = None,
                        fingerprint: str | None = None) -> EvidenceSet:
    """Phase 4 for one paper: evidence for every non-checklist claim of `claims`."""
    if llm is None:
        from onus.llm.client import get_client

        llm = get_client()
    if fingerprint is None:
        from onus.version import fingerprint as _fp

        fingerprint = _fp()
    run = claims.run_index if run_index is None else run_index
    index = ObjectIndex(doc, QuoteVerifier(doc))
    pool = CandidatePool(doc, index)
    warnings: list[str] = []
    stats = {"invalid_ids": 0, "select_failed": 0, "over_cap": 0}
    metas: list[CallMeta] = []

    targets = [c for c in claims.claims if c.type != "checklist"]
    err = await pool.embed_pool(llm)
    qvecs, qerr = await embed_queries(llm, [c.quote for c in targets], doc.paper_id) if err is None else (None, err)
    if err or qerr:
        warnings.append(f"embedding failed, BM25 only: {err or qerr}")
    linked = await asyncio.gather(*[
        _link_one(c, index, pool, qvecs[i] if qvecs else None, llm, run, doc.paper_id, stats, metas)
        for i, c in enumerate(targets)
    ])

    out: list[ClaimEvidence] = []
    n = 0
    methods = {"explicit_ref": 0, "retrieval": 0, "llm_selected": 0}
    relations = {r: 0 for r in (*RELATIONS, "none")}
    for ce, evidence in linked:
        notes = {}
        for i, e in enumerate(evidence):
            n += 1
            e.id = f"ev{n}"
            if str(i) in ce.notes:
                notes[e.id] = ce.notes[str(i)]
            methods[e.link_method] += 1
            relations[e.relation or "none"] += 1
        ce.evidence = evidence
        ce.notes = notes
        ce.link = _link_check(evidence, ce.dangling, len(ce.candidates), ce.select_failed)
        out.append(ce)
    if stats["select_failed"]:
        warnings.append(f"select_evidence failed on {stats['select_failed']} claim(s); kept unjudged candidates")

    not_run = len(claims.claims) - len(targets)
    ledger = LedgerEntry(check="link", ran_on=len(targets), not_run_on=not_run,
                         reasons={"checklist claim (checked by the checklist audit)": not_run} if not_run else {})
    stats |= {
        "claims": len(targets),
        "pool": len(pool.candidates),
        "with_support": sum(1 for c in out if c.link.outcome == "pass"),
        "no_support": sum(1 for c in out if c.link.outcome == "fail"),
        "with_contradicts": sum(1 for c in out if any(e.relation == "contradicts" for e in c.evidence)),
        "dangling_claims": sum(1 for c in out if c.dangling),
        "no_candidates": sum(1 for c in out if not c.candidates),
        "with_explicit": sum(1 for c in out if c.explicit),
        "link_methods": methods,
        "relations": relations,
    }
    return EvidenceSet(paper_id=doc.paper_id, run_index=run, fingerprint=fingerprint, claims=out, ledger=ledger,
                       stats=stats, cost=_cost(metas), warnings=warnings)


# ---------------------------------------------------------------- persistence


def evidence_path(var_dir: Path, paper_id: str, run_index: int = 0) -> Path:
    from onus.ingest.document_builder import safe_id

    return var_dir / "evidence" / f"{safe_id(paper_id)}.r{run_index}.json"


def save_evidence(es: EvidenceSet, var_dir: Path) -> Path:
    p = evidence_path(var_dir, es.paper_id, es.run_index)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(es.model_dump_json(indent=1))
    return p


def load_evidence(path: Path) -> EvidenceSet:
    return EvidenceSet.model_validate_json(path.read_text())
