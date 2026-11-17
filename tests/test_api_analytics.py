"""Product analytics: request log, POST /api/events, the admin export, privacy rules (no Berget, no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from onus.api.analytics import AnalyticsSink, SessionBudget, clean_events, client_kind
from onus.api.settings import ServingSettings
from onus.db.models import Event, RequestLog
from onus.db.session import session_scope
from tests.test_api import client_for, env  # noqa: F401  (pytest fixture)

SID = "3f0c2a9e-1111-4c2b-9d7a-0123456789ab"


def rows(c, model):
    ctx = c.app.state.ctx
    ctx.analytics.flush()
    with session_scope(ctx.factory) as s:
        return list(s.scalars(select(model).order_by(model.ts)))


def post(c, events, session=SID, headers=None):
    return c.post("/api/events", json={"session": session, "events": events}, headers=headers or {})


def test_events_are_stored_with_whitelisted_props_only(env):  # noqa: F811
    with client_for(env) as c:
        r = post(c, [
            {"type": "claim_open", "analysis_id": "a1b2", "path": "/analyses/a1b2",
             "props": {"claim_id": "c1", "verdict": "untraced", "is_main": True, "secret": "x", "via": 5}},
            {"type": "search", "props": {"length": 7, "query": "free text is never kept"}},
            {"type": "not_a_type", "props": {}},
            {"type": "heartbeat", "props": {"seconds": 15.7}},
        ])
        assert r.status_code == 204
        ev = rows(c, Event)
        assert [e.type for e in ev] == ["claim_open", "search", "heartbeat"]
        assert ev[0].props == {"claim_id": "c1", "verdict": "untraced", "is_main": True}
        assert ev[0].analysis_id == "a1b2" and ev[0].session_id == SID and len(ev[0].visitor) == 16
        assert ev[1].props == {"length": 7}
        assert ev[2].props == {"seconds": 15}


def test_bad_bodies_are_dropped_silently(env):  # noqa: F811
    with client_for(env) as c:
        assert c.post("/api/events", content=b"not json").status_code == 204
        assert c.post("/api/events", json={"session": "bad id!", "events": []}).status_code == 204
        assert c.post("/api/events", json={"session": SID, "events": "x"}).status_code == 204
        big = [{"type": "heartbeat", "props": {"seconds": 1, "pad": "x" * 2000}}] * 40
        assert post(c, big).status_code == 204  # over 32 KB
        assert post(c, [{"type": "heartbeat", "analysis_id": "../../etc", "props": {"seconds": 1}}]).status_code == 204
        ev = rows(c, Event)
        assert len(ev) == 1 and ev[0].analysis_id is None


def test_do_not_track_and_gpc_are_respected(env):  # noqa: F811
    with client_for(env) as c:
        post(c, [{"type": "page_view", "props": {"route": "/"}}], headers={"DNT": "1"})
        post(c, [{"type": "page_view", "props": {"route": "/"}}], headers={"Sec-GPC": "1"})
        c.get("/api/meta", headers={"DNT": "1"})
        assert rows(c, Event) == []
        assert [r for r in rows(c, RequestLog) if r.route == "/api/meta"] == []


def test_request_log_records_api_and_pages_but_not_polling(env, tmp_path):  # noqa: F811
    dist = env["tmp"] / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>")
    with client_for(env) as c:
        aid = c.get("/api/papers").json()["items"][0]["analysis_id"]
        c.get(f"/api/analyses/{aid}", headers={"Referer": "https://news.example.org/post",
                                               "User-Agent": "Mozilla/5.0 (iPhone)"})
        c.get(f"/api/analyses/{aid}/status")
        c.get("/api/health")
        c.get(f"/analyses/{aid}")
        post(c, [{"type": "page_view"}])
        logged = rows(c, RequestLog)
        routes = [r.route for r in logged]
        assert "/api/papers" in routes and "/api/analyses/{analysis_id}" in routes
        assert "page:/analyses/{id}" in routes
        assert not any(r.endswith("/status") or r == "/api/health" or "events" in r for r in routes)
        detail = next(r for r in logged if r.route == "/api/analyses/{analysis_id}")
        assert detail.analysis_id == aid and detail.status == 200 and detail.client == "mobile"
        assert detail.referrer_host == "news.example.org"
        page = next(r for r in logged if r.route.startswith("page:"))
        assert page.analysis_id == aid


def test_analytics_can_be_turned_off(env):  # noqa: F811
    with client_for(env, serving=ServingSettings(onus_analytics=False)) as c:
        post(c, [{"type": "page_view"}])
        c.get("/api/meta")
        assert rows(c, Event) == [] and rows(c, RequestLog) == []


def test_admin_export_needs_a_token(env):  # noqa: F811
    with client_for(env) as c:  # no token configured: the endpoint does not exist
        assert c.get("/api/admin/analytics?table=events").status_code == 404
    with client_for(env, serving=ServingSettings(analytics_token="tok-123")) as c:
        assert c.get("/api/admin/analytics?table=events").status_code == 401
        assert c.get("/api/admin/analytics?table=events",
                     headers={"Authorization": "Bearer nope"}).status_code == 401
        post(c, [{"type": "page_view", "props": {"route": "/"}}, {"type": "export", "props": {"format": "md"}}])
        auth = {"Authorization": "Bearer tok-123"}
        out = c.get("/api/admin/analytics?table=events", headers=auth).json()
        assert [r["type"] for r in out["rows"]] == ["page_view", "export"] and out["more"] is False
        again = c.get(f"/api/admin/analytics?table=events&since={out['next_since']}", headers=auth).json()
        assert again["rows"] == []
        req = c.get("/api/admin/analytics?table=requests", headers=auth).json()["rows"]
        assert all("/admin/" not in r["path"] for r in req)
        assert c.get("/api/admin/analytics?table=analyses", headers=auth).status_code == 200
        assert c.get("/api/admin/analytics?table=papers", headers=auth).status_code == 422


def test_live_analysis_shows_up_in_export(env):  # noqa: F811
    from tests.test_api import PDF, wait_done

    with client_for(env, serving=ServingSettings(analytics_token="tok-123")) as c:
        r = c.post("/api/analyses", files={"file": ("p.pdf", PDF + b"x", "application/pdf")})
        aid = r.json()["analysis_id"]
        wait_done(c, aid)
        out = c.get("/api/admin/analytics?table=analyses", headers={"Authorization": "Bearer tok-123"}).json()
        row = next(x for x in out["rows"] if x["id"] == aid)
        assert row["status"] == "done" and row["source"] == "upload" and len(row["visitor"]) == 16
        assert row["counts"]["contradicted"] == 1


def test_session_budget():
    b = SessionBudget(budget=5, window_s=10)
    assert b.take("s", 3, now=0) == 3
    assert b.take("s", 3, now=1) == 2
    assert b.take("s", 1, now=2) == 0
    assert b.take("s", 4, now=12) == 4  # window slid


def test_purge_and_failed_writes_never_raise(tmp_path):
    from onus.db.session import init_db, make_engine, make_sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path}/a.db")
    init_db(engine)
    sink = AnalyticsSink(make_sessionmaker(engine), retention_days=30)
    old = datetime.now(UTC) - timedelta(days=40)
    sink.add(Event(ts=old, session_id=SID, visitor="v" * 16, type="page_view"))
    sink.add(Event(session_id=SID, visitor="v" * 16, type="page_view"))
    assert sink.flush() == 2
    sink.purge()
    with session_scope(sink.factory) as s:
        assert len(list(s.scalars(select(Event)))) == 1
    engine.dispose()
    broken = AnalyticsSink(make_sessionmaker(make_engine(f"sqlite:///{tmp_path}/missing/dir/x.db")))
    broken.add(Event(session_id=SID, visitor="v" * 16, type="page_view"))
    assert broken.flush() == 0  # no table: logged, not raised


def test_helpers():
    assert client_kind("Mozilla/5.0 (X11; Linux x86_64) Chrome/140") == "desktop"
    assert client_kind("Googlebot/2.1") == "bot" and client_kind("") == "unknown"
    assert clean_events({"session": SID, "events": [{"type": "tab_switch", "props": {"tab": "code"}}]}) == \
        (SID, [{"type": "tab_switch", "ago_ms": 0, "path": None, "analysis_id": None, "props": {"tab": "code"}}])
    _, ev = clean_events({"session": SID, "events": [{"type": "heartbeat", "ago": 9e9}, {"type": "heartbeat", "ago": -5}]})
    assert [e["ago_ms"] for e in ev] == [600_000, 0]


def test_events_are_dated_by_their_offset_in_batch_order(env):  # noqa: F811
    with client_for(env) as c:
        post(c, [{"type": "tab_switch", "ago": 4000, "props": {"tab": "claims"}},
                 {"type": "tab_switch", "ago": 2000, "props": {"tab": "code"}},
                 {"type": "tab_switch", "ago": 2000, "props": {"tab": "checklist"}}])
        ev = rows(c, Event)
        assert [e.props["tab"] for e in ev] == ["claims", "code", "checklist"]
        assert 1.9 < (ev[1].ts - ev[0].ts).total_seconds() < 2.1
