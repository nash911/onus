"""scripts/migrate_sqlite_to_postgres.py, exercised SQLite -> SQLite (same code path; Postgres DDL is tested
in test_db.py)."""

from __future__ import annotations

import importlib.util
from datetime import date

import pytest

from onus.config import repo_root
from onus.db import repo
from onus.db.session import init_db, make_engine, make_sessionmaker, session_scope


@pytest.fixture(scope="module")
def mig():
    spec = importlib.util.spec_from_file_location(
        "migrate_sqlite_to_postgres", repo_root() / "scripts" / "migrate_sqlite_to_postgres.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed(url: str) -> dict:
    engine = make_engine(url)
    init_db(engine)
    f = make_sessionmaker(engine)
    with session_scope(f) as s:
        p = repo.upsert_paper(s, source="agents4science", external_id="abc", title="T", display={"tier": "fails"})
        a = repo.create_analysis(s, paper_id=p.id, onus_version="0.1.0", fingerprint="fp", mode="batch", runs=3)
        repo.store_result(s, a, {"summary": {"counts": {"supported": 1}}, "cost": {"eur": 0.1}, "claims": []})
        repo.add_dispute(s, analysis_id=a.id, claim_id="c1", reason="r")
        repo.add_live_usage(s, date(2026, 11, 15), analyses=2, cost_eur=0.3)
        repo.record_live_request(s, "h", a.id)
        ids = {"paper": p.id, "analysis": a.id}
    engine.dispose()
    return ids


def test_copy_all_and_rerun(tmp_path, mig):
    src_url, dst_url = f"sqlite:///{tmp_path}/src.db", f"sqlite:///{tmp_path}/dst.db"
    ids = seed(src_url)
    src, dst = make_engine(src_url), make_engine(dst_url)
    with pytest.raises(SystemExit, match="missing tables"):
        mig.copy_all(src, dst, log=lambda *_: None)
    rep = mig.copy_all(src, dst, create=True, log=lambda *_: None)
    assert {k: v["copied"] for k, v in rep.items()} == {"papers": 1, "analyses": 1, "disputes": 1,
                                                        "live_usage": 1, "live_requests": 1}
    again = mig.copy_all(src, dst, log=lambda *_: None)
    assert all(v["copied"] == 0 and v["skipped"] == v["source"] for v in again.values())
    f = make_sessionmaker(dst)
    with session_scope(f) as s:
        a = repo.get_analysis(s, ids["analysis"])
        assert a.summary["counts"] == {"supported": 1} and a.created_at.tzinfo is not None
        assert a.paper.display == {"tier": "fails"} and len(a.disputes) == 1
        assert repo.get_live_usage(s, date(2026, 11, 15)).analyses == 2
    src.dispose()
    dst.dispose()


def test_cli_dry_run(tmp_path, mig, capsys):
    src_url = f"sqlite:///{tmp_path}/src.db"
    seed(src_url)
    assert mig.main(["--from", src_url, "--to", f"sqlite:///{tmp_path}/dst.db", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "papers" in out and "source=     1" in out


def test_password_hidden(mig):
    assert "s3cret" not in mig.safe("postgresql+psycopg://u:s3cret@h:6543/postgres")
