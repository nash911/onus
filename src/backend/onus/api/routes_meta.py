"""/api/health, /api/meta, /api/findings, /api/metrics."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from onus.api.context import AppContext, Ctx
from onus.api.routes_papers import best_analyses, corpus_rows, resolve_paper
from onus.api.serialize import paper_base
from onus.db.session import session_scope

router = APIRouter(prefix="/api")

EMPTY_RESULTS = "Results appear after the held-out runs."
N_FEATURED = 3


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/meta")
def meta(ctx: Ctx) -> dict[str, Any]:
    with session_scope(ctx.factory) as s:
        featured: list[dict] = []
        source = "config"
        for ref in ctx.settings.featured_paper_ids:
            p = resolve_paper(s, ref)
            if p is not None:
                best = best_analyses(s, [p.id]).get(p.id)
                featured.append(paper_base(p) | {"analysis_id": best.id if best else None,
                                                 "summary": best.summary if best else None})
        if not featured:
            # No curated list: show the corpus papers with the most flagged claims.
            source = "auto"
            rows = sorted(corpus_rows(s), key=lambda r: -((r["summary"] or {}).get("flagged_share") or 0))
            featured = rows[:N_FEATURED]
        n_papers = len(corpus_rows(s))
    lim = ctx.limits
    return {
        "onus_version": ctx.onus_version,
        "fingerprint": ctx.fingerprint,
        "models": ctx.models,
        "featured": featured[:N_FEATURED],
        "featured_source": source,
        "corpus_papers": n_papers,
        "has_findings": ctx.data_file("findings.json") is not None,
        "has_metrics": ctx.data_file("metrics.json") is not None,
        "live": {
            "enabled": ctx.live_enabled,
            "runs": 1,
            "code_checks": False,
            "max_pdf_mb": lim.max_pdf_mb,
            "max_per_ip_per_hour": lim.max_per_ip_per_hour,
            "max_per_day": lim.max_per_day,
        },
    }


def _data(ctx: AppContext, name: str) -> Any:
    p = ctx.data_file(name)
    if p is None:
        raise HTTPException(404, EMPTY_RESULTS)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(500, f"{name} could not be read") from e


@router.get("/findings")
def findings(ctx: Ctx) -> Any:
    return _data(ctx, "findings.json")


@router.get("/metrics")
def metrics(ctx: Ctx) -> Any:
    return _data(ctx, "metrics.json")
