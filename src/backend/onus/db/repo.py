"""All database access. Functions take a Session; callers own the transaction (see `session_scope`).

Enum-like string fields are validated here, since the schema stores them as plain strings.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from onus.db.models import (
    DISPUTE_STATUS_VALUES,
    MODE_VALUES,
    SOURCE_VALUES,
    STATUS_VALUES,
    Analysis,
    Dispute,
    LiveRequest,
    LiveUsage,
    Paper,
    utcnow,
)


def _check(value: str, allowed: tuple[str, ...], what: str) -> None:
    if value not in allowed:
        raise ValueError(f"invalid {what} {value!r}; expected one of {allowed}")


# ---------------------------------------------------------------- papers


def get_paper(s: Session, paper_id: str) -> Paper | None:
    return s.get(Paper, paper_id)


def find_paper(s: Session, *, source: str, external_id: str) -> Paper | None:
    return s.scalars(select(Paper).where(Paper.source == source, Paper.external_id == external_id)).first()


def find_paper_by_sha(s: Session, pdf_sha256: str) -> Paper | None:
    return s.scalars(select(Paper).where(Paper.pdf_sha256 == pdf_sha256).order_by(Paper.created_at)).first()


def upsert_paper(
    s: Session, *, source: str, external_id: str, title: str | None = None, pdf_sha256: str | None = None,
    pages: int | None = None, display: dict | None = None,
) -> Paper:
    _check(source, SOURCE_VALUES, "source")
    p = find_paper(s, source=source, external_id=external_id)
    if p is None:
        p = Paper(source=source, external_id=external_id)
        s.add(p)
    for name, value in (("title", title), ("pdf_sha256", pdf_sha256), ("pages", pages), ("display", display)):
        if value is not None:
            setattr(p, name, value)
    s.flush()
    return p


def list_papers(s: Session, *, offset: int = 0, limit: int = 100) -> list[Paper]:
    return list(s.scalars(select(Paper).order_by(Paper.title, Paper.id).offset(offset).limit(limit)))


def count_papers(s: Session) -> int:
    return int(s.scalar(select(func.count()).select_from(Paper)) or 0)


# ---------------------------------------------------------------- analyses


def get_analysis(s: Session, analysis_id: str) -> Analysis | None:
    return s.get(Analysis, analysis_id)


def find_analysis(s: Session, *, paper_id: str, fingerprint: str, mode: str) -> Analysis | None:
    return s.scalars(
        select(Analysis).where(
            Analysis.paper_id == paper_id, Analysis.fingerprint == fingerprint, Analysis.mode == mode
        )
    ).first()


def analyses_for_paper(s: Session, paper_id: str) -> list[Analysis]:
    return list(s.scalars(select(Analysis).where(Analysis.paper_id == paper_id).order_by(Analysis.created_at)))


def create_analysis(
    s: Session, *, paper_id: str, onus_version: str, fingerprint: str, mode: str, runs: int = 1,
    status: str = "queued",
) -> Analysis:
    _check(mode, MODE_VALUES, "mode")
    _check(status, STATUS_VALUES, "status")
    a = Analysis(paper_id=paper_id, onus_version=onus_version, fingerprint=fingerprint, mode=mode, runs=runs,
                 status=status)
    s.add(a)
    s.flush()
    return a


def set_analysis_status(
    s: Session, analysis: Analysis, status: str, *, progress: dict | None = None, error: str | None = None
) -> Analysis:
    _check(status, STATUS_VALUES, "status")
    analysis.status = status
    if progress is not None:
        analysis.progress = progress
    if error is not None:
        analysis.error = error
    if status in ("done", "failed"):
        analysis.finished_at = utcnow()
    s.flush()
    return analysis


def store_result(s: Session, analysis: Analysis, result: dict[str, Any]) -> Analysis:
    analysis.result = result
    analysis.summary = result.get("summary")
    analysis.cost_eur = (result.get("cost") or {}).get("eur")
    return set_analysis_status(s, analysis, "done")


def fail_stale_running(s: Session, message: str = "server restarted while running") -> int:
    stale = list(s.scalars(select(Analysis).where(Analysis.status.in_(("queued", "running")))))
    for a in stale:
        set_analysis_status(s, a, "failed", error=message)
    return len(stale)


# ---------------------------------------------------------------- disputes


def add_dispute(s: Session, *, analysis_id: str, claim_id: str, reason: str, contact: str | None = None) -> Dispute:
    d = Dispute(analysis_id=analysis_id, claim_id=claim_id, reason=reason, contact=contact, status="open")
    _check(d.status, DISPUTE_STATUS_VALUES, "dispute status")
    s.add(d)
    s.flush()
    return d


def list_disputes(s: Session, analysis_id: str | None = None) -> list[Dispute]:
    q = select(Dispute).order_by(Dispute.created_at)
    if analysis_id:
        q = q.where(Dispute.analysis_id == analysis_id)
    return list(s.scalars(q))


# ---------------------------------------------------------------- live usage and rate limits


def get_live_usage(s: Session, day: date) -> LiveUsage:
    u = s.get(LiveUsage, day)
    if u is None:
        u = LiveUsage(day=day, analyses=0, cost_eur=0.0)
        s.add(u)
        s.flush()
    return u


def add_live_usage(s: Session, day: date, *, analyses: int = 0, cost_eur: float = 0.0) -> LiveUsage:
    u = get_live_usage(s, day)
    u.analyses += analyses
    u.cost_eur += cost_eur
    s.flush()
    return u


def record_live_request(s: Session, ip_hash: str, analysis_id: str | None = None) -> LiveRequest:
    r = LiveRequest(ip_hash=ip_hash, analysis_id=analysis_id)
    s.add(r)
    s.flush()
    return r


def count_live_requests_since(s: Session, ip_hash: str, since: datetime) -> int:
    q = select(func.count()).select_from(LiveRequest).where(
        LiveRequest.ip_hash == ip_hash, LiveRequest.created_at >= since
    )
    return int(s.scalar(q) or 0)


def count_live_requests_last_hour(s: Session, ip_hash: str) -> int:
    return count_live_requests_since(s, ip_hash, utcnow() - timedelta(hours=1))
