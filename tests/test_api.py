"""API tests (Phase 8) with TestClient, a temp SQLite database, a fake pipeline and a fake fetcher.

No Berget calls and no network: the analyzer and fetcher are injected.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from onus.api.main import create_app
from onus.config import Settings
from onus.schemas.result import RunResult

FP = "fp0000000000"
PDF = b"%PDF-1.4\n% fake test pdf\n"


def make_result(pid: str, *, verdicts=("supported", "overstated"), fp: str = FP, mode: str = "batch",
                sha: str | None = None, title: str | None = None, code: bool = True,
                created_at: str = "2026-11-15T12:00:00+00:00") -> dict:
    claims = []
    for i, v in enumerate(verdicts):
        claims.append({
            "id": f"{pid[:6]}c{i}", "type": "result", "quote": f"Claim {i} of {pid}.",
            "location": {"page": 1, "paragraph": 1, "sentence": i + 1, "start": i * 10, "end": i * 10 + 9},
            "is_main": i == 0, "wording": {"strength": "plain", "scope": "specific", "markers": []},
            "verdict": v, "reason": "r", "risk": {"supported": 0, "overstated": 3, "contradicted": 4,
                                                   "untraced": 2, "not_checkable": 1}[v]})
    flagged = sum(v in ("overstated", "untraced", "contradicted") for v in verdicts)
    r = {
        "onus_version": "0.1.0", "fingerprint": fp, "models": {"extract": "m"}, "mode": mode,
        "runs": 3 if mode == "batch" else 1,
        "paper": {"id": pid, "source": "agents4science" if mode == "batch" else "upload", "external_id": None,
                  "title": title or f"Paper {pid}", "pdf_sha256": sha or hashlib.sha256(pid.encode()).hexdigest(),
                  "pages": 9},
        "created_at": created_at, "claims": claims,
        "checklist": [{"key": "error_bars", "title": "Error bars", "answer": "Yes", "justification": "j",
                       "status": "unbacked", "reason": "no spread"}],
        "code": {"available": code, "files": 3 if code else 0},
        "summary": {"counts": {v: verdicts.count(v) for v in ("supported", "overstated", "untraced",
                                                                "contradicted", "not_checkable")},
                    "flagged_share": round(flagged / len(verdicts), 4), "main_flagged_share": 0.0,
                    "n_claims": len(verdicts)},
        "cost": {"eur": 0.05},
    }
    RunResult.model_validate(r)
    return r


def write_bundle(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def read_bundle(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


class FakePipeline:
    """Async analyzer: emits the stages, optionally waits on a gate, returns a canned live RunResult."""

    def __init__(self, fail: bool = False, empty: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.gate = threading.Event()
        self.gate.set()
        self.fail = fail
        self.empty = empty  # finish "done" with no claims (e.g. a PDF whose text could not be read)

    async def __call__(self, pdf_path: Path, paper_id: str, source: str, progress) -> dict:
        import asyncio

        self.calls.append((paper_id, source))
        for stage in ("ingest", "claims", "evidence", "checks", "verdicts"):
            progress(stage, {"run": 0})
            await asyncio.sleep(0)
        while not self.gate.is_set():
            await asyncio.sleep(0.01)
        if self.fail:
            raise RuntimeError("boom /secret/path")
        sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        r = make_result(paper_id, verdicts=("contradicted", "supported"), mode="live", sha=sha,
                        title="Live paper", code=False)
        r["paper"]["source"] = source
        if self.empty:
            empty_result(r)
        return r


def empty_result(r: dict) -> dict:
    """A done result with no claims, as the pipeline returns when it finds nothing to check."""
    r["claims"] = []
    r["summary"].update(counts={v: 0 for v in r["summary"]["counts"]}, flagged_share=0.0, n_claims=0)
    r["warnings"] = ["No sections were detected; claims could not be extracted."]
    RunResult.model_validate(r)
    return r


@pytest.fixture
def env(tmp_path: Path):
    import_dir = tmp_path / "deploy"
    write_bundle(import_dir / "bundle_dev.jsonl.gz", [
        make_result("PaperAAAA01", verdicts=("supported", "overstated"), title="Alpha study"),
        make_result("PaperBBBB02", verdicts=("contradicted", "untraced", "supported"), title="Beta study",
                    code=False),
        make_result("PaperCCCC03", verdicts=("supported",), title="Gamma study", sha=hashlib.sha256(PDF).hexdigest()),
    ])
    # display objects as `onus bundle` writes them
    rows = read_bundle(import_dir / "bundle_dev.jsonl.gz")
    disp = {"PaperAAAA01": ("accepted", "executable"), "PaperBBBB02": ("rejected", "no-code"),
            "PaperCCCC03": ("rejected", "fails")}
    for r in rows:
        dec, tier = disp[r["paper"]["id"]]
        r["display"] = {"title": r["paper"]["title"], "decision": dec, "scores": {"ai_mean": 3.0}, "tier": tier}
    write_bundle(import_dir / "bundle_dev.jsonl.gz", rows)
    settings = Settings(_env_file=None, onus_var_dir=tmp_path / "var", database_url=f"sqlite:///{tmp_path}/t.db",
                        onus_results_import_dir=import_dir, berget_api_key="test-key-not-real",
                        live_max_per_ip_per_hour=3, live_max_per_day=10, live_max_pdf_mb=1,
                        featured_papers="")
    return {"tmp": tmp_path, "settings": settings, "import_dir": import_dir}


def client_for(env, pipeline=None, fetched: list | None = None, **kw) -> TestClient:
    def fetcher(source: str, ext: str, max_mb: int) -> bytes:
        if fetched is not None:
            fetched.append((source, ext))
        return PDF + ext.encode()

    app = create_app(env["settings"], analyzer=pipeline or FakePipeline(), fetcher=fetcher, fingerprint=FP,
                     frontend_dist=env["tmp"] / "dist", eval_log=env["tmp"] / "no_eval_log.md", **kw)
    return TestClient(app)


def wait_done(c: TestClient, aid: str, timeout: float = 5.0) -> dict:
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        st = c.get(f"/api/analyses/{aid}/status").json()
        if st["status"] in ("done", "failed"):
            return st
        time.sleep(0.02)
    raise AssertionError(f"analysis {aid} did not finish: {st}")


# ---------------------------------------------------------------- import, list, detail


def test_health_meta_and_import(env):
    with client_for(env) as c:
        assert c.get("/api/health").json() == {"status": "ok"}
        m = c.get("/api/meta").json()
        assert m["fingerprint"] == FP and m["corpus_papers"] == 3
        assert m["has_findings"] is False and m["has_metrics"] is False
        assert m["featured_source"] == "auto" and m["featured"][0]["ref"] == "PaperBBBB02"
        assert m["live"]["enabled"] and m["live"]["runs"] == 1 and m["live"]["code_checks"] is False
        assert m["versions"] == [{"onus_version": "0.1.0", "fingerprint": FP, "mode": "batch", "analyses": 3,
                                  "first_seen": m["versions"][0]["first_seen"],
                                  "last_seen": m["versions"][0]["last_seen"]}]
        assert c.get("/api/findings").status_code == 404
        assert "held-out" in c.get("/api/metrics").json()["detail"]


def test_import_is_idempotent_and_updates(env):
    with client_for(env) as c:
        first = c.get("/api/papers").json()
    with client_for(env) as c:  # restart: same bundle, nothing new
        again = c.get("/api/papers").json()
        assert again["total"] == 3
        assert [r["analysis_id"] for r in again["items"]] == [r["analysis_id"] for r in first["items"]]
        assert c.app.state.ctx.import_report["unchanged"] == 3
    # a re-run bundle (new created_at) updates in place
    rows = read_bundle(env["import_dir"] / "bundle_dev.jsonl.gz")
    rows[0]["created_at"] = "2026-11-16T09:00:00+00:00"
    write_bundle(env["import_dir"] / "bundle_dev.jsonl.gz", rows)
    with client_for(env) as c:
        rep = c.app.state.ctx.import_report
        assert rep["updated"] == 1 and rep["unchanged"] == 2 and rep["inserted"] == 0
        assert c.get("/api/papers").json()["total"] == 3


def test_bad_bundle_is_reported_not_fatal(env):
    (env["import_dir"] / "bundle_bad.jsonl.gz").write_bytes(gzip.compress(b'{"not": "a result"}\n'))
    with client_for(env) as c:
        assert c.get("/api/papers").json()["total"] == 3
        assert any("bundle_bad" in e for e in c.app.state.ctx.import_report["errors"])


def test_list_filters_sort_paging(env):
    with client_for(env) as c:
        r = c.get("/api/papers").json()
        assert [p["ref"] for p in r["items"]] == ["PaperBBBB02", "PaperAAAA01", "PaperCCCC03"]  # flagged desc
        assert r["facets"]["decision"] == ["accepted", "rejected"]
        assert [p["title"] for p in c.get("/api/papers?sort=title").json()["items"]] == \
            ["Alpha study", "Beta study", "Gamma study"]
        assert c.get("/api/papers?decision=accepted").json()["total"] == 1
        assert c.get("/api/papers?tier=fails").json()["items"][0]["ref"] == "PaperCCCC03"
        assert c.get("/api/papers?has_code=false").json()["items"][0]["ref"] == "PaperBBBB02"
        assert c.get("/api/papers?has_code=true").json()["total"] == 2
        assert c.get("/api/papers?verdict=contradicted").json()["total"] == 1
        assert c.get("/api/papers?q=gamma").json()["total"] == 1
        page = c.get("/api/papers?sort=title&limit=1&offset=1").json()
        assert page["total"] == 3 and [p["title"] for p in page["items"]] == ["Beta study"]
        assert c.get("/api/papers?verdict=bogus").status_code == 422


def test_paper_detail_and_analysis(env):
    with client_for(env) as c:
        p = c.get("/api/papers/PaperAAAA01").json()
        assert p["source_url"] == "https://openreview.net/forum?id=PaperAAAA01"
        assert p["display"]["decision"] == "accepted" and len(p["analyses"]) == 1
        assert c.get(f"/api/papers/{p['id']}").json()["ref"] == "PaperAAAA01"  # by uuid too
        a = c.get(f"/api/analyses/{p['analysis_id']}").json()
        RunResult.model_validate({k: v for k, v in a.items() if k not in ("display", "analysis")})
        assert a["display"]["tier"] == "executable" and a["analysis"]["paper_ref"] == "PaperAAAA01"
        assert c.get("/api/papers/nope").status_code == 404
        assert c.get("/api/analyses/nope").status_code == 404


def test_findings_and_metrics_when_present(env):
    (env["import_dir"] / "findings.json").write_text('{"rq1": {"rho": 0.1}}')
    (env["import_dir"] / "metrics.json").write_text('{"recall": {}}')
    with client_for(env) as c:
        m = c.get("/api/meta").json()
        assert m["has_findings"] and m["has_metrics"]
        assert c.get("/api/findings").json() == {"rq1": {"rho": 0.1}}


def test_featured_from_config(env):
    env["settings"].featured_papers = "PaperCCCC03, missing"
    with client_for(env) as c:
        m = c.get("/api/meta").json()
        assert m["featured_source"] == "config" and [f["ref"] for f in m["featured"]] == ["PaperCCCC03"]


# ---------------------------------------------------------------- live jobs


def test_live_upload_lifecycle(env):
    fake = FakePipeline()
    with client_for(env, fake) as c:
        pdf = PDF + b"new paper"
        r = c.post("/api/analyses", files={"file": ("p.pdf", pdf, "application/pdf")})
        assert r.status_code == 202, r.text
        aid = r.json()["analysis_id"]
        st = wait_done(c, aid)
        assert st["status"] == "done" and st["progress"]["stage"] == "done"
        assert st["progress"]["history"][:2] == ["ingest", "claims"]
        res = c.get(f"/api/analyses/{aid}").json()
        assert res["mode"] == "live" and res["paper"]["external_id"] == hashlib.sha256(pdf).hexdigest()[:12]
        assert fake.calls == [(f"upload:{hashlib.sha256(pdf).hexdigest()[:12]}", "upload")]
        assert (env["settings"].var_dir / "uploads" / f"{hashlib.sha256(pdf).hexdigest()}.pdf").is_file()
        # the same PDF again is a cache hit: no new job
        r2 = c.post("/api/analyses", files={"file": ("p.pdf", pdf, "application/pdf")})
        assert r2.status_code == 200 and r2.json() == {"analysis_id": aid, "status": "done", "cached": True}
        assert len(fake.calls) == 1
        # live uploads are not in the corpus list, but reachable
        assert c.get("/api/papers").json()["total"] == 3
        p = c.get(f"/api/papers/{res['analysis']['paper_ref']}").json()
        assert p["title"] == "Live paper"


def test_cache_hit_on_corpus_pdf(env):
    fake = FakePipeline()
    with client_for(env, fake) as c:
        r = c.post("/api/analyses", files={"file": ("p.pdf", PDF, "application/pdf")})
        assert r.status_code == 200 and r.json()["cached"] is True
        assert c.get(f"/api/analyses/{r.json()['analysis_id']}").json()["paper"]["id"] == "PaperCCCC03"
        assert fake.calls == []


def test_live_arxiv_fetch_and_cache(env):
    fetched: list = []
    fake = FakePipeline()
    with client_for(env, fake, fetched) as c:
        r = c.post("/api/analyses", json={"source": "arxiv", "id": "2401.01234"})
        assert r.status_code == 202
        aid = r.json()["analysis_id"]
        assert wait_done(c, aid)["status"] == "done"
        assert fetched == [("arxiv", "2401.01234")] and fake.calls == [("arxiv:2401.01234", "arxiv")]
        r2 = c.post("/api/analyses", json={"ref": "https://arxiv.org/abs/2401.01234"})
        assert r2.json() == {"analysis_id": aid, "status": "done", "cached": True}
        p = c.get("/api/papers/arxiv:2401.01234").json()
        assert p["source_url"] == "https://arxiv.org/abs/2401.01234"
        assert c.post("/api/analyses", json={"source": "arxiv", "id": "not an id"}).status_code == 422
        assert c.post("/api/analyses", json={"source": "zenodo", "id": "1"}).status_code == 422


def test_in_flight_dedupe_and_failure_retry(env):
    fake = FakePipeline(fail=True)
    fake.gate.clear()
    with client_for(env, fake) as c:
        pdf = PDF + b"flaky"
        aid = c.post("/api/analyses", files={"file": ("p.pdf", pdf, "application/pdf")}).json()["analysis_id"]
        again = c.post("/api/analyses", files={"file": ("p.pdf", pdf, "application/pdf")}).json()
        assert again["analysis_id"] == aid and again["status"] in ("queued", "running")
        fake.gate.set()
        st = wait_done(c, aid)
        assert st["status"] == "failed" and st["error"] == "The analysis failed (RuntimeError)."
        assert "/secret" not in st["error"]
        assert c.get(f"/api/analyses/{aid}").status_code == 409
        fake.fail = False
        retry = c.post("/api/analyses", files={"file": ("p.pdf", pdf, "application/pdf")}).json()
        assert retry["analysis_id"] == aid  # the failed row is reused
        assert wait_done(c, aid)["status"] == "done"


def test_done_with_no_claims_is_rerun_not_cached(env):
    fake = FakePipeline(empty=True)
    with client_for(env, fake) as c:
        pdf = PDF + b"unreadable"
        aid = c.post("/api/analyses", files={"file": ("p.pdf", pdf, "application/pdf")}).json()["analysis_id"]
        assert wait_done(c, aid)["status"] == "done"
        res = c.get(f"/api/analyses/{aid}").json()
        assert res["claims"] == [] and res["warnings"]  # the empty report itself is still served
        # asking again re-runs it on the same row instead of serving the empty result
        fake.empty = False
        again = c.post("/api/analyses", files={"file": ("p.pdf", pdf, "application/pdf")})
        assert again.status_code == 202, again.text
        assert again.json() == {"analysis_id": aid, "status": "queued", "cached": False}
        assert wait_done(c, aid)["status"] == "done"
        assert len(fake.calls) == 2
        assert len(c.get(f"/api/analyses/{aid}").json()["claims"]) == 2
        # a done result with claims is served from the cache: no third job
        third = c.post("/api/analyses", files={"file": ("p.pdf", pdf, "application/pdf")})
        assert third.status_code == 200 and third.json() == {"analysis_id": aid, "status": "done", "cached": True}
        assert len(fake.calls) == 2


def test_empty_corpus_result_is_not_a_cache_hit(env):
    """An imported (batch) result with no claims is not served for a live request of the same PDF."""
    pdf = PDF + b"empty corpus paper"
    rows = read_bundle(env["import_dir"] / "bundle_dev.jsonl.gz")
    rows.append(empty_result(make_result("PaperDDDD04", sha=hashlib.sha256(pdf).hexdigest(), title="Delta")))
    write_bundle(env["import_dir"] / "bundle_dev.jsonl.gz", rows)
    fake = FakePipeline()
    with client_for(env, fake) as c:
        r = c.post("/api/analyses", files={"file": ("p.pdf", pdf, "application/pdf")})
        assert r.status_code == 202 and r.json()["cached"] is False
        assert wait_done(c, r.json()["analysis_id"])["status"] == "done"
        assert len(fake.calls) == 1
        # a corpus result with claims is still a cache hit
        assert c.post("/api/analyses", files={"file": ("p.pdf", PDF, "application/pdf")}).json()["cached"] is True
        assert len(fake.calls) == 1


def test_rerun_of_empty_result_respects_limits(env):
    fake = FakePipeline(empty=True)
    with client_for(env, fake) as c:
        pdf = PDF + b"empty"
        aid = c.post("/api/analyses", files={"file": ("p.pdf", pdf, "application/pdf")}).json()["analysis_id"]
        assert wait_done(c, aid)["status"] == "done"
        for i in range(2):  # the per-IP limit is 3 per hour
            r = c.post("/api/analyses", files={"file": ("p.pdf", PDF + bytes([i]) * 4, "application/pdf")})
            assert r.status_code == 202
            wait_done(c, r.json()["analysis_id"])
        r = c.post("/api/analyses", files={"file": ("p.pdf", pdf, "application/pdf")})
        assert r.status_code == 429 and "per hour" in r.json()["detail"]
        assert len(fake.calls) == 3
        # refused: the row keeps its finished (empty) result
        assert c.get(f"/api/analyses/{aid}/status").json()["status"] == "done"
    env["settings"].live_max_per_ip_per_hour = 100
    with client_for(env, fake) as c:
        ctx = c.app.state.ctx
        from onus.api.limits import today
        from onus.db import repo
        from onus.db.session import session_scope

        with session_scope(ctx.factory) as s:
            repo.add_live_usage(s, today(), cost_eur=ctx.settings.live_max_cost_eur_per_day)
        r = c.post("/api/analyses", files={"file": ("p.pdf", pdf, "application/pdf")})
        assert r.status_code == 429 and "budget" in r.json()["detail"]


def test_rate_limit_per_ip(env):
    with client_for(env) as c:
        for i in range(3):
            r = c.post("/api/analyses", files={"file": ("p.pdf", PDF + bytes([i]) * 3, "application/pdf")})
            assert r.status_code == 202
        r = c.post("/api/analyses", files={"file": ("p.pdf", PDF + b"zzz", "application/pdf")})
        assert r.status_code == 429 and "per hour" in r.json()["detail"]
        # another client IP (behind the proxy) is not limited
        r = c.post("/api/analyses", files={"file": ("p.pdf", PDF + b"zzz", "application/pdf")},
                   headers={"x-forwarded-for": "203.0.113.9"})
        assert r.status_code == 202
        # cache hits never count against the limit
        assert c.post("/api/analyses", files={"file": ("p.pdf", PDF, "application/pdf")}).status_code == 200


def test_daily_limits(env):
    from onus.api.limits import today
    from onus.db import repo
    from onus.db.session import session_scope

    with client_for(env) as c:
        ctx = c.app.state.ctx
        with session_scope(ctx.factory) as s:
            repo.add_live_usage(s, today(), cost_eur=ctx.settings.live_max_cost_eur_per_day)
        r = c.post("/api/analyses", files={"file": ("p.pdf", PDF + b"x", "application/pdf")})
        assert r.status_code == 429 and "budget" in r.json()["detail"]
    env["settings"].live_max_cost_eur_per_day = 1e9
    with client_for(env) as c:
        ctx = c.app.state.ctx
        with session_scope(ctx.factory) as s:
            repo.add_live_usage(s, today(), analyses=ctx.settings.live_max_per_day)
        r = c.post("/api/analyses", files={"file": ("p.pdf", PDF + b"x", "application/pdf")})
        assert r.status_code == 429 and "daily limit" in r.json()["detail"]


def test_live_cost_is_recorded(env):
    from onus.api.limits import today
    from onus.db import repo
    from onus.db.session import session_scope

    with client_for(env) as c:
        aid = c.post("/api/analyses", files={"file": ("p.pdf", PDF + b"cost", "application/pdf")}).json()
        wait_done(c, aid["analysis_id"])
        with session_scope(c.app.state.ctx.factory) as s:
            u = repo.get_live_usage(s, today())
            assert u.analyses == 1 and u.cost_eur == pytest.approx(0.05)


def test_upload_validation(env):
    with client_for(env) as c:
        r = c.post("/api/analyses", files={"file": ("x.pdf", b"GIF89a not a pdf", "application/pdf")})
        assert r.status_code == 400 and "not a PDF" in r.json()["detail"]
        big = PDF + b"0" * (1024 * 1024 + 10)
        assert c.post("/api/analyses", files={"file": ("x.pdf", big, "application/pdf")}).status_code == 413
        assert c.post("/api/analyses", files={"other": ("x.pdf", PDF, "application/pdf")}).status_code == 400
        assert c.post("/api/analyses", content=b"nope", headers={"content-type": "text/plain"}).status_code == 422


def test_live_disabled_without_key(env):
    env["settings"].berget_api_key = type(env["settings"].berget_api_key)("")
    with client_for(env) as c:
        assert c.get("/api/meta").json()["live"]["enabled"] is False
        r = c.post("/api/analyses", files={"file": ("p.pdf", PDF + b"nokey", "application/pdf")})
        assert r.status_code == 503
        # cache hits still work
        assert c.post("/api/analyses", files={"file": ("p.pdf", PDF, "application/pdf")}).status_code == 200


def test_stale_jobs_marked_failed_on_startup(env):
    from onus.db import repo
    from onus.db.session import session_scope

    with client_for(env) as c, session_scope(c.app.state.ctx.factory) as s:
        p = repo.upsert_paper(s, source="upload", external_id="abc")
        aid = repo.create_analysis(s, paper_id=p.id, onus_version="0", fingerprint=FP, mode="live",
                                   status="running").id
    with client_for(env) as c:
        st = c.get(f"/api/analyses/{aid}/status").json()
        assert st["status"] == "failed" and "restarted" in st["error"]


# ---------------------------------------------------------------- disputes


def test_dispute(env):
    with client_for(env) as c:
        aid = c.get("/api/papers/PaperAAAA01").json()["analysis_id"]
        claim_id = c.get(f"/api/analyses/{aid}").json()["claims"][1]["id"]
        r = c.post(f"/api/analyses/{aid}/disputes", json={"claim_id": claim_id, "reason": "Table 2 row 3 says so",
                                                         "contact": " me@example.org "})
        assert r.status_code == 201 and r.json()["status"] == "open"
        assert c.post(f"/api/analyses/{aid}/disputes",
                      json={"claim_id": "checklist:error_bars", "reason": "See Fig 3"}).status_code == 201
        assert c.post(f"/api/analyses/{aid}/disputes", json={"claim_id": "zzz", "reason": "abc"}).status_code == 422
        assert c.post(f"/api/analyses/{aid}/disputes", json={"claim_id": claim_id, "reason": ""}).status_code == 422
        assert c.post("/api/analyses/nope/disputes", json={"claim_id": "x", "reason": "abc"}).status_code == 404
        from onus.db import repo
        from onus.db.session import session_scope

        with session_scope(c.app.state.ctx.factory) as s:
            ds = repo.list_disputes(s, aid)
            assert [d.contact for d in ds] == ["me@example.org", None]


# ---------------------------------------------------------------- static


def test_spa_fallback(env):
    dist = env["tmp"] / "dist"
    with client_for(env) as c:
        assert "not built" in c.get("/").text
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    with client_for(env) as c:
        assert "id=root" in c.get("/papers/PaperAAAA01").text
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert "immutable" in c.get("/assets/app.js").headers["cache-control"]
        assert c.get("/assets/missing.js").status_code == 404
        assert c.get("/api/unknown").status_code == 404
        assert c.get("/../pyproject.toml").status_code in (200, 404)
        assert "[project]" not in c.get("/%2e%2e/%2e%2e/pyproject.toml").text


def test_live_follows_the_configured_provider(env):
    """With ONUS_LLM_PROVIDER=gemini, live analysis needs the Gemini key, not the Berget one."""
    from onus.api.context import llm_key

    st = env["settings"]
    if "onus_llm_provider" not in type(st).model_fields:
        pytest.skip("this configuration has no provider setting")
    secret = type(st.berget_api_key)
    st.onus_llm_provider = "gemini"
    st.berget_api_key, st.gemini_api_key = secret("berget-key-not-real"), secret("")
    assert llm_key(st) == ""
    with client_for(env) as c:
        assert c.get("/api/meta").json()["live"]["enabled"] is False
    st.berget_api_key, st.gemini_api_key = secret(""), secret("gemini-key-not-real")
    assert llm_key(st) == "gemini-key-not-real"
    with client_for(env) as c:
        meta = c.get("/api/meta").json()
        assert meta["live"]["enabled"] is True
        assert meta["models"] == st.models_config  # whatever the server runs, nothing hard-coded


def test_gemini_key_is_masked(env):
    from onus.api.code_stage import secret_values

    st = env["settings"]
    if "gemini_api_key" not in type(st).model_fields:
        pytest.skip("this configuration has no Gemini key setting")
    st.gemini_api_key = type(st.berget_api_key)("gemini-key-not-real")
    assert "gemini-key-not-real" in secret_values(st)
