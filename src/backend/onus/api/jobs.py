"""Live analysis jobs (BUILD_PLAN Phase 8.3): an in-process asyncio queue with concurrency 1.

Status and progress are persisted on the `analyses` row, so a page reload (or another viewer) sees the same
state. On startup, rows left `queued` or `running` by a previous process are marked `failed` (the queue is in
memory, so they can never finish). Live mode is always 1 run. When live code checks are on (Phase 11b), the
finished P0 report is stored first and then handed to the code stage (`code_stage.py`), which has its own
one-worker queue and updates the report in place. Once a report is stored, its PDF anchors are computed in the
background (`anchors_warm.py`) so the report page's first `/anchors` request reads the cache.

For arXiv and OpenReview papers the display title comes from the source's metadata (`fetch_title`), not from
the title parsed out of the PDF (which can pick up margin stamps and emails); the parsed title is the fallback.

The pipeline itself is injected (`Analyzer`), so tests run the whole lifecycle with a fake pipeline and no
Berget calls. The default analyzer ingests the PDF and calls `onus.pipeline.analyze_doc_runs(..., mode="live")`
with a live-mode `LLMClient` (per-paper cap from ONUS_MAX_EUR_PER_PAPER; the daily caps are in `limits.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from onus.api.code_stage import CodeJob, CodeRunner, discard_upload
from onus.config import Settings
from onus.db import repo
from onus.db.session import session_scope

log = logging.getLogger("onus.jobs")

STAGES = ("fetch", "ingest", "claims", "evidence", "checks", "verdicts")
ProgressFn = Callable[[str, dict], None]
# (pdf_path, paper_id, source, progress) -> RunResult as a JSON-ready dict
Analyzer = Callable[[Path, str, str, ProgressFn], Awaitable[dict[str, Any]]]
# (source, external_id, max_mb) -> PDF bytes
Fetcher = Callable[[str, str, int], bytes]
# (source, external_id) -> display title from the source's metadata, or None
TitleFetcher = Callable[[str, str], "str | None"]


@dataclass
class LiveJob:
    analysis_id: str
    paper_db_id: str
    source: str  # upload | arxiv | openreview
    external_id: str  # the source's id (without the code key of the paper row)
    pdf_path: Path | None = None  # set for uploads; fetched for arxiv / openreview
    code_upload: Path | None = None  # uploaded code .zip in a temporary folder (code checks on only)
    ip_hash: str | None = None

    @property
    def pipeline_paper_id(self) -> str:
        return f"{self.source}:{self.external_id}"


def default_fetcher(source: str, external_id: str, max_mb: int) -> bytes:
    from onus.ingest.fetch import PaperRef, fetch_pdf

    return fetch_pdf(PaperRef(source, external_id), max_mb=max_mb)


def default_title_fetcher(source: str, external_id: str) -> str | None:
    from onus.ingest.fetch import PaperRef, fetch_title

    return fetch_title(PaperRef(source, external_id))


def no_title_fetcher(source: str, external_id: str) -> str | None:
    return None


def make_pipeline_analyzer(settings: Settings, fingerprint: str) -> Analyzer:
    async def analyze(pdf_path: Path, paper_id: str, source: str, progress: ProgressFn) -> dict[str, Any]:
        from onus.ingest.document_builder import ingest_pdf, save_document
        from onus.llm.client import LLMClient
        from onus.pipeline import analyze_doc_runs

        progress("ingest", {})
        t = time.monotonic()
        doc = await asyncio.to_thread(ingest_pdf, pdf_path, settings.var_dir, source=source, paper_id=paper_id)
        ingest_s = time.monotonic() - t
        try:  # the ingest cache: PDF anchors (anchors.load_doc) reuse it instead of re-ingesting the PDF
            await asyncio.to_thread(save_document, doc, settings.var_dir)
        except OSError as e:
            log.warning("could not cache the Document for %s: %s", paper_id, type(e).__name__)
        llm = LLMClient(settings, mode="live")
        result = await analyze_doc_runs(doc, 1, "live", llm=llm, fingerprint=fingerprint, progress=progress,
                                        ingest_s=ingest_s)
        return json.loads(result.model_dump_json())

    return analyze


def public_error(e: BaseException) -> str:
    """A plain message for the UI; never a traceback or internal path."""
    from onus.ingest.fetch import FetchError
    from onus.llm.costs import BudgetExceeded

    if isinstance(e, FetchError):
        return f"Could not download the PDF ({e})."
    if isinstance(e, BudgetExceeded):
        return "The analysis was stopped because it reached its cost cap."
    if isinstance(e, FileNotFoundError):
        return "The PDF could not be found."
    return f"The analysis failed ({type(e).__name__})."


class JobRunner:
    def __init__(self, factory: sessionmaker[Session], settings: Settings, *, analyzer: Analyzer,
                 fetcher: Fetcher = default_fetcher, title_fetcher: TitleFetcher = no_title_fetcher,
                 code: CodeRunner | None = None, anchors: Any = None):
        self.factory = factory
        self.settings = settings
        self.analyzer = analyzer
        self.fetcher = fetcher
        self.title_fetcher = title_fetcher
        self.code = code
        self.anchors = anchors  # AnchorWarmer: PDF anchors computed as soon as a result is stored
        self._queue: asyncio.Queue[LiveJob] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle

    def recover(self) -> int:
        with session_scope(self.factory) as s:
            return repo.fail_stale_running(s, "The server restarted before this analysis finished. Please retry.")

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._worker(), name="onus-live-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    def enqueue(self, job: LiveJob) -> None:
        """Thread-safe: route handlers run in FastAPI's threadpool."""
        if self._loop is None or self._queue is None:
            raise RuntimeError("job runner not started")
        self._loop.call_soon_threadsafe(self._queue.put_nowait, job)

    @property
    def pending(self) -> int:
        return self._queue.qsize() if self._queue else 0

    # ------------------------------------------------------------ work

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            try:
                await self.run(job)
            except Exception:
                log.exception("live job %s crashed", job.analysis_id)
            finally:
                self._queue.task_done()

    def _progress(self, analysis_id: str, history: list[str]) -> ProgressFn:
        def emit(stage: str, info: dict) -> None:
            if stage not in history:
                history.append(stage)
            progress = {"stage": stage, "stages": list(STAGES), "history": list(history),
                        "info": {k: v for k, v in (info or {}).items() if isinstance(v, (int, float, str))}}
            with session_scope(self.factory) as s:
                a = repo.get_analysis(s, analysis_id)
                if a is not None and a.status in ("queued", "running"):
                    repo.set_analysis_status(s, a, "running", progress=progress)

        return emit

    async def _source_title(self, job: LiveJob) -> str | None:
        try:
            return await asyncio.to_thread(self.title_fetcher, job.source, job.external_id)
        except Exception:  # noqa: BLE001 - the parsed title is the fallback
            return None

    async def run(self, job: LiveJob) -> None:
        history: list[str] = []
        progress = self._progress(job.analysis_id, history)
        handed_off = False
        try:
            pdf_path = job.pdf_path
            if pdf_path is None:
                progress("fetch", {"source": job.source})
                data = await asyncio.to_thread(self.fetcher, job.source, job.external_id,
                                               self.settings.live_max_pdf_mb)
                from onus.ingest.fetch import store_upload

                pdf_path, sha = store_upload(data, self.settings.var_dir)
                title = await self._source_title(job)
                with session_scope(self.factory) as s:
                    p = repo.get_paper(s, job.paper_db_id)
                    if p is not None:
                        p.pdf_sha256 = sha
                        if title:
                            p.title = title
            result = await self.analyzer(pdf_path, job.pipeline_paper_id, job.source, progress)
            paper = result.setdefault("paper", {})
            if not paper.get("external_id"):
                paper["external_id"] = job.external_id
            with session_scope(self.factory) as s:
                a = repo.get_analysis(s, job.analysis_id)
                if a is None:
                    return
                p = repo.get_paper(s, job.paper_db_id)
                if p is not None:
                    p.title = p.title or paper.get("title")
                    p.pages = paper.get("pages") or p.pages
                    p.pdf_sha256 = paper.get("pdf_sha256") or p.pdf_sha256
                from onus.api.limits import today

                code_on = self.code is not None and self.code.enabled
                a.progress = {"stage": "done", "stages": list(STAGES), "history": history + ["done"], "info": {},
                              **({"code": {"status": "queued", "day": today().isoformat(),
                                           "ahead": self.code.pending}} if code_on else {})}
                repo.store_result(s, a, result)  # P0 is published here, before any code check
                repo.add_live_usage(s, today(), cost_eur=float((result.get("cost") or {}).get("eur") or 0.0))
            if self.anchors is not None:
                self.anchors.submit(job.analysis_id)  # a worker thread; failures are logged there, never raised
            if code_on:
                assert self.code is not None
                self.code.submit(CodeJob(analysis_id=job.analysis_id, paper_id=job.pipeline_paper_id,
                                         source=job.source, pdf_path=pdf_path, upload=job.code_upload,
                                         ip_hash=job.ip_hash))
                handed_off = True
        except Exception as e:  # noqa: BLE001 - a failed paper is a status, not a crash
            log.warning("live job %s failed: %s", job.analysis_id, type(e).__name__)
            with session_scope(self.factory) as s:
                a = repo.get_analysis(s, job.analysis_id)
                if a is not None and a.status != "done":
                    repo.set_analysis_status(s, a, "failed", error=public_error(e))
        finally:
            if not handed_off:
                discard_upload(job.code_upload)
