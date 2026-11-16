"""Phase 11 follow-up: older-version labels, code-check outcomes in reports, live-off and limit messages,
the freeze and public docs on /api/meta. Reuses the Phase 8 fixtures (temp DB, fake pipeline, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from onus.api.main import create_app
from tests.test_api import FP, PDF, FakePipeline, client_for, make_result, write_bundle
from tests.test_api import env as _env  # noqa: F401  (Phase 8 fixture, re-exposed below)


@pytest.fixture
def env(_env):  # noqa: F811
    return _env


def client_with_docs(env, eval_log: Path, frozen: Path) -> TestClient:
    return TestClient(create_app(env["settings"], analyzer=FakePipeline(), fingerprint=FP,
                                 frontend_dist=env["tmp"] / "dist", eval_log=eval_log, frozen_manifest=frozen))


def _older_bundle(env) -> None:
    """One corpus paper analysed by an older pipeline, next to the current-version bundle."""
    write_bundle(env["import_dir"] / "bundle_old.jsonl.gz", [
        make_result("PaperOLD0004", fp="b19c7c9dcdcd", title="Old study", created_at="2026-11-10T12:00:00+00:00"),
    ])


# ---------------------------------------------------------------- item 2: results from another version

def test_older_version_rows_are_listed_with_their_fingerprint(env):
    _older_bundle(env)
    with client_for(env) as c:
        meta = c.get("/api/meta").json()
        assert meta["fingerprint"] == FP
        counts = {(v["fingerprint"], v["mode"]): v["analyses"] for v in meta["versions"]}
        assert counts == {("b19c7c9dcdcd", "batch"): 1, (FP, "batch"): 3}

        items = c.get("/api/papers?limit=50").json()["items"]
        by_title = {p["title"]: p for p in items}
        assert len(items) == 4  # never hidden
        assert by_title["Old study"]["fingerprint"] == "b19c7c9dcdcd"
        assert {p["fingerprint"] for t, p in by_title.items() if t != "Old study"} == {FP}

        report = c.get(f"/api/analyses/{by_title['Old study']['analysis_id']}").json()
        assert report["fingerprint"] == "b19c7c9dcdcd"


def test_featured_rows_carry_fingerprint(env):
    _older_bundle(env)
    env["settings"].featured_papers = "PaperOLD0004,PaperAAAA01"
    with client_for(env) as c:
        meta = c.get("/api/meta").json()
        assert meta["featured_source"] == "config"
        fps = {p["title"]: p["fingerprint"] for p in meta["featured"]}
        assert fps == {"Old study": "b19c7c9dcdcd", "Alpha study": FP}
        assert all(p["onus_version"] == "0.1.0" for p in meta["featured"])
    env["settings"].featured_papers = ""
    with client_for(env) as c:
        assert all(p["fingerprint"] for p in c.get("/api/meta").json()["featured"])


# ---------------------------------------------------------------- item 3: code-check outcomes pass through

def test_rerun_outcomes_reach_the_report_unchanged(env):
    r = make_result("PaperCODE05", title="Code study")
    r["code"]["reruns"] = [
        {"status": "fails_as_shipped", "run_status": "fails_as_shipped", "reason": "python main.py -> KeyError: 'x'",
         "as_shipped": {"outcome": "fails_as_shipped", "cmd": "python main.py", "rc": 1, "error_line": "KeyError: 'x'"},
         "commands": ["$ python main.py   # exit 1, 2.0 s"], "modifications": []},
        {"status": "consistent_modified", "run_status": "reproduced", "modified": True,
         "modifications": [{"path": "a.py", "change": "fix path", "diff": "-/abs/data\n+./data"}], "commands": []},
        {"status": "missing_data", "reason": "FileNotFoundError: /home/u/data.csv"},
        {"status": "install_failed", "reason": "install failed: ERROR: torch==9"},
        {"status": "failed", "reason": "max_steps"},
        {"status": "some_future_status"},
    ]
    write_bundle(env["import_dir"] / "bundle_code.jsonl.gz", [r])
    with client_for(env) as c:
        p = next(x for x in c.get("/api/papers?limit=50").json()["items"] if x["title"] == "Code study")
        reruns = c.get(f"/api/analyses/{p['analysis_id']}").json()["code"]["reruns"]
        assert [x["status"] for x in reruns] == ["fails_as_shipped", "consistent_modified", "missing_data",
                                                 "install_failed", "failed", "some_future_status"]
        assert reruns[0]["as_shipped"]["error_line"] == "KeyError: 'x'"
        assert reruns[1]["modifications"][0]["diff"] == "-/abs/data\n+./data"


# ---------------------------------------------------------------- item 5: live analysis off / limits

def test_live_off_says_so_in_plain_words(env):
    from onus.api.routes_analyses import LIVE_OFF

    env["settings"].berget_api_key = type(env["settings"].berget_api_key)("")
    with client_for(env) as c:
        assert c.get("/api/meta").json()["live"]["enabled"] is False
        for req in ({"files": {"file": ("p.pdf", PDF + b"new", "application/pdf")}},
                    {"json": {"source": "arxiv", "id": "2401.01234"}}):
            r = c.post("/api/analyses", **req)
            assert r.status_code == 503
            assert r.json()["detail"] == LIVE_OFF
            assert "switched off" in LIVE_OFF and "HTTP" not in LIVE_OFF


def test_limit_messages_are_plain_text(env):
    """Each 429 body has a readable `detail` (the UI shows it as-is)."""
    from onus.api.limits import today
    from onus.db import repo
    from onus.db.session import session_scope

    with client_for(env) as c:
        factory = c.app.state.ctx.factory
        for i in range(3):
            assert c.post("/api/analyses", json={"source": "arxiv", "id": f"2401.0000{i}"}).status_code == 202
        hourly = c.post("/api/analyses", json={"source": "arxiv", "id": "2401.00009"})
        assert hourly.status_code == 429 and hourly.json()["detail"].startswith("You can start at most 3 live")

        with session_scope(factory) as s:
            repo.add_live_usage(s, today(), analyses=100)
        daily = c.post("/api/analyses", json={"source": "arxiv", "id": "2401.00010"},
                       headers={"x-forwarded-for": "203.0.113.9"})
        assert daily.status_code == 429 and "daily limit of 10" in daily.json()["detail"]

        with session_scope(factory) as s:
            repo.add_live_usage(s, today(), cost_eur=1000.0)
        cap = c.post("/api/analyses", json={"source": "arxiv", "id": "2401.00011"},
                     headers={"x-forwarded-for": "203.0.113.10"})
        assert cap.status_code == 429 and "budget for live analyses is used up" in cap.json()["detail"]


# ---------------------------------------------------------------- item 6: freeze and public docs

def test_meta_freeze_and_public_docs(env, tmp_path: Path):
    docs = tmp_path / "public"
    docs.mkdir()
    (docs / "eval_log.md").write_text("| # | Split |\n| --- | --- |\n| eval-1 | eval |\n")
    (docs / "eval_protocol.md").write_text("# Protocol\n")
    (docs / "README.md").write_text("not linked")
    frozen = tmp_path / "frozen.json"
    frozen.write_text(json.dumps({"version": "1.0.0", "fingerprint": FP, "harness_fingerprint": "b6018f89f149",
                                  "created_at": "2026-11-16T00:43:22+00:00", "source": {"x.py": "h"}}))
    with client_with_docs(env, docs / "eval_log.md", frozen) as c:
        meta = c.get("/api/meta").json()
        assert meta["freeze"] == {"version": "1.0.0", "fingerprint": FP, "harness_fingerprint": "b6018f89f149",
                                  "created_at": "2026-11-16T00:43:22+00:00", "matches_current": True}
        assert meta["public_docs"] == ["eval_protocol.md", "eval_log.md"]
        r = c.get("/api/public/eval_protocol.md")
        assert r.status_code == 200 and r.text == "# Protocol\n" and r.headers["content-type"].startswith("text/plain")
        assert c.get("/api/public/README.md").status_code == 404
        assert c.get("/api/public/..%2F..%2Ffrozen.json").status_code == 404

    frozen.write_text(json.dumps({"version": "1.0.0", "fingerprint": "otherfp00000"}))
    with client_with_docs(env, docs / "eval_log.md", frozen) as c:
        assert c.get("/api/meta").json()["freeze"]["matches_current"] is False
    with client_with_docs(env, tmp_path / "none" / "eval_log.md", tmp_path / "missing.json") as c:
        meta = c.get("/api/meta").json()
        assert meta["freeze"] is None and meta["public_docs"] == []
        assert c.get("/api/public/eval_log.md").status_code == 404
