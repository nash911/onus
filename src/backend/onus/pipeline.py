"""analyze_document / analyze_paper: the full claim-map pipeline. (Phase 6)

One run: claims (F2) -> evidence (F3) -> checks (F4-F7) -> rule verdicts (B.5) -> adjudication of every
non-supported claim -> the derived "claims" checklist item. `analyze_paper` ingests once, does `runs` runs
(run indexes `run_start ...`, each its own cache key), merges them by claim clustering and majority verdict,
renumbers evidence ids so they are unique in the result, and fills the ledger, not-checked list, summary,
cost and timings.

Checklist claims are not adjudicated: their verdict is the checklist audit, which already verified its quotes.
Progress callbacks: `progress(stage, info)` with stage in ingest, claims, evidence, checks, verdicts, done.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from onus.checks.models import CheckSet
from onus.checks.run import run_checks
from onus.claims.extract import extract_claims
from onus.claims.models import ClaimSet, DraftClaim
from onus.evidence.models import EvidenceSet
from onus.evidence.select import link_evidence
from onus.llm.client import CallMeta
from onus.schemas.document import Document
from onus.schemas.result import (
    RISK,
    ChecklistAudit,
    Claim,
    CodeReport,
    Evidence,
    RunResult,
)
from onus.verdict.adjudicate import adjudicate
from onus.verdict.ledger import build_ledger, not_checked
from onus.verdict.rules import RuleVerdict, audit_check, checklist_verdict, rule_verdict
from onus.verdict.stability import merge_runs

Progress = Callable[[str, dict], None] | None
STAGES = ("ingest", "claims", "evidence", "checks", "verdicts")
FLAGGED = {"contradicted", "overstated", "untraced"}


def _emit(progress: Progress, stage: str, **info) -> None:
    if progress is not None:
        progress(stage, info)


@dataclass
class RunOutput:
    run_index: int
    claims: list[Claim]
    audits: list[ChecklistAudit]
    claimset: ClaimSet
    evidence: EvidenceSet
    checks: CheckSet
    cost: dict
    timings: dict
    warnings: list[str] = field(default_factory=list)


def _meta_cost(metas: list[CallMeta]) -> dict:
    return {"eur": sum(m.eur for m in metas), "calls": sum(m.calls for m in metas),
            "cache_hits": sum(m.cache_hits for m in metas), "tokens_in": sum(m.tokens_in for m in metas),
            "tokens_out": sum(m.tokens_out for m in metas)}


def _sum_cost(parts: dict[str, dict]) -> dict:
    out = {"eur": 0.0, "calls": 0, "cache_hits": 0, "tokens_in": 0, "tokens_out": 0}
    for p in parts.values():
        for k in out:
            out[k] += p.get(k, 0) or 0
    out["eur"] = round(out["eur"], 6)
    return out | {"by_stage": {k: round(v.get("eur", 0.0), 6) for k, v in parts.items()},
                  "note": "chat calls; embeddings (≈ €0.0002 per paper) are in the cost ledger only"}


def own_dangling(doc: Document, claim: DraftClaim, dangling: list[str]) -> list[str]:
    """Dangling references whose mention lies inside the claim's own quote."""
    if not dangling:
        return []
    s, e = claim.location.start, claim.location.end
    return list(dict.fromkeys(m.label for m in doc.mentions if s <= m.start < e and m.label in dangling))


async def verdicts_for_run(doc: Document, cs: ClaimSet, es: EvidenceSet, ks: CheckSet, llm, *, run_index: int,
                           metas: list[CallMeta]) -> tuple[list[Claim], list[ChecklistAudit]]:
    regular = [c for c in cs.claims if c.type != "checklist"]

    async def one(c: DraftClaim) -> Claim:
        ce = es.for_claim(c.id)
        cc = ks.for_claim(c.id)
        evidence = list(ce.evidence) if ce else []
        checks = ([ce.link] if ce else []) + (list(cc.checks) if cc else [])
        rv = rule_verdict(evidence, checks, own_dangling(doc, c, ce.dangling if ce else []))
        verdict, reason, caveat, adj = rv.verdict, rv.reason, rv.caveat, None
        if rv.verdict != "supported":
            verdict, reason, caveat, adj = await adjudicate(
                quote=c.quote, section=c.location.section, wording=c.wording, evidence=evidence, checks=checks,
                rule=rv, llm=llm, run_index=run_index, paper_id=doc.paper_id, metas=metas)
        return Claim(id=c.id, type=c.type, quote=c.quote, location=c.location, is_main=c.is_main, wording=c.wording,
                     evidence=evidence, checks=checks, verdict=verdict, reason=reason, caveat=caveat,
                     risk=RISK[verdict], checklist_key=c.checklist_key, rule_verdict=rv.verdict, adjudication=adj)

    claims = list(await asyncio.gather(*[one(c) for c in regular]))

    audits = [a.model_copy() for a in ks.checklist]
    contradicted_main = [c for c, d in zip(claims, regular, strict=True)
                         if c.verdict == "contradicted" and c.is_main and d.section_role == "abstract"]
    for i, a in enumerate(audits):
        if a.key in ks.derived_checklist:
            if contradicted_main:
                audits[i] = a.model_copy(update={"status": "unbacked", "reason": (
                    f"{len(contradicted_main)} main claim(s) in the abstract are contradicted by the paper's own "
                    "results.")})
            else:
                audits[i] = a.model_copy(update={"status": "backed", "reason": "No main claim in the abstract is "
                                                                              "contradicted by the paper's results."})
    by_key = {a.key: a for a in audits}
    for c in cs.claims:
        if c.type != "checklist":
            continue
        audit = by_key.get(c.checklist_key or "")
        rv: RuleVerdict | None = checklist_verdict(audit)
        if rv is None:
            continue  # answered No or NA: excluded
        claims.append(Claim(id=c.id, type=c.type, quote=c.quote, location=c.location, is_main=c.is_main,
                            wording=c.wording, evidence=list(audit.evidence) if audit else [],
                            checks=[audit_check(audit)] if audit else [], verdict=rv.verdict, reason=rv.reason,
                            caveat=rv.caveat, risk=RISK[rv.verdict], checklist_key=c.checklist_key,
                            rule_verdict=rv.verdict))
    return claims, audits


async def analyze_document(doc: Document, run_index: int = 0, mode: str = "batch", *, llm=None,
                           fingerprint: str | None = None, progress: Progress = None) -> RunOutput:
    """One run over an ingested Document."""
    if llm is None:
        from onus.llm.client import get_client

        llm = get_client()
    if fingerprint is None:
        from onus.version import fingerprint as _fp

        fingerprint = _fp()
    timings: dict[str, float] = {}
    t = time.monotonic()
    _emit(progress, "claims", run=run_index)
    cs = await extract_claims(doc, llm, run_index=run_index, fingerprint=fingerprint)
    timings["claims_s"] = time.monotonic() - t
    t = time.monotonic()
    _emit(progress, "evidence", run=run_index, claims=len(cs.claims))
    es = await link_evidence(doc, cs, llm, run_index=run_index, fingerprint=fingerprint)
    timings["evidence_s"] = time.monotonic() - t
    t = time.monotonic()
    _emit(progress, "checks", run=run_index)
    ks = await run_checks(doc, cs, es, llm, run_index=run_index, fingerprint=fingerprint)
    timings["checks_s"] = time.monotonic() - t
    t = time.monotonic()
    _emit(progress, "verdicts", run=run_index)
    metas: list[CallMeta] = []
    claims, audits = await verdicts_for_run(doc, cs, es, ks, llm, run_index=run_index, metas=metas)
    timings["verdicts_s"] = time.monotonic() - t
    cost = {"claims": cs.cost, "evidence": es.cost, "checks": ks.cost, "verdicts": _meta_cost(metas)}
    warnings = [f"claims: {w}" for w in cs.warnings] + [f"evidence: {w}" for w in es.warnings] + \
        [f"checks: {w}" for w in ks.warnings]
    failed_adj = sum(1 for c in claims if c.adjudication and c.adjudication.get("decision") is None)
    if failed_adj:
        warnings.append(f"verdicts: adjudication failed on {failed_adj} claim(s); rule verdicts stand")
    return RunOutput(run_index=run_index, claims=claims, audits=audits, claimset=cs, evidence=es, checks=ks,
                     cost=cost, timings=timings, warnings=warnings)


# ---------------------------------------------------------------- merging and assembly


def merge_audits(runs: list[list[ChecklistAudit]]) -> list[ChecklistAudit]:
    """Majority status per item across runs (ties -> the first run's status); the shown audit is the first run's
    audit with that status."""
    if len(runs) == 1:
        return runs[0]
    out = []
    for a0 in runs[0]:
        same = [a for r in runs for a in r if a.key == a0.key]
        counts = Counter(a.status for a in same)
        top = max(counts.values())
        status = a0.status if counts[a0.status] == top else next(s for s, n in counts.items() if n == top)
        out.append(next(a for a in same if a.status == status))
    return out


def renumber(claims: list[Claim]) -> list[Claim]:
    """Evidence ids ev1, ev2, ... unique across the result; check and adjudication references follow."""
    n = 0
    out = []
    for c in claims:
        mapping: dict[str, str] = {}
        ev: list[Evidence] = []
        for e in c.evidence:
            if e.id.startswith("ev"):
                n += 1
                mapping[e.id] = f"ev{n}"
                ev.append(e.model_copy(update={"id": mapping[e.id]}))
            else:
                ev.append(e)
        checks = [r.model_copy(update={"evidence_ids": [mapping.get(i, i) for i in r.evidence_ids
                                                        if i in mapping or not i.startswith("ev")]})
                  for r in c.checks]
        adj = c.adjudication
        if adj and adj.get("evidence_ids"):
            adj = adj | {"evidence_ids": [mapping[i] for i in adj["evidence_ids"] if i in mapping]}
        out.append(c.model_copy(update={"evidence": ev, "checks": checks, "adjudication": adj}))
    return out


def summarise(claims: list[Claim], audits: list[ChecklistAudit]) -> dict:
    counts = Counter(c.verdict for c in claims)
    main = [c for c in claims if c.is_main]
    stab = [c.stability.agree / c.stability.runs for c in claims if c.stability]
    return {
        "counts": {v: counts.get(v, 0) for v in RISK},
        "n_claims": len(claims),
        "n_checklist_claims": sum(1 for c in claims if c.type == "checklist"),
        "flagged_share": round(sum(1 for c in claims if c.verdict in FLAGGED) / len(claims), 4) if claims else 0.0,
        "main_flagged_share": round(sum(1 for c in main if c.verdict in FLAGGED) / len(main), 4) if main else 0.0,
        "n_main": len(main),
        "checklist": dict(Counter(a.status for a in audits)),
        "stability_mean": round(sum(stab) / len(stab), 4) if stab else None,
        "adjudicated": sum(1 for c in claims if c.adjudication),
        "downgraded": sum(1 for c in claims if c.rule_verdict and c.rule_verdict != c.verdict),
    }


def assemble(doc: Document, outs: list[RunOutput], *, mode: str, fingerprint: str, ingest_s: float,
             wall_s: float) -> RunResult:
    from onus.config import get_settings
    from onus.version import ONUS_VERSION

    merged, unstable = merge_runs([o.claims for o in outs])
    merged.sort(key=lambda c: (-c.risk, c.location.start))
    claims = renumber(merged)
    audits = merge_audits([o.audits for o in outs])
    first = outs[0]
    dropped = Counter(d.reason for d in first.claimset.dropped)
    lim_reason = None if first.checks.limitations else "no verified limitation statements in the paper"
    cost_parts: dict[str, dict] = {}
    for o in outs:
        for stage, c in o.cost.items():
            acc = cost_parts.setdefault(stage, {})
            for k in ("eur", "calls", "cache_hits", "tokens_in", "tokens_out"):
                acc[k] = acc.get(k, 0) + (c.get(k, 0) or 0)
    timings: dict[str, float] = {"ingest_s": round(ingest_s, 2)}
    for o in outs:
        for k, v in o.timings.items():
            timings[k] = round(timings.get(k, 0.0) + v, 2)
    timings["wall_s"] = round(wall_s, 2)
    warnings = list(dict.fromkeys(w for o in outs for w in o.warnings))
    return RunResult(
        onus_version=ONUS_VERSION, fingerprint=fingerprint, models=get_settings().models_config, mode=mode,  # type: ignore[arg-type]
        runs=len(outs),
        paper={"id": doc.paper_id, "source": doc.source, "external_id": None, "title": doc.title,
               "pdf_sha256": doc.pdf_sha256, "pages": doc.pages},
        created_at=datetime.now(UTC), claims=claims, checklist=audits,
        code=CodeReport(available=doc.code.present, files=len(doc.code.files)),
        ledger=build_ledger(claims, audits, mode),
        not_checked=not_checked(doc, mode=mode, dropped_quotes=dropped, unstable=unstable, runs=len(outs),
                                limitations_reason=lim_reason),
        summary=summarise(claims, audits), cost=_sum_cost(cost_parts), timings=timings, warnings=warnings)


def load_or_ingest(paper_dir: Path, var_dir: Path, refresh: bool = False) -> Document:
    from onus.ingest.document_builder import doc_path, ingest_paper_dir, load_document, save_document

    p = doc_path(var_dir, paper_dir.name)
    if p.is_file() and not refresh and p.stat().st_mtime >= (paper_dir / "paper.pdf").stat().st_mtime:
        return load_document(p)
    doc = ingest_paper_dir(paper_dir, var_dir)
    save_document(doc, var_dir)
    return doc


async def analyze_doc_runs(doc: Document, runs: int = 3, mode: str = "batch", *, run_start: int = 0, llm=None,
                           fingerprint: str | None = None, progress: Progress = None,
                           ingest_s: float = 0.0) -> RunResult:
    if fingerprint is None:
        from onus.version import fingerprint as _fp

        fingerprint = _fp()
    if mode == "live":
        runs = 1
    t0 = time.monotonic()
    outs = await asyncio.gather(*[analyze_document(doc, run_start + r, mode, llm=llm, fingerprint=fingerprint,
                                                   progress=progress) for r in range(runs)])
    result = assemble(doc, list(outs), mode=mode, fingerprint=fingerprint, ingest_s=ingest_s,
                      wall_s=time.monotonic() - t0 + ingest_s)
    _emit(progress, "done", claims=len(result.claims))
    return result


async def analyze_paper(paper_dir: Path, runs: int = 3, mode: str = "batch", *, var_dir: Path | None = None,
                        run_start: int = 0, llm=None, progress: Progress = None) -> RunResult:
    """Ingest a paper directory (or reuse its stored Document) and analyse it `runs` times."""
    if var_dir is None:
        from onus.config import get_settings

        var_dir = get_settings().var_dir
    t = time.monotonic()
    _emit(progress, "ingest", paper=paper_dir.name)
    doc = load_or_ingest(paper_dir, var_dir)
    return await analyze_doc_runs(doc, runs, mode, run_start=run_start, llm=llm, progress=progress,
                                  ingest_s=time.monotonic() - t)
