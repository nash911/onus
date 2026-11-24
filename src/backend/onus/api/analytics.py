"""Product analytics: what visitors do in the web app.

Two sources, both written in batches by `AnalyticsSink` so requests never wait on the database:

- `request_log`: one row per page or API request, recorded by `RequestLogMiddleware` (server side, so usage is
  visible even when the browser sends nothing);
- `events`: interaction events the frontend sends to `POST /api/events` (claim opened, evidence clicked, tab
  switched, export, time on page, ...).

Privacy: no cookies and no free text. The session id is random per browser tab (sessionStorage); the visitor is
the salted IP hash already used for rate limits, truncated to 16 hex characters; event properties are limited
to the whitelisted keys in `EVENT_TYPES`. Requests with `DNT: 1` or `Sec-GPC: 1` are not recorded. Rows older
than `ANALYTICS_RETENTION_DAYS` are deleted.

`GET /api/admin/analytics` exports rows for the author's local dashboard. It needs the `ANALYTICS_TOKEN`
variable and returns 404 without it, so the endpoint does not exist unless the author turns it on.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware

from onus.api.context import Ctx
from onus.api.limits import client_ip, hash_ip
from onus.db.models import Analysis, Dispute, Event, LiveRequest, Paper, RequestLog
from onus.db.session import session_scope

log = logging.getLogger("onus.api.analytics")

router = APIRouter(prefix="/api")

# event type -> allowed property keys and their types; anything else is dropped
_S, _I, _F, _B = str, int, float, bool
EVENT_TYPES: dict[str, dict[str, type]] = {
    "page_view": {"route": _S, "referrer": _S, "screen": _S, "returning": _B},
    "submit": {"kind": _S, "source": _S, "ref": _S, "has_code": _B, "pdf_kb": _I},
    "submit_result": {"status": _S, "cached": _B, "http": _I},
    "example_click": {"ref": _S, "where": _S},
    "progress_view": {"stage": _S},
    "report_open": {"status": _S, "mode": _S, "n_claims": _I, "flagged": _I, "has_code": _B},
    "claim_open": {"claim_id": _S, "verdict": _S, "is_main": _B, "via": _S},
    "evidence_click": {"claim_id": _S, "evidence_id": _S, "kind": _S, "method": _S},
    "margin_mark_click": {"claim_id": _S, "verdict": _S},
    "tab_switch": {"tab": _S},
    "filter": {"kind": _S, "value": _S, "on": _B},
    "search": {"length": _I, "results": _I},
    "pdf_zoom": {"zoom": _F, "mode": _S},
    "pdf_page": {"page": _I, "pages": _I},
    "export": {"format": _S},
    "dispute_open": {"claim_id": _S},
    "dispute_submit": {"claim_id": _S, "with_contact": _B},
    "outbound_click": {"target": _S},
    "heartbeat": {"seconds": _I},
    "client_error": {"message": _S, "where": _S},
}
MAX_EVENTS_PER_POST = 50
MAX_BODY_BYTES = 32 * 1024
MAX_STR = 120
SESSION_RE = re.compile(r"^[A-Za-z0-9-]{8,36}$")
ID_RE = re.compile(r"^[A-Za-z0-9-]{1,36}$")
# per-session budget: at most this many events in a sliding 10-minute window
SESSION_BUDGET = 600
SESSION_WINDOW_S = 600.0

# requests worth logging: SPA pages and API calls; not polling, not static assets, not this module's endpoints
_SKIP_PREFIXES = ("/assets/", "/api/events", "/api/admin/", "/api/health", "/favicon", "/robots.txt")
_SKIP_SUFFIXES = ("/status",)
_STATIC_EXT = re.compile(r"\.(js|css|map|png|jpg|jpeg|gif|svg|ico|webp|woff2?|ttf|txt|json|webmanifest)$")


def visitor_id(request: Request, salt: bytes) -> str:
    return hash_ip(client_ip(request), salt)[:16]


def opted_out(request: Request) -> bool:
    return request.headers.get("dnt") == "1" or request.headers.get("sec-gpc") == "1"


# Substrings of crawler, scraper, preview and HTTP-library user agents (lower case).
BOT_UA_MARKERS = (
    "bot", "crawl", "spider", "slurp", "scrape", "fetcher", "preview", "headless", "phantomjs", "puppeteer",
    "playwright", "selenium", "lighthouse", "pagespeed", "facebookexternalhit", "embedly", "exauser", "exa.ai",
    "perplexity", "chatgpt", "gptbot", "anthropic", "claude-web", "ccbot", "bytespider", "petalbot", "ahrefs",
    "semrush", "dataforseo", "mj12", "zgrab", "masscan", "nmap", "nuclei", "censys", "shodan", "expanse",
    "curl", "wget", "python", "httpx", "aiohttp", "go-http-client", "okhttp", "java/", "node-fetch", "axios",
    "undici", "libwww", "scrapy", "httpclient", "postman", "insomnia",
)
# How long a visitor stays a bot after a crawler user agent was seen from it: crawlers often render the page
# with an ordinary-looking browser from the same address a moment later.
BOT_MEMORY_S = 600.0


def client_kind(user_agent: str) -> str:
    ua = user_agent.lower().strip()
    if not ua:
        return "unknown"
    if any(b in ua for b in BOT_UA_MARKERS):
        return "bot"
    if not ua.startswith("mozilla/") and not ua.startswith("opera/"):  # every real browser sends Mozilla/5.0
        return "bot"
    if any(m in ua for m in ("mobile", "android", "iphone", "ipad")):
        return "mobile"
    return "desktop"


def referrer_host(request: Request) -> str | None:
    ref = request.headers.get("referer") or ""
    if not ref:
        return None
    host = (urlsplit(ref).hostname or "")[:128]
    own = (request.headers.get("host") or "").split(":")[0]
    return None if not host or host == own else host


def clean_props(etype: str, props: Any) -> dict | None:
    allowed = EVENT_TYPES[etype]
    if not isinstance(props, dict):
        return None
    out: dict[str, Any] = {}
    for k, kind in allowed.items():
        if k not in props:
            continue
        v = props[k]
        if kind is _B and isinstance(v, bool):
            out[k] = v
        elif kind is _I and isinstance(v, int | float) and not isinstance(v, bool):
            out[k] = int(max(-1e9, min(1e9, v)))
        elif kind is _F and isinstance(v, int | float) and not isinstance(v, bool):
            out[k] = round(float(max(-1e9, min(1e9, v))), 3)
        elif kind is _S and isinstance(v, str):
            out[k] = v[:MAX_STR]
    return out or None


def clean_events(payload: Any) -> tuple[str, list[dict]] | None:
    """Validate a POST body: `{session, events: [{type, path?, analysis_id?, props?}]}`. Unknown types and keys
    are dropped; returns None when the body is not usable at all."""
    if not isinstance(payload, dict):
        return None
    session = payload.get("session")
    events = payload.get("events")
    if not isinstance(session, str) or not SESSION_RE.match(session) or not isinstance(events, list):
        return None
    out = []
    for e in events[:MAX_EVENTS_PER_POST]:
        if not isinstance(e, dict) or e.get("type") not in EVENT_TYPES:
            continue
        path = e.get("path")
        aid = e.get("analysis_id")
        ago = e.get("ago")
        ago = int(min(max(ago, 0), 600_000)) if isinstance(ago, int | float) and not isinstance(ago, bool) else 0
        out.append({"type": e["type"], "ago_ms": ago,
                    "path": path[:255] if isinstance(path, str) else None,
                    "analysis_id": aid if isinstance(aid, str) and ID_RE.match(aid) else None,
                    "props": clean_props(e["type"], e.get("props"))})
    return session, out


class SessionBudget:
    """Sliding-window event budget per session id (in memory; resets on restart)."""

    def __init__(self, budget: int = SESSION_BUDGET, window_s: float = SESSION_WINDOW_S) -> None:
        self.budget, self.window = budget, window_s
        self._seen: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def take(self, session: str, n: int, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._seen[session]
            while q and now - q[0] > self.window:
                q.popleft()
            n = max(0, min(n, self.budget - len(q)))
            q.extend([now] * n)
            if len(self._seen) > 50_000:  # drop idle sessions
                for k in [k for k, v in self._seen.items() if not v or now - v[-1] > self.window]:
                    del self._seen[k]
            return n


class AnalyticsSink:
    """Buffers rows in memory and writes them in batches from a background task. Never raises into a request."""

    def __init__(self, factory: sessionmaker[Session], *, enabled: bool = True, retention_days: int = 90,
                 flush_every_s: float = 3.0, max_buffer: int = 20_000) -> None:
        self.factory = factory
        self.enabled = enabled
        self.retention_days = retention_days
        self.flush_every_s = flush_every_s
        self.max_buffer = max_buffer
        self.budget = SessionBudget()
        self._rows: list[Event | RequestLog] = []
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self._last_purge = 0.0
        self.dropped = 0
        self._bot_seen: dict[str, float] = {}  # visitor -> monotonic time a crawler user agent was last seen

    def classify(self, visitor: str, user_agent: str, now: float | None = None) -> str:
        """`client_kind`, except that a visitor seen with a crawler user agent in the last `BOT_MEMORY_S` seconds
        stays a bot (a crawler's page renderer looks like a browser)."""
        now = time.monotonic() if now is None else now
        kind = client_kind(user_agent)
        with self._lock:
            if kind == "bot":
                self._bot_seen[visitor] = now
                if len(self._bot_seen) > 10_000:
                    self._bot_seen = {k: t for k, t in self._bot_seen.items() if now - t < BOT_MEMORY_S}
            elif now - self._bot_seen.get(visitor, -1e18) < BOT_MEMORY_S:
                kind = "bot"
        return kind

    def add(self, row: Event | RequestLog) -> None:
        if not self.enabled:
            return
        with self._lock:
            if len(self._rows) >= self.max_buffer:
                self.dropped += 1
                return
            self._rows.append(row)

    def flush(self) -> int:
        with self._lock:
            rows, self._rows = self._rows, []
        if not rows:
            return 0
        try:
            with session_scope(self.factory) as s:
                s.add_all(rows)
            return len(rows)
        except Exception as e:  # noqa: BLE001 - analytics must never break the app
            log.warning("analytics: could not write %d rows: %s", len(rows), type(e).__name__)
            return 0

    def purge(self, now: datetime | None = None) -> None:
        cutoff = (now or datetime.now(UTC)) - timedelta(days=self.retention_days)
        try:
            with session_scope(self.factory) as s:
                s.execute(delete(Event).where(Event.ts < cutoff))
                s.execute(delete(RequestLog).where(RequestLog.ts < cutoff))
        except Exception as e:  # noqa: BLE001 - analytics must never break the app
            log.warning("analytics: purge failed: %s", type(e).__name__)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_every_s)
            await asyncio.to_thread(self.flush)
            if time.monotonic() - self._last_purge > 6 * 3600:
                self._last_purge = time.monotonic()
                await asyncio.to_thread(self.purge)

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await asyncio.to_thread(self.flush)


def _should_log(path: str) -> bool:
    if path.startswith(_SKIP_PREFIXES) or path.endswith(_SKIP_SUFFIXES):
        return False
    return path.startswith("/api/") or not _STATIC_EXT.search(path)


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        try:
            ctx = getattr(request.app.state, "ctx", None)
            sink = getattr(ctx, "analytics", None)
            path = request.url.path
            if sink is not None and sink.enabled and request.method in ("GET", "POST") and _should_log(path) \
                    and not opted_out(request):
                params = request.scope.get("path_params") or {}
                aid = params.get("analysis_id")
                if path.startswith("/api/"):
                    route = request.scope.get("route")
                    template = getattr(route, "path", None) or "/api/(unmatched)"
                else:  # SPA pages: name the page, not the catch-all route
                    m = re.match(r"^/analyses/([A-Za-z0-9-]{1,36})/?$", path)
                    aid = m.group(1) if m else None
                    template = "page:/" if path == "/" else ("page:/analyses/{id}" if aid else f"page:{path[:60]}")
                visitor = visitor_id(request, ctx.salt)
                sink.add(RequestLog(visitor=visitor, method=request.method,
                                    route=str(template)[:128], path=path[:255], status=response.status_code,
                                    ms=round((time.perf_counter() - t0) * 1000, 1),
                                    analysis_id=aid if isinstance(aid, str) and ID_RE.match(aid) else None,
                                    referrer_host=referrer_host(request),
                                    client=sink.classify(visitor, request.headers.get("user-agent", ""))))
        except Exception as e:  # noqa: BLE001 - analytics must never break the app
            log.debug("analytics: request not logged: %s", type(e).__name__)
        return response


@router.post("/events", status_code=204, include_in_schema=False)
async def post_events(request: Request, ctx: Ctx) -> Response:
    """Always 204: a bad or over-budget batch is dropped silently, so the tracker never sees an error."""
    sink = ctx.analytics
    if sink is None or not sink.enabled or opted_out(request):
        return Response(status_code=204)
    try:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return Response(status_code=204)
        parsed = clean_events(json.loads(body))
        if parsed is None:
            return Response(status_code=204)
        session, events = parsed
        n = sink.budget.take(session, len(events))
        visitor = visitor_id(request, ctx.salt)
        now = datetime.now(UTC)
        for i, e in enumerate(events[:n]):
            # dated by when it happened (client offset, capped at 10 min), in batch order
            ts = now - timedelta(milliseconds=e.pop("ago_ms")) + timedelta(microseconds=i)
            sink.add(Event(ts=ts, session_id=session, visitor=visitor, **e))
    except Exception as e:  # noqa: BLE001 - analytics must never break the app
        log.debug("analytics: events dropped: %s", type(e).__name__)
    return Response(status_code=204)


# ---------------------------------------------------------------- export for the local dashboard

EXPORT_LIMIT = 5000


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _since(value: str | None) -> datetime:
    if not value:
        return datetime(1970, 1, 1, tzinfo=UTC)
    try:
        d = datetime.fromisoformat(value.replace(" ", "+"))  # an unencoded "+00:00" arrives as " 00:00"
    except ValueError as e:
        raise HTTPException(422, "since must be an ISO timestamp") from e
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _check_token(ctx, authorization: str | None) -> None:
    token = ctx.serving.analytics_token.get_secret_value() if ctx.serving.analytics_token else ""
    if not token:
        raise HTTPException(404, "Not Found")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode(), token.encode()):
        raise HTTPException(401, "bad token")


@router.get("/admin/analytics", include_in_schema=False)
def export_analytics(ctx: Ctx, table: str = Query(..., pattern="^(events|requests|analyses|disputes)$"),
                     since: str | None = None, limit: int = Query(EXPORT_LIMIT, ge=1, le=EXPORT_LIMIT),
                     authorization: str | None = Header(None)) -> dict[str, Any]:
    """Rows with a timestamp after `since`, oldest first, at most `limit`. `analyses` covers live analyses
    (and their latest status), keyed by `updated` = finished_at or created_at."""
    _check_token(ctx, authorization)
    if ctx.analytics is not None:
        ctx.analytics.flush()
    t = _since(since)
    with session_scope(ctx.factory) as s:
        if table == "events":
            q = select(Event).where(Event.ts > t).order_by(Event.ts).limit(limit)
            rows = [{"id": e.id, "ts": _iso(e.ts), "session_id": e.session_id, "visitor": e.visitor,
                     "type": e.type, "path": e.path, "analysis_id": e.analysis_id, "props": e.props}
                    for e in s.scalars(q)]
        elif table == "requests":
            q = select(RequestLog).where(RequestLog.ts > t).order_by(RequestLog.ts).limit(limit)
            rows = [{"id": r.id, "ts": _iso(r.ts), "visitor": r.visitor, "method": r.method, "route": r.route,
                     "path": r.path, "status": r.status, "ms": r.ms, "analysis_id": r.analysis_id,
                     "referrer_host": r.referrer_host, "client": r.client} for r in s.scalars(q)]
        elif table == "disputes":
            q = select(Dispute).where(Dispute.created_at > t).order_by(Dispute.created_at).limit(limit)
            rows = [{"id": d.id, "ts": _iso(d.created_at), "analysis_id": d.analysis_id, "claim_id": d.claim_id,
                     "has_contact": bool(d.contact)} for d in s.scalars(q)]
        else:
            # live analyses, re-sent when they finish (the dashboard upserts by id)
            from sqlalchemy import func

            upd = func.coalesce(Analysis.finished_at, Analysis.created_at)
            q = (select(Analysis, Paper, upd.label("upd")).join(Paper, Paper.id == Analysis.paper_id)
                 .where(Analysis.mode == "live", upd > t).order_by(upd).limit(limit))
            visitors = {r.analysis_id: r.ip_hash[:16] for r in s.scalars(
                select(LiveRequest).where(LiveRequest.analysis_id.is_not(None)))}
            rows = []
            for a, p, u in s.execute(q):
                summ = a.summary or {}
                code = ((a.progress or {}).get("code") or {})
                rows.append({"id": a.id, "ts": _iso(a.created_at), "updated": _iso(u),
                             "finished_at": _iso(a.finished_at), "status": a.status, "error": (a.error or "")[:300],
                             "cost_eur": a.cost_eur, "source": p.source, "external_id": p.external_id,
                             "title": p.title or (((a.result or {}).get("paper") or {}).get("title")),
                             "counts": summ.get("counts"), "n_claims": summ.get("n_claims"),
                             "code_status": code.get("status"), "visitor": visitors.get(a.id),
                             "warnings": ((a.result or {}).get("warnings") or [])[:5] if a.result else []})
    last = rows[-1]["updated" if table == "analyses" else "ts"] if rows else since
    return {"table": table, "rows": rows, "next_since": last, "more": len(rows) == limit}
