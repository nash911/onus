"""Live-analysis limits (BUILD_PLAN Phase 8.4): per IP per hour, per day, daily cost cap, upload checks.

The public app has no login, so every live request is counted against a salted hash of the client IP. The
salt lives in `$ONUS_VAR_DIR/ip_salt` (created on first use), so raw IPs are never stored and hashes survive
restarts. Counts come from the `live_requests` and `live_usage` tables only.

Code checks (Phase 11b) add their own limits: the uploaded code .zip (size, member count), the repository
download, and the daily re-run caps in `CodeLimits`. Re-run counts and Modal minutes are read back from the
code-stage record each live analysis keeps in `analyses.progress["code"]` (no extra table).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi import Request
from sqlalchemy.orm import Session

from onus.config import Settings
from onus.db import repo

PDF_MAGIC = b"%PDF"
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")

# Code for live code checks (uploaded .zip or a downloaded repository archive). Shown in the UI via /api/meta.
CODE_ZIP_MAX_MB = 25  # uploaded .zip (LIVE_MAX_CODE_ZIP_MB overrides)
REPO_ZIP_MAX_MB = 100  # downloaded repository archive
CODE_UNZIPPED_MAX_MB = 300  # text files written after extraction (binaries are skipped)
CODE_MAX_FILES = 5000  # members in an archive / files in a repository
REPO_DOWNLOAD_TIMEOUT_S = 90.0


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

    def check_code_zip(self, data: bytes, max_mb: int) -> None:
        check_code_zip(data, max_mb)


def check_code_zip(data: bytes, max_mb: int) -> None:
    """An uploaded code archive: a real .zip, within the size cap and the member-count cap."""
    import io
    import zipfile

    if len(data) > max_mb * 1024 * 1024:
        raise BadUpload(f"The code .zip is larger than {max_mb} MB.", 413)
    if not data.startswith(ZIP_MAGIC):
        raise BadUpload("The code must be a .zip file.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            n = len(zf.infolist())
    except zipfile.BadZipFile as e:
        raise BadUpload("The code .zip could not be read.") from e
    if n > CODE_MAX_FILES:
        raise BadUpload(f"The code .zip has more than {CODE_MAX_FILES} files.")


# ---------------------------------------------------------------- code re-runs (Phase 11b)


@dataclass
class CodeUsage:
    reruns: int = 0
    modal_minutes: float = 0.0
    reruns_ip: int = 0


@dataclass
class CodeLimits:
    max_reruns_per_day: int
    max_modal_minutes_per_day: float
    max_reruns_per_ip_per_day: int
    max_rerun_minutes: float  # ONUS_RERUN_MAX_MINUTES, per paper
    min_rerun_minutes: float = 5.0  # below this a re-run cannot install and run anything

    def usage(self, s: Session, ip_hash: str | None) -> CodeUsage:
        """Today's (UTC) re-runs and Modal minutes, from the code-stage records of live analyses."""
        day = today().isoformat()
        since = datetime.now(UTC) - timedelta(days=2)
        ip_ids = set(repo.analysis_ids_for_ip(s, ip_hash, since)) if ip_hash else set()
        u = CodeUsage()
        for aid, progress in repo.live_progress_since(s, since):
            code = (progress or {}).get("code") or {}
            rr = code.get("rerun") or {}
            if code.get("day") != day or not rr.get("attempted"):
                continue
            u.reruns += 1
            u.modal_minutes += float(rr.get("minutes") or 0.0)
            if aid in ip_ids:
                u.reruns_ip += 1
        return u

    def rerun_minutes(self, s: Session, ip_hash: str | None) -> tuple[float | None, str | None]:
        """(minutes this re-run may use, None) or (None, the plain reason it is skipped)."""
        u = self.usage(s, ip_hash)
        if u.reruns >= self.max_reruns_per_day:
            return None, (f"Skipped: the daily limit of {self.max_reruns_per_day} code re-runs has been reached. "
                          "The static code check still ran.")
        if ip_hash and u.reruns_ip >= self.max_reruns_per_ip_per_day:
            return None, (f"Skipped: at most {self.max_reruns_per_ip_per_day} code re-runs per visitor per day. "
                          "The static code check still ran.")
        left = self.max_modal_minutes_per_day - u.modal_minutes
        if left < self.min_rerun_minutes:
            return None, ("Skipped: today's sandbox time for code re-runs is used up. "
                          "The static code check still ran.")
        return min(self.max_rerun_minutes, left), None
