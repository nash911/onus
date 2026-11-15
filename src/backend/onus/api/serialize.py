"""JSON shapes the API returns for papers and analyses (the RunResult itself is the schema contract)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from onus.db.models import Analysis, Paper


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def source_url(source: str, external_id: str | None) -> str | None:
    """A link to the paper's public page (shown, never fetched by the API)."""
    if not external_id:
        return None
    if source in ("agents4science", "openreview"):
        return f"https://openreview.net/forum?id={external_id}"
    if source == "arxiv":
        return f"https://arxiv.org/abs/{external_id}"
    return None


def paper_ref(p: Paper) -> str:
    """The id the frontend uses in URLs: the external id for corpus papers, `source:id` otherwise."""
    if p.source == "agents4science":
        return p.external_id or p.id
    return f"{p.source}:{p.external_id}" if p.external_id else p.id


def paper_base(p: Paper) -> dict[str, Any]:
    display = p.display or {}
    return {
        "id": p.id,
        "ref": paper_ref(p),
        "source": p.source,
        "external_id": p.external_id,
        "title": display.get("title") or p.title,
        "pages": p.pages,
        "pdf_sha256": p.pdf_sha256,
        "source_url": source_url(p.source, p.external_id),
        "display": p.display,
    }


def analysis_header(a: Analysis | Any) -> dict[str, Any]:
    """Works for an Analysis row or a row from `repo.analysis_headers` (no result payload)."""
    out = {
        "id": a.id,
        "paper_id": a.paper_id,
        "mode": a.mode,
        "runs": a.runs,
        "fingerprint": a.fingerprint,
        "onus_version": a.onus_version,
        "summary": a.summary,
        "created_at": iso(a.created_at),
    }
    if isinstance(a, Analysis):
        out |= {"status": a.status, "cost_eur": a.cost_eur, "finished_at": iso(a.finished_at), "error": a.error}
    return out


def status_out(a: Analysis) -> dict[str, Any]:
    return {
        "id": a.id,
        "paper_id": a.paper_id,
        "status": a.status,
        "mode": a.mode,
        "progress": a.progress,
        "error": a.error,
        "created_at": iso(a.created_at),
        "finished_at": iso(a.finished_at),
    }
