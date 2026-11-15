"""/api/analyses: results, live job status, starting live analyses, disputes.

POST /api/analyses accepts either JSON `{source: "arxiv"|"openreview", id}` (or `{ref: "<id or URL>"}`) or a
multipart PDF upload (field `file`). A finished analysis of the same PDF (sha256) at the current fingerprint,
or of the same arXiv/OpenReview id, is returned at once (`cached: true`) and costs nothing; an analysis of the
same paper that is still queued or running is returned instead of starting a second one. Only new jobs are
counted against the live limits.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from onus.api.context import AppContext, Ctx
from onus.api.jobs import LiveJob
from onus.api.limits import BadUpload, LimitExceeded, client_ip, hash_ip
from onus.api.serialize import paper_base, status_out
from onus.db import repo
from onus.db.models import Analysis
from onus.db.session import session_scope

router = APIRouter(prefix="/api")


def _get_analysis(s, analysis_id: str) -> Analysis:
    a = repo.get_analysis(s, analysis_id)
    if a is None:
        raise HTTPException(404, "No such analysis.")
    return a


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: str, ctx: Ctx) -> dict[str, Any]:
    """The full RunResult, plus `display` (as in bundles) and `analysis` (id, paper ref, status)."""
    with session_scope(ctx.factory) as s:
        a = _get_analysis(s, analysis_id)
        if a.status != "done" or a.result is None:
            raise HTTPException(409, f"This analysis is {a.status}; see /api/analyses/{a.id}/status.")
        p = a.paper
        base = paper_base(p)
        return {**a.result, "display": p.display,
                "analysis": {"id": a.id, "paper_id": p.id, "paper_ref": base["ref"], "status": a.status,
                             "source_url": base["source_url"], "title": base["title"]}}


@router.get("/analyses/{analysis_id}/status")
def get_status(analysis_id: str, ctx: Ctx) -> dict[str, Any]:
    with session_scope(ctx.factory) as s:
        return status_out(_get_analysis(s, analysis_id))


# ---------------------------------------------------------------- start a live analysis


def _create(ctx: AppContext, *, source: str, external_id: str | None, data: bytes | None,
            ip: str) -> tuple[dict[str, Any], LiveJob | None]:
    fp = ctx.fingerprint
    with session_scope(ctx.factory) as s:
        sha = hashlib.sha256(data).hexdigest() if data is not None else None
        if sha is not None:
            external_id = sha[:12]
            hit = repo.done_analysis_for_sha(s, sha, fp)
            paper = repo.find_paper(s, source="upload", external_id=external_id)
        else:
            assert external_id is not None
            paper = repo.find_paper(s, source=source, external_id=external_id)
            hit = None
            if paper is not None:
                for mode in ("batch", "live"):
                    a = repo.find_analysis(s, paper_id=paper.id, fingerprint=fp, mode=mode)
                    if a is not None and a.status == "done":
                        hit = a
                        break
                if hit is None and paper.pdf_sha256:
                    hit = repo.done_analysis_for_sha(s, paper.pdf_sha256, fp)
        if hit is not None:
            return {"analysis_id": hit.id, "status": "done", "cached": True}, None
        existing = (repo.find_analysis(s, paper_id=paper.id, fingerprint=fp, mode="live")
                    if paper is not None else None)
        if existing is not None and existing.status in ("queued", "running"):
            return {"analysis_id": existing.id, "status": existing.status, "cached": False}, None

        if not ctx.live_enabled:
            raise HTTPException(503, "Live analysis is not available on this server.")
        ip_hash = hash_ip(ip, ctx.salt)
        ctx.limits.check(s, ip_hash)

        pdf_path = None
        if data is not None:
            from onus.ingest.fetch import store_upload

            pdf_path, _ = store_upload(data, ctx.settings.var_dir)
        paper = repo.upsert_paper(s, source=source, external_id=external_id, pdf_sha256=sha)
        if existing is not None:  # a failed earlier attempt: reuse its (paper, fingerprint, mode) row
            a = repo.reset_analysis(s, existing)
        else:
            a = repo.create_analysis(s, paper_id=paper.id, onus_version=ctx.onus_version, fingerprint=fp,
                                     mode="live", runs=1, status="queued")
        a.progress = {"stage": "queued", "stages": [], "history": [], "info": {"ahead": ctx.runner.pending}}
        ctx.limits.record(s, ip_hash, a.id)
        job = LiveJob(analysis_id=a.id, paper_db_id=paper.id, source=source, external_id=external_id,
                      pdf_path=pdf_path)
        return {"analysis_id": a.id, "status": "queued", "cached": False}, job


class RefIn(BaseModel):
    source: str | None = None
    id: str | None = None
    ref: str | None = Field(None, description="an arXiv or OpenReview id or URL")


def _parse_ref(body: RefIn) -> tuple[str, str]:
    from onus.ingest.fetch import parse_ref

    if body.ref:
        text = body.ref
    elif body.source and body.id:
        if body.source not in ("arxiv", "openreview"):
            raise HTTPException(422, "source must be 'arxiv' or 'openreview'.")
        text = f"{body.source}:{body.id}"
    else:
        raise HTTPException(422, "Give {source, id}, {ref}, or upload a PDF.")
    try:
        r = parse_ref(text)
    except ValueError as e:
        raise HTTPException(422, "That does not look like an arXiv or OpenReview id.") from e
    return r.source, r.external_id


@router.post("/analyses")
async def start_analysis(request: Request, ctx: Ctx) -> JSONResponse:
    ctype = request.headers.get("content-type", "")
    data: bytes | None = None
    external_id: str | None = None
    try:
        if ctype.startswith("multipart/form-data"):
            declared = int(request.headers.get("content-length") or 0)
            if declared > ctx.limits.max_pdf_bytes + 1024 * 1024:
                raise BadUpload(f"The PDF is larger than {ctx.limits.max_pdf_mb} MB.", 413)
            form = await request.form()
            f = form.get("file")
            if f is None or isinstance(f, str):
                raise BadUpload("Upload the PDF in a form field named 'file'.")
            data = await f.read(ctx.limits.max_pdf_bytes + 1)
            await f.close()
            ctx.limits.check_pdf(data)
            source = "upload"
        else:
            try:
                body = RefIn.model_validate(await request.json())
            except Exception as e:
                raise HTTPException(422, "Send JSON {source, id} or upload a PDF.") from e
            source, external_id = _parse_ref(body)
        out, job = await run_in_threadpool(_create, ctx, source=source, external_id=external_id, data=data,
                                           ip=client_ip(request))
    except BadUpload as e:
        raise HTTPException(e.status_code, str(e)) from e
    except LimitExceeded as e:
        return JSONResponse({"detail": str(e)}, status_code=429)
    if job is not None:
        ctx.runner.enqueue(job)
    return JSONResponse(out, status_code=200 if out["status"] == "done" else 202)


# ---------------------------------------------------------------- disputes


class DisputeIn(BaseModel):
    claim_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=3, max_length=4000)
    contact: str | None = Field(None, max_length=255)


@router.post("/analyses/{analysis_id}/disputes", status_code=201)
def add_dispute(analysis_id: str, body: DisputeIn, ctx: Ctx) -> dict[str, Any]:
    with session_scope(ctx.factory) as s:
        a = _get_analysis(s, analysis_id)
        ids = {c.get("id") for c in ((a.result or {}).get("claims") or [])}
        ids |= {f"checklist:{c.get('key')}" for c in ((a.result or {}).get("checklist") or [])}
        if body.claim_id not in ids:
            raise HTTPException(422, "That claim is not in this analysis.")
        d = repo.add_dispute(s, analysis_id=a.id, claim_id=body.claim_id, reason=body.reason.strip(),
                             contact=(body.contact or "").strip() or None)
        return {"id": d.id, "status": d.status, "claim_id": d.claim_id, "created_at": d.created_at.isoformat()}
