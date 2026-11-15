"""SQLAlchemy 2.0 models (BUILD_PLAN §8), portable between SQLite and Postgres.

Portability rules: UUID primary keys as 36-char strings; timezone-aware UTC datetimes; JSON columns that
become JSONB on Postgres; no SQLite-only SQL, autoincrement or triggers; enum-like fields are strings
checked in code (`STATUS_VALUES`, ...); all access goes through `db/repo.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")

SOURCE_VALUES = ("agents4science", "arxiv", "openreview", "upload")
MODE_VALUES = ("batch", "live")
STATUS_VALUES = ("queued", "running", "done", "failed")
DISPUTE_STATUS_VALUES = ("open",)


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC on every backend (SQLite drops tzinfo; this restores it)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    pdf_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    pages: Mapped[int | None] = mapped_column(Integer)
    display: Mapped[dict | None] = mapped_column(JSONType)  # decision, scores, tier (from bundles)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    analyses: Mapped[list[Analysis]] = relationship(back_populates="paper", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_papers_source_external_id"),)


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paper_id: Mapped[str] = mapped_column(String(36), ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    onus_version: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(8))
    runs: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    progress: Mapped[dict | None] = mapped_column(JSONType)
    result: Mapped[dict | None] = mapped_column(JSONType)  # the full RunResult
    summary: Mapped[dict | None] = mapped_column(JSONType)
    cost_eur: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    paper: Mapped[Paper] = relationship(back_populates="analyses")
    disputes: Mapped[list[Dispute]] = relationship(back_populates="analysis", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("paper_id", "fingerprint", "mode", name="uq_analyses_paper_fp_mode"),)


class Dispute(Base):
    __tablename__ = "disputes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(String(36), ForeignKey("analyses.id", ondelete="CASCADE"), index=True)
    claim_id: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    analysis: Mapped[Analysis] = relationship(back_populates="disputes")


class LiveUsage(Base):
    __tablename__ = "live_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    analyses: Mapped[int] = mapped_column(Integer, default=0)
    cost_eur: Mapped[float] = mapped_column(Float, default=0.0)


class LiveRequest(Base):
    __tablename__ = "live_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ip_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    analysis_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("analyses.id", ondelete="SET NULL"))


def postgres_ddl() -> str:
    """CREATE TABLE / CREATE INDEX statements for Postgres, compiled from the same metadata."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    dialect = postgresql.dialect()
    stmts: list[str] = []
    for table in Base.metadata.sorted_tables:
        stmts.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            stmts.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")
    return "\n\n".join(stmts) + "\n"
