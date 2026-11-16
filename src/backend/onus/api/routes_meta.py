"""/api/health, /api/meta, /api/findings, /api/metrics."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from onus.api.context import AppContext, Ctx
from onus.api.eval_log import read_eval_runs
from onus.api.routes_papers import best_analyses, corpus_rows, resolve_paper
from onus.api.serialize import iso, paper_base
from onus.db import repo
from onus.db.session import session_scope

router = APIRouter(prefix="/api")

EMPTY_RESULTS = "Results appear after the held-out runs."
N_FEATURED = 3


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def version_history(s) -> list[dict[str, Any]]:
    """Distinct (version, fingerprint, mode) behind the finished reports, oldest first."""
    groups: dict[tuple, dict[str, Any]] = {}
    for h in repo.analysis_headers(s):
        key = (h.onus_version, h.fingerprint, h.mode)
        g = groups.setdefault(key, {"onus_version": h.onus_version, "fingerprint": h.fingerprint, "mode": h.mode,
                                    "analyses": 0, "first_seen": None, "last_seen": None})
        g["analyses"] += 1
        ts = iso(h.created_at)
        if ts and (g["first_seen"] is None or ts < g["first_seen"]):
            g["first_seen"] = ts
        if ts and (g["last_seen"] is None or ts > g["last_seen"]):
            g["last_seen"] = ts
    return sorted(groups.values(), key=lambda g: (g["first_seen"] or "", g["mode"]))


def eval_runs(ctx: AppContext) -> dict[str, Any]:
    """Pre-freeze eval runs from docs/public/eval_log.md (rows whose split is "eval")."""
    runs = read_eval_runs(ctx.eval_log) if ctx.eval_log else []
    return {"count": len(runs), "max": 3, "runs": runs}


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
        versions = version_history(s)
    lim = ctx.limits
    return {
        "onus_version": ctx.onus_version,
        "fingerprint": ctx.fingerprint,
        "models": ctx.models,
        "featured": featured[:N_FEATURED],
        "featured_source": source,
        "corpus_papers": n_papers,
        "versions": versions,
        "eval_runs": eval_runs(ctx),
        "has_findings": ctx.data_file("findings.json") is not None,
        "has_metrics": ctx.data_file("metrics.json") is not None,
        "live": {
            "enabled": ctx.live_enabled,
            "runs": 1,
            "code_checks": False,
            "capabilities_probed_at": ctx.capabilities_probed_at,
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
