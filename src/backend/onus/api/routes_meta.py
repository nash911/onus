"""/api/health, /api/meta, /api/findings, /api/metrics, /api/public/{name}."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from onus.api.context import AppContext, Ctx
from onus.api.eval_log import read_eval_runs
from onus.api.routes_papers import best_analyses, corpus_rows, resolve_paper
from onus.api.serialize import iso, paper_base
from onus.db import repo
from onus.db.session import session_scope

router = APIRouter(prefix="/api")

EMPTY_RESULTS = "Results appear after the held-out runs."
N_FEATURED = 3
# docs/public files the Trust page links to (served as text; nothing else in that folder is exposed)
PUBLIC_DOCS = ("eval_protocol.md", "eval_log.md")


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


def freeze_info(ctx: AppContext) -> dict[str, Any] | None:
    """The freeze manifest's headline fields (frozen.json, written by `onus freeze`), or None before the freeze."""
    if ctx.frozen_manifest is None:
        return None
    try:
        m = json.loads(ctx.frozen_manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or not m.get("fingerprint"):
        return None
    return {
        "version": m.get("version"),
        "fingerprint": m.get("fingerprint"),
        "harness_fingerprint": m.get("harness_fingerprint"),
        "created_at": m.get("created_at"),
        "matches_current": m.get("fingerprint") == ctx.fingerprint,
    }


def public_docs(ctx: AppContext) -> list[str]:
    folder = ctx.eval_log.parent if ctx.eval_log else None
    return [n for n in PUBLIC_DOCS if folder is not None and (folder / n).is_file()]


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
                                                 "summary": best.summary if best else None,
                                                 "fingerprint": best.fingerprint if best else None,
                                                 "onus_version": best.onus_version if best else None})
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
        "freeze": freeze_info(ctx),
        "public_docs": public_docs(ctx),
        "has_findings": ctx.data_file("findings.json") is not None,
        "has_metrics": ctx.data_file("metrics.json") is not None,
        "live": {
            "enabled": ctx.live_enabled,
            "runs": 1,
            "code_checks": ctx.code_checks_enabled,
            "capabilities_probed_at": ctx.capabilities_probed_at,
            "max_pdf_mb": lim.max_pdf_mb,
            "max_per_ip_per_hour": lim.max_per_ip_per_hour,
            "max_per_day": lim.max_per_day,
            "code": {
                "max_zip_mb": ctx.serving.live_max_code_zip_mb,
                "max_reruns_per_day": ctx.serving.live_max_reruns_per_day,
                "max_reruns_per_ip_per_day": ctx.serving.live_max_reruns_per_ip_per_day,
                "max_rerun_minutes": ctx.settings.onus_rerun_max_minutes,
                "hosts": ["GitHub", "GitLab", "Hugging Face"],
            } if ctx.code_checks_enabled else None,
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


@router.get("/public/{name}", response_class=PlainTextResponse)
def public_doc(name: str, ctx: Ctx) -> PlainTextResponse:
    """docs/public/eval_protocol.md and eval_log.md as plain text (the Trust page links here)."""
    if name not in PUBLIC_DOCS or name not in public_docs(ctx):
        raise HTTPException(404, "No such document.")
    assert ctx.eval_log is not None
    text = (ctx.eval_log.parent / name).read_text(encoding="utf-8")
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")
