"""/api/papers (corpus list with filters, sort and paging) and /api/papers/{paper_id}.

The corpus is every paper with a finished batch analysis (live uploads are reachable by their own links but
not listed). Filtering and sorting happen in Python over the listing summaries: the corpus is a few hundred
rows, and this keeps the SQL portable (no JSON operators).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from onus.api.context import Ctx
from onus.api.serialize import analysis_header, paper_base
from onus.db import repo
from onus.db.models import SOURCE_VALUES, Paper
from onus.db.session import session_scope

router = APIRouter(prefix="/api")

VERDICTS = ("supported", "overstated", "untraced", "contradicted", "not_checkable")


def resolve_paper(s: Session, ref: str) -> Paper | None:
    """A paper by DB uuid, by `source:external_id`, or by bare external id (corpus forum id)."""
    p = repo.get_paper(s, ref)
    if p is not None:
        return p
    if ":" in ref:
        source, ext = ref.split(":", 1)
        if source in SOURCE_VALUES:
            return repo.find_paper(s, source=source, external_id=ext)
    for source in ("agents4science", "openreview", "arxiv", "upload"):
        p = repo.find_paper(s, source=source, external_id=ref)
        if p is not None:
            return p
    return None


def _rank(h: Any) -> tuple:
    # batch before live, more runs first, newest first
    return (0 if h.mode == "batch" else 1, -(h.runs or 0), -(h.created_at.timestamp() if h.created_at else 0))


def best_analyses(s: Session, paper_ids: list[str] | None = None, mode: str | None = None) -> dict[str, Any]:
    """paper_id -> the analysis header shown for that paper."""
    out: dict[str, Any] = {}
    wanted = set(paper_ids) if paper_ids is not None else None
    for h in repo.analysis_headers(s, mode=mode):
        if wanted is not None and h.paper_id not in wanted:
            continue
        cur = out.get(h.paper_id)
        if cur is None or _rank(h) < _rank(cur):
            out[h.paper_id] = h
    return out


def corpus_rows(s: Session) -> list[dict[str, Any]]:
    best = best_analyses(s, mode="batch")
    rows = []
    for p in repo.all_papers(s):
        h = best.get(p.id)
        if h is None:
            continue
        rows.append(paper_base(p) | {"analysis_id": h.id, "summary": h.summary, "fingerprint": h.fingerprint,
                                     "onus_version": h.onus_version, "runs": h.runs})
    return rows


def _has_code(row: dict) -> bool:
    code = (row.get("summary") or {}).get("code") or {}
    return bool(code.get("available"))


@router.get("/papers")
def list_papers(
    ctx: Ctx,
    decision: str | None = None,
    has_code: bool | None = None,
    tier: str | None = None,
    verdict: str | None = Query(None, description="papers with at least one claim of this verdict"),
    q: str | None = Query(None, description="title substring"),
    sort: Literal["flagged_share", "title", "main_flagged_share", "n_claims"] = "flagged_share",
    order: Literal["asc", "desc"] | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    if verdict is not None and verdict not in VERDICTS:
        raise HTTPException(422, f"verdict must be one of {', '.join(VERDICTS)}")
    with session_scope(ctx.factory) as s:
        rows = corpus_rows(s)
    decisions = sorted({(r["display"] or {}).get("decision") for r in rows} - {None})
    tiers = sorted({(r["display"] or {}).get("tier") for r in rows} - {None})

    def keep(r: dict) -> bool:
        d = r["display"] or {}
        if decision is not None and d.get("decision") != decision:
            return False
        if tier is not None and d.get("tier") != tier:
            return False
        if has_code is not None and _has_code(r) != has_code:
            return False
        if verdict is not None and not ((r["summary"] or {}).get("counts") or {}).get(verdict):
            return False
        return not (q and q.lower() not in (r["title"] or "").lower())

    rows = [r for r in rows if keep(r)]
    desc = (order or ("asc" if sort == "title" else "desc")) == "desc"
    if sort == "title":
        rows.sort(key=lambda r: ((r["title"] or "").lower(), r["id"]), reverse=desc)
    else:
        rows.sort(key=lambda r: ((r["summary"] or {}).get(sort) or 0, (r["title"] or "").lower()), reverse=desc)
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows[offset: offset + limit],
            "facets": {"decision": decisions, "tier": tiers, "verdict": list(VERDICTS)}}


@router.get("/papers/{paper_id:path}")
def get_paper(paper_id: str, ctx: Ctx) -> dict[str, Any]:
    with session_scope(ctx.factory) as s:
        p = resolve_paper(s, paper_id)
        if p is None:
            raise HTTPException(404, "No such paper.")
        analyses = repo.analyses_for_paper(s, p.id)
        best = best_analyses(s, [p.id]).get(p.id)
        return paper_base(p) | {"analysis_id": best.id if best else None,
                                "analyses": [analysis_header(a) for a in analyses]}
