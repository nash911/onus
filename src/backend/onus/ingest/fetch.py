"""Live-mode PDF fetchers for arXiv and OpenReview (PDF only). (Phase 2)

Only the PDF is ever fetched: never OpenReview notes, reviews or decisions (those would be labels).
Tests mock HTTP with `httpx.MockTransport`; nothing in the test suite touches the network.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

ARXIV_PDF = "https://arxiv.org/pdf/{id}"
OPENREVIEW_PDF = "https://openreview.net/pdf?id={id}"

_ARXIV_ID = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$", re.IGNORECASE)
_OPENREVIEW_ID = re.compile(r"^[A-Za-z0-9_\-]{6,20}$")


class FetchError(RuntimeError):
    pass


@dataclass
class PaperRef:
    source: str  # arxiv | openreview
    external_id: str

    @property
    def paper_id(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def pdf_url(self) -> str:
        return (ARXIV_PDF if self.source == "arxiv" else OPENREVIEW_PDF).format(id=self.external_id)


def parse_ref(s: str) -> PaperRef:
    """Accepts "arxiv:2401.01234", "2401.01234v2", arXiv abs/pdf URLs, "openreview:<id>", OpenReview
    forum/pdf URLs. Raises ValueError on anything else."""
    s = s.strip()
    m = re.match(r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/(?P<id>[^?#\s]+?)(?:\.pdf)?/?$", s, re.IGNORECASE)
    if m:
        s = "arxiv:" + m.group("id")
    m = re.match(r"^https?://(?:www\.)?openreview\.net/(?:forum|pdf)\?(?:.*&)?id=(?P<id>[A-Za-z0-9_\-]+)", s, re.IGNORECASE)
    if m:
        s = "openreview:" + m.group("id")
    if s.lower().startswith("arxiv:"):
        ident = s.split(":", 1)[1]
        if _ARXIV_ID.match(ident):
            return PaperRef("arxiv", ident)
        raise ValueError(f"not an arXiv id: {ident!r}")
    if s.lower().startswith("openreview:"):
        ident = s.split(":", 1)[1]
        if _OPENREVIEW_ID.match(ident):
            return PaperRef("openreview", ident)
        raise ValueError(f"not an OpenReview id: {ident!r}")
    if _ARXIV_ID.match(s):
        return PaperRef("arxiv", s)
    raise ValueError(f"unrecognised paper reference: {s!r}")


def fetch_pdf(ref: PaperRef, *, max_mb: float = 20, client: httpx.Client | None = None,
              timeout: float = 60.0) -> bytes:
    """Download the PDF for `ref`. Validates the %PDF magic and the size cap."""
    own = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True,
                                    headers={"User-Agent": "onus-pdf-fetch/0.1"})
    limit = int(max_mb * 1024 * 1024)
    try:
        with client.stream("GET", ref.pdf_url) as r:
            if r.status_code != 200:
                raise FetchError(f"{ref.paper_id}: HTTP {r.status_code}")
            declared = int(r.headers.get("content-length") or 0)
            if declared > limit:
                raise FetchError(f"{ref.paper_id}: PDF larger than {max_mb} MB")
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > limit:
                    raise FetchError(f"{ref.paper_id}: PDF larger than {max_mb} MB")
    except httpx.HTTPError as e:
        raise FetchError(f"{ref.paper_id}: {type(e).__name__}") from e
    finally:
        if own:
            client.close()
    data = bytes(buf)
    if not data.startswith(b"%PDF"):
        raise FetchError(f"{ref.paper_id}: response is not a PDF")
    return data


def store_upload(data: bytes, var_dir: Path) -> tuple[Path, str]:
    """Save PDF bytes as `$ONUS_VAR_DIR/uploads/<sha256>.pdf`; returns (path, sha256)."""
    if not data.startswith(b"%PDF"):
        raise FetchError("not a PDF")
    sha = hashlib.sha256(data).hexdigest()
    d = var_dir / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sha}.pdf"
    if not p.exists():
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
    return p, sha
