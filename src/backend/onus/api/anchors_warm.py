"""Anchors computed before the first open (Phase 14).

`/api/analyses/{id}/anchors` computes on demand and caches at `$ONUS_VAR_DIR/anchors/<id>.json` (`anchors.py`);
the first request may re-ingest the PDF and take seconds. `AnchorWarmer` fills that cache ahead of time, in one
low-priority worker thread:

- when a live analysis is stored as `done` (`jobs.py`), and again when the code stage rewrites its result
  (`code_stage.py`); the cache stamp holds a digest of the result's anchor-relevant parts, so an unchanged
  result keeps its file and a changed one is recomputed;
- on startup, for every done analysis with a stored PDF whose cache is missing or stale (an `ANCHORS_VERSION`
  bump makes every file stale), one at a time.

Best effort: a failure is logged and ignored. It never changes an analysis's status or result, and the request
path still computes on demand (the per-analysis lock in `anchors_for` makes a request wait for a precompute
already running instead of doing the work twice).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from onus.api.anchors import anchors_for, cache_stamp, cached_anchors, stored_pdf
from onus.db import repo
from onus.db.models import Analysis, Paper
from onus.db.session import session_scope

log = logging.getLogger("onus.anchors")

NICE = 10  # the worker thread's scheduling niceness on Linux (best effort)


def _lower_priority() -> None:
    with contextlib.suppress(AttributeError, OSError):
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), NICE)


class AnchorWarmer:
    def __init__(self, factory: sessionmaker[Session], var_dir: Path, *, enabled: bool = True):
        self.factory = factory
        self.var_dir = var_dir
        self.enabled = enabled
        self._pool: ThreadPoolExecutor | None = None

    def _executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="onus-anchors",
                                            initializer=_lower_priority)
        return self._pool

    def submit(self, analysis_id: str) -> Future | None:
        """Queue one analysis; returns at once. Never raises."""
        if not self.enabled:
            return None
        try:
            return self._executor().submit(self.warm, analysis_id)
        except RuntimeError:  # shut down
            return None

    def warm(self, analysis_id: str) -> bool:
        """Compute and cache one analysis's anchors (blocking). False when skipped or failed; never raises."""
        try:
            with session_scope(self.factory) as s:
                a = repo.get_analysis(s, analysis_id)
                if a is None or a.status != "done" or not a.result:
                    return False
                sha, result = a.paper.pdf_sha256, dict(a.result)
            if stored_pdf(self.var_dir, sha) is None:
                return False
            anchors_for(self.var_dir, analysis_id, result, sha)
            return True
        except Exception as e:  # noqa: BLE001 - best effort; the request path computes on demand
            log.warning("anchors precompute for %s failed: %s", analysis_id, type(e).__name__)
            return False

    def stale(self) -> list[str]:
        """Done analyses with a stored PDF whose anchors cache is missing or stale, oldest first."""
        out: list[str] = []
        with session_scope(self.factory) as s:
            q = (select(Analysis.id, Paper.pdf_sha256).join(Paper, Analysis.paper_id == Paper.id)
                 .where(Analysis.status == "done", Paper.pdf_sha256.is_not(None)).order_by(Analysis.created_at))
            rows = [(aid, sha) for aid, sha in s.execute(q) if stored_pdf(self.var_dir, sha) is not None]
            for aid, sha in rows:
                a = repo.get_analysis(s, aid)
                if a is not None and a.result and cached_anchors(self.var_dir, aid, cache_stamp(a.result, sha)) is None:
                    out.append(aid)
        return out

    def warm_existing(self) -> Future | None:
        """Startup: find stale caches and warm them one at a time, in the worker thread."""
        if not self.enabled:
            return None

        def scan() -> int:
            try:
                ids = self.stale()
            except Exception as e:  # noqa: BLE001
                log.warning("anchors warm-up scan failed: %s", type(e).__name__)
                return 0
            if ids:
                log.info("anchors warm-up: %d analyses", len(ids))
            return sum(self.warm(aid) for aid in ids)

        try:
            return self._executor().submit(scan)
        except RuntimeError:
            return None

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
