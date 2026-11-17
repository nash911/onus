"""GET /api/analyses/{id}/pdf and /anchors (Phase 13a): the paper's PDF and where each claim sits in it.

Both resolve only through the analysis id: the PDF is `$ONUS_VAR_DIR/uploads/<sha256>.pdf` for the sha stored on
the analysis's paper row, and no part of the URL reaches the filesystem. Corpus analyses imported from bundles
have no stored PDF: /pdf is 404 `{"detail": "no_pdf"}` and /anchors is 200 with `pdf: false`. See `anchors.py`
for the anchors contract (PDF points, top-left origin).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from onus.api.anchors import anchors_for, empty, stored_pdf
from onus.api.context import Ctx
from onus.db import repo
from onus.db.session import session_scope

router = APIRouter(prefix="/api")

PDF_CACHE = "public, max-age=31536000, immutable"  # content-addressed: an analysis's paper never changes its PDF


def _paper(ctx, analysis_id: str) -> tuple[str | None, str, dict | None]:
    """(pdf sha, status, result) for an analysis, or 404."""
    with session_scope(ctx.factory) as s:
        a = repo.get_analysis(s, analysis_id)
        if a is None:
            raise HTTPException(404, "No such analysis.")
        return a.paper.pdf_sha256, a.status, a.result


@router.get("/analyses/{analysis_id}/pdf", response_model=None)
def get_pdf(analysis_id: str, request: Request, ctx: Ctx) -> Response:
    sha, _, _ = _paper(ctx, analysis_id)
    path = stored_pdf(ctx.settings.var_dir, sha)
    if path is None:
        return JSONResponse({"detail": "no_pdf"}, status_code=404)
    etag = f'"{sha}"'
    headers = {"ETag": etag, "Cache-Control": PDF_CACHE}
    if etag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="application/pdf",
                        headers={**headers, "Content-Disposition": 'inline; filename="paper.pdf"'})


@router.get("/analyses/{analysis_id}/anchors")
def get_anchors(analysis_id: str, ctx: Ctx) -> dict[str, Any]:
    """Highlight rectangles per claim (`<claim_id>`) and evidence item (`<claim_id>:<ev_id>`), in PDF points with a
    top-left origin. Runs in FastAPI's threadpool; the first call may ingest the PDF (seconds), later calls read
    `$ONUS_VAR_DIR/anchors/<id>.json`."""
    sha, status, result = _paper(ctx, analysis_id)
    if status != "done" or result is None:
        raise HTTPException(409, f"This analysis is {status}; see /api/analyses/{analysis_id}/status.")
    if stored_pdf(ctx.settings.var_dir, sha) is None:
        return empty()
    return anchors_for(ctx.settings.var_dir, analysis_id, result, sha)
