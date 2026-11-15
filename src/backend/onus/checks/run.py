"""Phase 5 orchestrator: every check for one paper -> `CheckSet` (`var/checks/<id>.r<N>.json`).

Per non-checklist claim: number recheck and dominance (in parallel), then the strength rubric (which uses the
dominance outcome). Then the scope check for the eligible claims against the verified limitations, and the
checklist audit for the paper. Each claim gets one `CheckResult` per check in `CLAIM_CHECKS`; a check that does
not apply is `not_run` with its reason, and the ledger counts those reasons.
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from pathlib import Path

from onus.claims.models import ClaimSet
from onus.evidence.models import EvidenceSet
from onus.llm.client import CallMeta
from onus.schemas.document import Document
from onus.schemas.result import ChecklistAudit, LedgerEntry

from .checklist_audit import audit_checklist
from .context import PaperContext, claim_context
from .dominance import check_dominance
from .models import CLAIM_CHECKS, CheckSet, ClaimChecks
from .numbers import check_numbers
from .scope import check_scope, extract_limitations
from .strength import PaperFeatures, check_strength

CHECKLIST_REASON = "checklist claim (checked by the checklist audit)"


def _cost(metas: list[CallMeta]) -> dict:
    by_prompt: dict[str, float] = defaultdict(float)
    for m in metas:
        by_prompt[m.prompt_id] += m.eur
    return {"eur": round(sum(m.eur for m in metas), 6), "calls": sum(m.calls for m in metas),
            "cache_hits": sum(m.cache_hits for m in metas), "tokens_in": sum(m.tokens_in for m in metas),
            "tokens_out": sum(m.tokens_out for m in metas),
            "by_prompt": {k: round(v, 6) for k, v in sorted(by_prompt.items())}}


def build_ledger(claims: list[ClaimChecks], n_checklist_claims: int, audits: list[ChecklistAudit],
                 derived: list[str]) -> list[LedgerEntry]:
    out = []
    for name in CLAIM_CHECKS:
        results = [c.get(name) for c in claims]
        reasons = Counter(r.data.get("reason", r.detail) for r in results if r is not None and r.outcome == "not_run")
        if n_checklist_claims:
            reasons[CHECKLIST_REASON] += n_checklist_claims
        ran = sum(1 for r in results if r is not None and r.outcome != "not_run")
        out.append(LedgerEntry(check=name, ran_on=ran, not_run_on=sum(reasons.values()), reasons=dict(reasons)))
    na = sum(1 for a in audits if a.status == "not_applicable")
    reasons = {}
    if na:
        reasons["answered No or NA"] = na
    if derived:
        reasons["derived after verdicts"] = len(derived)
    out.append(LedgerEntry(check="checklist", ran_on=len(audits) - na - len(derived), not_run_on=na + len(derived),
                           reasons=reasons))
    return out


async def run_checks(doc: Document, claims: ClaimSet, evidence: EvidenceSet, llm=None, *,
                     run_index: int | None = None, fingerprint: str | None = None) -> CheckSet:
    if llm is None:
        from onus.llm.client import get_client

        llm = get_client()
    if fingerprint is None:
        from onus.version import fingerprint as _fp

        fingerprint = _fp()
    run = claims.run_index if run_index is None else run_index
    pc = PaperContext(doc)
    pf = PaperFeatures(pc)
    metas: list[CallMeta] = []
    stats: dict = defaultdict(int)
    kw = {"run_index": run, "paper_id": doc.paper_id, "metas": metas}

    targets = [c for c in claims.claims if c.type != "checklist"]
    ctxs = [claim_context(pc, c, evidence.for_claim(c.id)) for c in targets]
    lim_task = asyncio.create_task(extract_limitations(pc, llm, run_index=run, metas=metas, stats=stats))

    async def per_claim(ctx):
        num, dom = await asyncio.gather(check_numbers(ctx, llm, **kw), check_dominance(ctx, llm, **kw))
        strength, rub = await check_strength(ctx, pf, dom, llm, **kw)
        return num, dom, strength, rub

    per = await asyncio.gather(*[per_claim(c) for c in ctxs])
    lims, lim_reason = await lim_task
    scope = await check_scope(ctxs, lims, lim_reason, llm, stats=stats, **kw)
    audits, derived = await audit_checklist(pc, llm, run_index=run, metas=metas, stats=stats)

    out = [ClaimChecks(claim_id=ctx.claim.id, checks=[num, dom, strength, scope[ctx.claim.id]], rubric=rub)
           for ctx, (num, dom, strength, rub) in zip(ctxs, per, strict=True)]
    ledger = build_ledger(out, len(claims.claims) - len(targets), audits, derived)

    warnings = []
    if lim_reason and "failed" in lim_reason:
        warnings.append(lim_reason)
    for name in CLAIM_CHECKS:
        oc = Counter(c.get(name).outcome for c in out)  # type: ignore[union-attr]
        stats[name] = dict(oc)
        stats[f"{name}_fail_severity"] = dict(Counter(c.get(name).severity for c in out  # type: ignore[union-attr]
                                                      if c.get(name).outcome == "fail"))  # type: ignore[union-attr]
    stats["checklist"] = dict(Counter(a.status for a in audits))
    stats["limitations"] = len(lims)
    stats["claims"] = len(out)
    failed_llm = sum(1 for c in out for r in c.checks if r.outcome == "inconclusive" and "error" in r.data)
    if failed_llm:
        warnings.append(f"{failed_llm} check call(s) failed; those checks are inconclusive")
    return CheckSet(paper_id=doc.paper_id, run_index=run, fingerprint=fingerprint, claims=out, checklist=audits,
                    derived_checklist=derived, limitations=lims, ledger=ledger, stats=dict(stats), cost=_cost(metas),
                    warnings=warnings)


# ---------------------------------------------------------------- persistence


def checks_path(var_dir: Path, paper_id: str, run_index: int = 0) -> Path:
    from onus.ingest.document_builder import safe_id

    return var_dir / "checks" / f"{safe_id(paper_id)}.r{run_index}.json"


def save_checks(ks: CheckSet, var_dir: Path) -> Path:
    p = checks_path(var_dir, ks.paper_id, ks.run_index)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ks.model_dump_json(indent=1))
    return p


def load_checks(path: Path) -> CheckSet:
    return CheckSet.model_validate_json(path.read_text())
