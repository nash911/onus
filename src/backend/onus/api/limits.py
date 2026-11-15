"""Live-analysis limits (BUILD_PLAN Phase 8.4): per IP per hour, per day, daily cost cap, upload checks.

The public app has no login, so every live request is counted against a salted hash of the client IP. The
salt lives in `$ONUS_VAR_DIR/ip_salt` (created on first use), so raw IPs are never stored and hashes survive
restarts. Counts come from the `live_requests` and `live_usage` tables only.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import Request
from sqlalchemy.orm import Session

from onus.config import Settings
from onus.db import repo

PDF_MAGIC = b"%PDF"


class LimitExceeded(Exception):
    """Raised with a plain-language message; the API turns it into HTTP 429."""


class BadUpload(Exception):
    """Raised with a plain-language message; the API turns it into HTTP 400 or 413."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def today() -> date:
    return datetime.now(UTC).date()


def load_salt(var_dir: Path) -> bytes:
    p = var_dir / "ip_salt"
    try:
        data = p.read_bytes()
        if len(data) >= 16:
            return data
    except FileNotFoundError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(32)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)
    return data


def client_ip(request: Request) -> str:
    """The first X-Forwarded-For hop (Railway's proxy sets it), else the socket peer."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd.strip():
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def hash_ip(ip: str, salt: bytes) -> str:
    return hashlib.sha256(salt + ip.encode()).hexdigest()


@dataclass
class LiveLimits:
    max_per_day: int
    max_per_ip_per_hour: int
    max_cost_eur_per_day: float
    max_pdf_mb: int

    @classmethod
    def from_settings(cls, s: Settings) -> LiveLimits:
        return cls(s.live_max_per_day, s.live_max_per_ip_per_hour, s.live_max_cost_eur_per_day, s.live_max_pdf_mb)

    @property
    def max_pdf_bytes(self) -> int:
        return self.max_pdf_mb * 1024 * 1024

    def check(self, s: Session, ip_hash: str) -> None:
        """Raise LimitExceeded if a new live analysis may not start now."""
        usage = repo.get_live_usage(s, today())
        if usage.cost_eur >= self.max_cost_eur_per_day:
            raise LimitExceeded("Today's budget for live analyses is used up. Please try again tomorrow (UTC), "
                                "or browse the analysed corpus.")
        if usage.analyses >= self.max_per_day:
            raise LimitExceeded(f"The daily limit of {self.max_per_day} live analyses has been reached. "
                                "Please try again tomorrow (UTC).")
        if repo.count_live_requests_last_hour(s, ip_hash) >= self.max_per_ip_per_hour:
            raise LimitExceeded(f"You can start at most {self.max_per_ip_per_hour} live analyses per hour. "
                                "Please try again later.")

    def record(self, s: Session, ip_hash: str, analysis_id: str) -> None:
        """Count a started live analysis (cost is added when it finishes)."""
        repo.record_live_request(s, ip_hash, analysis_id)
        repo.add_live_usage(s, today(), analyses=1)

    def check_pdf(self, data: bytes) -> None:
        if len(data) > self.max_pdf_bytes:
            raise BadUpload(f"The PDF is larger than {self.max_pdf_mb} MB.", 413)
        if not data.startswith(PDF_MAGIC):
            raise BadUpload("That file is not a PDF.")
