"""Live code checks (Phase 11b): a separate stage with its own one-worker queue, after the P0 report is stored.

The P0 report is published first. The code stage then:

1. gets the code: the .zip the visitor uploaded with the paper, else the paper's first supported repository link
   (GitHub, GitLab, Hugging Face; the repository's default branch, downloaded as an archive), else nothing;
2. extracts it into a temporary folder of the container (never the `ONUS_VAR_DIR` volume) with the same
   `safe_unzip` and `index_tree` the corpus uses, under the size, file-count and time caps in `limits.py`;
3. calls the frozen code-v1 functions unchanged: `static_check`, `rerun_paper` (Modal), `apply_code`;
4. replaces the stored result with the code-checked one, and deletes the temporary folder whatever happened.

Code is only read on the server; it runs only inside the Modal sandbox (`rerun_paper`). A failed code stage never
touches the P0 report. The stage's state lives in `analyses.progress["code"]` (status, code source, re-run use,
spend): the report page polls it and shows "Code checks running..." until the result updates in place.

Limits (`CodeLimits`): the daily live cost cap (code-stage Berget spend counts toward LIVE_MAX_COST_EUR_PER_DAY),
re-runs per day, Modal minutes per day and re-runs per visitor per day. When a re-run limit is hit, the static
check still runs and the re-run is recorded as skipped with the reason. The per-paper caps stay: the re-run
agent's own `max_agent_eur`, ONUS_MAX_EUR_PER_PAPER and ONUS_RERUN_MAX_MINUTES.

Secrets (CLAUDE.md rule 7): this module never handles a key. `rerun_paper` passes the SANDBOX_* keys, as a Modal
Secret, only to code that calls those providers. Everything stored is masked again here: every known secret
value anywhere in the result, and key-shaped strings in the code section and the stage record.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from onus.api.limits import (
    CODE_MAX_FILES,
    CODE_UNZIPPED_MAX_MB,
    REPO_DOWNLOAD_TIMEOUT_S,
    REPO_ZIP_MAX_MB,
    CodeLimits,
    today,
)
from onus.config import Settings
from onus.db import repo
from onus.db.session import session_scope

log = logging.getLogger("onus.code_stage")

CODE_KEY = "+code-"  # paper external_id suffix for an analysis with uploaded code: "<id>+code-<sha12>"
TMP_PREFIX = "onus-code-"
SUPPORTED_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "huggingface.co": "huggingface"}
HF_MAX_FILES = 500  # Hugging Face has no archive download: files are fetched one by one


class CodeFetchError(RuntimeError):
    """Raised with a plain-language message for the report."""


def code_key(sha256: str) -> str:
    return f"{CODE_KEY}{sha256[:12]}"


def base_external_id(external_id: str | None) -> str | None:
    return external_id.split(CODE_KEY, 1)[0] if external_id else external_id


# ---------------------------------------------------------------- repository links in the paper text

_HOSTS = r"(?:github\.com|gitlab\.com|huggingface\.co)"
_SEG = r"[A-Za-z0-9][A-Za-z0-9_.\-]*"
# "https://github.com/ tensorflow/tensor2tensor": the break falls after the host
_SPLIT_AFTER_HOST = re.compile(rf"(https?://(?:www\.)?{_HOSTS}/)\s+(?={_SEG}/{_SEG})", re.IGNORECASE)
# "https://github.com/tensorflow/ tensor2tensor": the break falls after the owner
_SPLIT_AFTER_OWNER = re.compile(rf"(https?://(?:www\.)?{_HOSTS}/{_SEG}/)\s+(?={_SEG})", re.IGNORECASE)
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}$")
_GITHUB_RESERVED = {"orgs", "settings", "features", "topics", "sponsors", "marketplace", "about", "collections",
                    "login", "join", "explore", "issues", "pulls", "search", "apps", "site", "pricing"}
_HF_RESERVED = {"docs", "papers", "blog", "models", "learn", "api", "tasks", "pricing", "join", "login", "posts",
                "organizations", "collections", "settings"}


def rejoin_split_links(text: str) -> str:
    """Rejoin repository links that the PDF broke across whitespace or a line break."""
    text = _SPLIT_AFTER_HOST.sub(r"\1", text)
    return _SPLIT_AFTER_OWNER.sub(r"\1", text)


@dataclass(frozen=True)
class RepoRef:
    host: str  # github | gitlab | huggingface
    path: str  # owner/repo (GitLab: group/.../repo; Hugging Face: [datasets/|spaces/]owner/repo)

    @property
    def url(self) -> str:
        domain = next(d for d, h in SUPPORTED_HOSTS.items() if h == self.host)
        return f"https://{domain}/{self.path}"


def _clean_seg(s: str) -> str:
    s = s.rstrip(".,;:")
    return s[:-4] if s.lower().endswith(".git") else s


def parse_repo(url: str) -> RepoRef | None:
    """A supported repository (owner/repo shape checked), or None."""
    m = re.match(r"^https?://(?:www\.)?([^/\s]+)/(.*)$", url.strip(), re.IGNORECASE)
    if not m:
        return None
    host = SUPPORTED_HOSTS.get(m.group(1).lower())
    parts = [p for p in re.split(r"[/?#]", m.group(2)) if p]
    if host is None or len(parts) < 2:
        return None
    if host == "github":
        owner, name = parts[0], _clean_seg(parts[1])
        if owner.lower() in _GITHUB_RESERVED or not (_OWNER_RE.match(owner) and _OWNER_RE.match(name)):
            return None
        return RepoRef("github", f"{owner}/{name}")
    if host == "gitlab":
        segs: list[str] = []
        for p in parts:
            if p in ("-", "tree", "blob", "raw", "issues", "merge_requests", "wikis"):
                break
            segs.append(_clean_seg(p))
        if len(segs) < 2 or not all(_OWNER_RE.match(s) for s in segs):
            return None
        return RepoRef("gitlab", "/".join(segs))
    kind = ""
    if parts[0] in ("datasets", "spaces"):
        kind, parts = parts[0] + "/", parts[1:]
    if len(parts) < 2:
        return None
    owner, name = parts[0], _clean_seg(parts[1])
    if (not kind and owner.lower() in _HF_RESERVED) or not (_OWNER_RE.match(owner) and _OWNER_RE.match(name)):
        return None
    return RepoRef("huggingface", f"{kind}{owner}/{name}")


def find_repo_links(text: str) -> tuple[list[RepoRef], list[str]]:
    """(supported repositories in paper order, links to other code hosts) from the paper text."""
    from onus.ingest.code_index import repo_urls

    supported: list[RepoRef] = []
    other: list[str] = []
    for u in repo_urls(rejoin_split_links(text)):
        m = re.match(r"^https?://(?:www\.)?([^/\s]+)", u, re.IGNORECASE)
        domain = m.group(1).lower() if m else ""
        if domain in SUPPORTED_HOSTS:
            r = parse_repo(u)
            if r is not None and r not in supported:
                supported.append(r)
        elif u not in other:
            other.append(u)
    return supported, other


# ---------------------------------------------------------------- getting the code (temporary folder only)


def _stream_to(client, url: str, dest: Path, max_bytes: int, deadline: float, what: str) -> None:
    import httpx

    try:
        with client.stream("GET", url) as r:
            if r.status_code == 404:
                raise CodeFetchError(f"{what} was not found (it may be private or deleted).")
            if r.status_code != 200:
                raise CodeFetchError(f"{what} could not be downloaded (HTTP {r.status_code}).")
            if int(r.headers.get("content-length") or 0) > max_bytes:
                raise CodeFetchError(f"{what} is larger than {max_bytes // (1024 * 1024)} MB.")
            n = 0
            with dest.open("wb") as f:
                for chunk in r.iter_bytes():
                    n += len(chunk)
                    if n > max_bytes:
                        raise CodeFetchError(f"{what} is larger than {max_bytes // (1024 * 1024)} MB.")
                    if time.monotonic() > deadline:
                        raise CodeFetchError(f"{what} took too long to download.")
                    f.write(chunk)
    except httpx.HTTPError as e:
        raise CodeFetchError(f"{what} could not be downloaded ({type(e).__name__}).") from e


def _single_top(root: Path) -> Path:
    """Repository archives wrap everything in one top folder (`repo-<sha>/`): index from inside it."""
    kids = [p for p in root.iterdir() if not p.name.startswith(".")]
    return kids[0] if len(kids) == 1 and kids[0].is_dir() else root


def extract_zip(zip_path: Path, dest: Path, what: str = "The code .zip") -> Path:
    """`safe_unzip` (the corpus's own extractor) with the live caps; returns the folder to index."""
    from onus.ingest.code_index import UnsafeArchiveError, safe_unzip

    try:
        with zipfile.ZipFile(zip_path) as zf:
            if len(zf.infolist()) > CODE_MAX_FILES:
                raise CodeFetchError(f"{what} has more than {CODE_MAX_FILES} files.")
        safe_unzip(zip_path, dest, max_bytes=CODE_UNZIPPED_MAX_MB * 1024 * 1024)
    except (UnsafeArchiveError, zipfile.BadZipFile, OSError) as e:
        raise CodeFetchError(f"{what} could not be extracted safely ({str(e)[:120]}).") from e
    finally:
        zip_path.unlink(missing_ok=True)
    return _single_top(dest)


def _hf_files(client, ref: RepoRef, dest: Path, deadline: float) -> None:
    import httpx

    from onus.ingest.code_index import BINARY_EXT, MAX_TEXT_BYTES, SKIP_DIRS

    kind, _, rest = ref.path.partition("/") if ref.path.startswith(("datasets/", "spaces/")) else ("", "", ref.path)
    api = f"https://huggingface.co/api/{kind or 'models'}/{rest}?blobs=true"
    try:
        r = client.get(api)
    except httpx.HTTPError as e:
        raise CodeFetchError(f"The repository {ref.url} could not be listed ({type(e).__name__}).") from e
    if r.status_code != 200:
        raise CodeFetchError(f"The repository {ref.url} was not found (HTTP {r.status_code}).")
    info = r.json() or {}
    rev = info.get("sha") or "main"
    binary = {e.lower() for e in BINARY_EXT}
    files = [s for s in info.get("siblings") or [] if isinstance(s, dict) and isinstance(s.get("rfilename"), str)]
    total, n = 0, 0
    prefix = f"{kind}/" if kind else ""
    for sib in files:
        name = sib["rfilename"]
        p = PurePosixPath(name)
        if name.startswith("/") or ".." in p.parts or SKIP_DIRS & set(p.parts) or p.suffix.lower() in binary:
            continue
        size = int(sib.get("size") or 0)
        if size > MAX_TEXT_BYTES:
            continue
        n += 1
        total += size
        if n > HF_MAX_FILES or total > CODE_UNZIPPED_MAX_MB * 1024 * 1024:
            raise CodeFetchError(f"The repository {ref.url} has too many files to check.")
        target = dest / Path(*p.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        _stream_to(client, f"https://huggingface.co/{prefix}{rest}/resolve/{rev}/{name}", target, MAX_TEXT_BYTES,
                   deadline, f"The file {name}")


def download_repo(ref: RepoRef, dest: Path, *, client=None, timeout: float = REPO_DOWNLOAD_TIMEOUT_S) -> Path:
    """Download the repository's default branch into `dest` (a temporary folder); returns the folder to index."""
    from urllib.parse import quote

    import httpx

    own = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": "onus-code-fetch/0.1"})
    deadline = time.monotonic() + timeout
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if ref.host == "huggingface":
            root = dest / "code"
            _hf_files(client, ref, root, deadline)
            return root
        url = (f"https://github.com/{ref.path}/archive/HEAD.zip" if ref.host == "github" else
               f"https://gitlab.com/api/v4/projects/{quote(ref.path, safe='')}/repository/archive.zip")
        zpath = dest / "repo.zip"
        _stream_to(client, url, zpath, REPO_ZIP_MAX_MB * 1024 * 1024, deadline, f"The repository {ref.url}")
        return extract_zip(zpath, dest / "code", f"The repository archive of {ref.url}")
    finally:
        if own:
            client.close()


def save_upload_zip(data: bytes, tmp_root: Path | None = None) -> Path:
    """Hold an uploaded code .zip in a temporary folder until the code stage runs (deleted by the stage)."""
    d = Path(tempfile.mkdtemp(prefix=TMP_PREFIX + "upload-", dir=tmp_root))
    p = d / "code.zip"
    p.write_bytes(data)
    return p


def discard_upload(path: Path | None) -> None:
    if path is not None:
        shutil.rmtree(path.parent, ignore_errors=True)


# ---------------------------------------------------------------- secrets


def secret_values(settings: Settings) -> list[str]:
    """Every secret value this server holds (Berget, Modal, SANDBOX_*, HF_TOKEN...). Never printed."""
    vals = [settings.berget_api_key, settings.modal_token_id, settings.modal_token_secret,
            settings.sandbox_openai_api_key, settings.sandbox_anthropic_api_key, settings.sandbox_gemini_api_key]
    out = [v.get_secret_value() for v in vals if v.get_secret_value()]
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "BERGET_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET",
                 "SANDBOX_OPENAI_API_KEY", "SANDBOX_ANTHROPIC_API_KEY", "SANDBOX_GEMINI_API_KEY"):
        if os.environ.get(name):
            out.append(os.environ[name])
    with contextlib.suppress(Exception):
        from onus.codecheck.common import known_secret_values

        out += known_secret_values()
    return sorted({v for v in out if len(v) >= 6}, key=len, reverse=True)


def mask_tree(obj: Any, known: list[str], *, patterns: bool) -> Any:
    """Mask every string in a JSON-like tree: known secret values always; key-shaped strings when `patterns`."""
    from onus.codecheck.common import MASK, mask_secrets

    if isinstance(obj, str):
        if patterns:
            return mask_secrets(obj, known)
        for k in known:
            obj = obj.replace(k, MASK)
        return obj
    if isinstance(obj, dict):
        return {k: mask_tree(v, known, patterns=patterns) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_tree(v, known, patterns=patterns) for v in obj]
    return obj


def mask_result(result: dict, known: list[str]) -> dict:
    out = mask_tree(result, known, patterns=False)
    if isinstance(out.get("code"), dict):
        out["code"] = mask_tree(out["code"], known, patterns=True)
    return out


# ---------------------------------------------------------------- the code-check functions (injectable)


@dataclass
class CodeDeps:
    """The frozen code-v1 functions behind small adapters, so tests can swap in fakes (no Berget, no Modal)."""

    ingest: Callable[[Path, str, str], Any]  # (pdf, paper id, source) -> Document
    llm: Callable[[], Any]  # -> an LLMClient (its ledger gives the stage's spend)
    static: Callable[[Any, Any], Awaitable[Any]]  # (doc, llm) -> StaticReport
    rerun: Callable[[Any, dict, Any, float], Awaitable[tuple[dict, list[dict]]]]  # (doc, p0, llm, minutes)
    skip_rerun: Callable[[Any, dict, str], tuple[dict, list[dict]]]  # (doc, p0, reason) -> (report, comparisons)
    apply: Callable[[dict, Any, Any, list[dict], list[dict]], dict]  # (p0, doc, static, reruns, cmps) -> result
    download: Callable[[RepoRef, Path], Path]  # (repo, temp folder) -> folder to index


def default_deps(settings: Settings) -> CodeDeps:
    def ingest(pdf: Path, paper_id: str, source: str):
        from onus.ingest.document_builder import ingest_pdf

        return ingest_pdf(pdf, settings.var_dir, source=source, paper_id=paper_id)

    def llm():
        from onus.codecheck.version import code_prompts_dir
        from onus.llm.client import LLMClient
        from onus.llm.prompts import PromptRegistry

        return LLMClient(settings, mode="live", prompts=PromptRegistry(code_prompts_dir()))

    async def static(doc, client):
        from onus.codecheck.static import static_check

        return await static_check(doc, None, client)

    async def rerun(doc, p0: dict, client, minutes: float):
        from onus.codecheck.rerun.run import rerun_paper
        from onus.schemas.result import RunResult

        return await rerun_paper(doc, RunResult.model_validate(p0), None, client, settings=settings,
                                 max_minutes=minutes)

    def skip_rerun(doc, p0: dict, reason: str):
        from onus.codecheck.common import rubric
        from onus.codecheck.rerun.agent import select_targets
        from onus.codecheck.rerun.compare import compare
        from onus.schemas.result import RunResult

        targets = select_targets(RunResult.model_validate(p0), int(rubric("rerun.yaml")["max_targets"]))
        rep = {"status": "skipped", "reason": reason, "commands": [], "modifications": [], "stdout_tail": "",
               "extracted": [], "targets": targets, "minutes": 0.0}
        return rep, compare(targets, [], [], why_none=reason)

    def apply(p0: dict, doc, st, reruns: list[dict], cmps: list[dict]) -> dict:
        from onus.codecheck.apply import apply_code
        from onus.codecheck.common import rubric
        from onus.codecheck.version import code_version_label
        from onus.schemas.result import RunResult

        scfg = rubric("static.yaml")
        res = apply_code(RunResult.model_validate(p0), doc, st, reruns, cmps, code_version=code_version_label(),
                         min_shared=int(scfg["min_shared_terms"]), claim_types=tuple(scfg["claim_types"]))
        return json.loads(res.model_dump_json())

    return CodeDeps(ingest=ingest, llm=llm, static=static, rerun=rerun, skip_rerun=skip_rerun, apply=apply,
                    download=lambda ref, dest: download_repo(ref, dest))


# ---------------------------------------------------------------- the stage


@dataclass
class CodeJob:
    analysis_id: str
    paper_id: str  # pipeline paper id, "source:external_id"
    source: str
    pdf_path: Path
    upload: Path | None = None  # uploaded code .zip in a temporary folder
    ip_hash: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _spent(llm: Any) -> float:
    try:
        return float(sum(llm.ledger.paper_eur.values()))
    except Exception:  # noqa: BLE001 - a fake or missing ledger costs nothing
        return 0.0


@dataclass
class CodeRunner:
    factory: sessionmaker[Session]
    settings: Settings
    limits: CodeLimits
    enabled: bool
    deps: CodeDeps
    tmp_root: Path | None = None
    anchors: Any = None  # AnchorWarmer: re-warms the PDF anchors when a code-checked result is saved
    _queue: asyncio.Queue[CodeJob] | None = field(default=None, init=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False)
    _task: asyncio.Task | None = field(default=None, init=False)

    # ------------------------------------------------------------ lifecycle

    def recover(self) -> int:
        """Code stages left queued or running by a restart become failed (their P0 report stays)."""
        n = 0
        with session_scope(self.factory) as s:
            for a in repo.live_done_with_progress(s):
                code = (a.progress or {}).get("code") or {}
                if code.get("status") in ("queued", "running"):
                    a.progress = {**a.progress, "code": {**code, "status": "failed", "finished_at": _now(),
                                                         "message": "The server restarted before the code checks "
                                                                    "finished. The report above is complete."}}
                    n += 1
        root = Path(self.tmp_root or tempfile.gettempdir())
        with contextlib.suppress(OSError):
            for d in root.glob(TMP_PREFIX + "*"):
                if d.is_dir() and time.time() - d.stat().st_mtime > 6 * 3600:
                    shutil.rmtree(d, ignore_errors=True)
        return n

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._worker(), name="onus-code-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    def submit(self, job: CodeJob) -> None:
        """Called from the P0 worker (same event loop)."""
        if self._queue is None:
            discard_upload(job.upload)
            raise RuntimeError("code runner not started")
        self._queue.put_nowait(job)

    @property
    def pending(self) -> int:
        return self._queue.qsize() if self._queue else 0

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            try:
                await self.run(job)
            except Exception:
                log.exception("code stage %s crashed", job.analysis_id)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------ state

    def _save(self, analysis_id: str, rec: dict, result: dict | None = None, eur: float = 0.0) -> None:
        known = secret_values(self.settings)
        rec = mask_tree(rec, known, patterns=True)
        with session_scope(self.factory) as s:
            a = repo.get_analysis(s, analysis_id)
            if a is None or a.status != "done":
                return
            if result is not None:
                result = mask_result(result, known)
                a.result = result
                a.summary = repo.listing_summary(result)
                a.cost_eur = float(a.cost_eur or 0.0) + eur
            a.progress = {**(a.progress or {}), "code": rec}
            if eur:
                repo.add_live_usage(s, today(), cost_eur=eur)
        if result is not None and self.anchors is not None:
            self.anchors.submit(analysis_id)  # after the commit; recomputed only if the anchor digest changed

    # ------------------------------------------------------------ work

    async def _get_code(self, job: CodeJob, doc: Any, work: Path, rec: dict) -> Path | None:
        if job.upload is not None:
            rec["source"] = {"kind": "upload", "fetched_at": today().isoformat()}
            return await asyncio.to_thread(extract_zip, job.upload, work / "code")
        repos, other = find_repo_links(doc.text or "")
        if not repos:
            rec["source"] = {"kind": "none", "unsupported": other[:5]}
            if other:
                rec["message"] = "Repository host not supported: " + ", ".join(other[:3])
            return None
        last: CodeFetchError | None = None
        for ref in repos[:3]:  # the first supported link that downloads
            rec["source"] = {"kind": "repo", "url": ref.url, "host": ref.host, "fetched_at": today().isoformat()}
            try:
                return await asyncio.to_thread(self.deps.download, ref, work / ref.host)
            except CodeFetchError as e:
                last = e
        raise last or CodeFetchError("The repository could not be downloaded.")

    async def run(self, job: CodeJob) -> None:
        rec: dict[str, Any] = {"status": "running", "day": today().isoformat(), "started_at": _now()}
        work = Path(tempfile.mkdtemp(prefix=TMP_PREFIX, dir=self.tmp_root))
        llm = None
        eur_saved = False
        try:
            self._save(job.analysis_id, rec)
            with session_scope(self.factory) as s:
                usage = repo.get_live_usage(s, today())
                a = repo.get_analysis(s, job.analysis_id)
                p0 = dict(a.result) if a is not None and a.result else None
            if p0 is None:  # the analysis was reset or removed meanwhile: nothing to update
                rec["status"] = "failed"
                return
            if usage.cost_eur >= self.settings.live_max_cost_eur_per_day:
                rec |= {"status": "skipped", "message": "Code checks skipped: today's budget for live analyses is "
                                                        "used up. The report above is complete."}
                return
            doc = await asyncio.to_thread(self.deps.ingest, job.pdf_path, job.paper_id, job.source)
            root = await self._get_code(job, doc, work, rec)
            if root is None:
                rec["status"] = "no_code"
                return
            from onus.ingest.code_index import index_tree

            idx = await asyncio.to_thread(index_tree, root)
            if not idx.present:
                rec |= {"status": "no_code", "message": "The code has no source files that Onus can read."}
                return
            idx.repo_urls_in_paper = list(doc.code.repo_urls_in_paper)
            doc = doc.model_copy(update={"code": idx})
            p0 = {**p0, "code": {**(p0.get("code") or {}), "available": True, "files": len(idx.files)}}
            rec["files"] = len(idx.files)

            llm = self.deps.llm()
            st = await self.deps.static(doc, llm)
            rec["static"] = {"status": st.status, "reason": st.reason}
            with session_scope(self.factory) as s:
                minutes, why = self.limits.rerun_minutes(s, job.ip_hash)
            if minutes is None:
                rep, cmps = self.deps.skip_rerun(doc, p0, why or "Skipped.")
                rec["rerun"] = {"attempted": False, "skipped": why, "minutes": 0.0}
            else:
                rec["rerun"] = {"attempted": True, "minutes": 0.0, "max_minutes": round(minutes, 1)}
                self._save(job.analysis_id, rec)  # counted before it starts: the daily caps see it
                t0 = time.monotonic()
                try:
                    rep, cmps = await self.deps.rerun(doc, p0, llm, minutes)
                finally:
                    rec["rerun"]["minutes"] = round((time.monotonic() - t0) / 60, 2)
                if rep.get("status") == "not_run" and str(rep.get("reason", "")).startswith("needs external"):
                    rep = {**rep, "status": "needs_external"}  # a provider key (SANDBOX_*) is not set
                if not float(rep.get("minutes") or 0.0) and rep.get("status") in ("not_run", "needs_external"):
                    rec["rerun"]["attempted"] = False  # stopped before any sandbox started
                else:
                    rec["rerun"]["minutes"] = round(max(float(rep.get("minutes") or 0.0),
                                                        rec["rerun"]["minutes"]), 2)
                rec["rerun"]["status"] = rep.get("status")
            result = self.deps.apply(p0, doc, st, [rep], cmps)
            eur = _spent(llm)
            rec |= {"status": "done", "eur": round(eur, 4), "finished_at": _now()}
            self._save(job.analysis_id, rec, result=result, eur=eur)
            eur_saved = True
        except CodeFetchError as e:
            rec |= {"status": "failed", "message": f"{e} The report above is complete."}
        except Exception as e:  # noqa: BLE001 - a failed code stage is a status; the P0 report stays
            from onus.llm.costs import BudgetExceeded

            log.warning("code stage %s failed: %s", job.analysis_id, type(e).__name__)
            msg = ("The code checks were stopped because they reached their cost cap." if isinstance(e, BudgetExceeded)
                   else f"The code checks failed ({type(e).__name__}).")
            rec |= {"status": "failed", "message": msg + " The report above is complete."}
        finally:
            shutil.rmtree(work, ignore_errors=True)
            discard_upload(job.upload)
            if not eur_saved:
                eur = _spent(llm) if llm is not None else 0.0
                rec.setdefault("finished_at", _now())
                rec["eur"] = round(eur, 4)
                with contextlib.suppress(Exception):
                    self._save(job.analysis_id, rec, eur=eur)


def public_code_stage(progress: dict | None) -> dict | None:
    """The code-stage record as the API shows it (no day bookkeeping)."""
    code = (progress or {}).get("code")
    if not isinstance(code, dict):
        return None
    keep = ("status", "message", "source", "files", "static", "rerun", "started_at", "finished_at")
    out = {k: code.get(k) for k in keep if k in code}
    if isinstance(out.get("rerun"), dict):
        out["rerun"] = {k: v for k, v in out["rerun"].items() if k in ("attempted", "skipped", "minutes", "status")}
    return out
