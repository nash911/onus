from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from onus.db import repo
from onus.db.models import postgres_ddl
from onus.db.session import init_db, make_engine, make_sessionmaker, session_scope


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'sub' / 'onus.db'}")
    init_db(engine)
    return make_sessionmaker(engine)


def test_sqlite_create_tables_and_wal(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'a' / 'onus.db'}")
    init_db(engine)
    assert set(inspect(engine).get_table_names()) == {"papers", "analyses", "disputes", "live_usage", "live_requests",
                                                       "events", "request_log"}
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_repo_round_trip(factory):
    with session_scope(factory) as s:
        p = repo.upsert_paper(s, source="agents4science", external_id="abc", title="T", pdf_sha256="f" * 64, pages=9)
        assert len(p.id) == 36
        assert repo.upsert_paper(s, source="agents4science", external_id="abc", pages=10).id == p.id
        a = repo.create_analysis(s, paper_id=p.id, onus_version="0.1.0", fingerprint="fp", mode="batch", runs=3)
        repo.store_result(s, a, {"summary": {"counts": {}}, "cost": {"eur": 0.12}})
        d = repo.add_dispute(s, analysis_id=a.id, claim_id="c1", reason="Table 2 says otherwise")
        pid, aid, did = p.id, a.id, d.id

    with session_scope(factory) as s:
        p = repo.get_paper(s, pid)
        assert p.pages == 10 and repo.find_paper_by_sha(s, "f" * 64).id == pid
        a = repo.find_analysis(s, paper_id=pid, fingerprint="fp", mode="batch")
        assert a.id == aid and a.status == "done" and a.cost_eur == 0.12
        assert a.created_at.tzinfo is not None and a.finished_at.tzinfo is not None
        assert [x.id for x in repo.list_disputes(s, aid)] == [did]
        assert repo.count_papers(s) == 1


def test_unique_analysis_per_paper_fingerprint_mode(factory):
    with pytest.raises(IntegrityError), session_scope(factory) as s:
        p = repo.upsert_paper(s, source="arxiv", external_id="2501.00001")
        repo.create_analysis(s, paper_id=p.id, onus_version="0.1.0", fingerprint="fp", mode="live")
        repo.create_analysis(s, paper_id=p.id, onus_version="0.1.0", fingerprint="fp", mode="live")


def test_enum_fields_checked_in_code(factory):
    with session_scope(factory) as s:
        with pytest.raises(ValueError):
            repo.upsert_paper(s, source="nowhere", external_id="x")
        p = repo.upsert_paper(s, source="upload", external_id="abc")
        with pytest.raises(ValueError):
            repo.create_analysis(s, paper_id=p.id, onus_version="0", fingerprint="f", mode="weird")


def test_stale_jobs_and_live_usage(factory):
    with session_scope(factory) as s:
        p = repo.upsert_paper(s, source="upload", external_id="x")
        a = repo.create_analysis(s, paper_id=p.id, onus_version="0", fingerprint="f", mode="live", status="running")
        assert repo.fail_stale_running(s) == 1 and a.status == "failed"
        day = date(2026, 11, 15)
        repo.add_live_usage(s, day, analyses=1, cost_eur=0.2)
        repo.add_live_usage(s, day, analyses=1, cost_eur=0.3)
        assert repo.get_live_usage(s, day).analyses == 2
        repo.record_live_request(s, "iphash", a.id)
        repo.record_live_request(s, "iphash")
        assert repo.count_live_requests_last_hour(s, "iphash") == 2
        assert repo.count_live_requests_since(s, "iphash", datetime(2100, 1, 1, tzinfo=UTC)) == 0


def test_naive_datetimes_rejected(factory):
    with pytest.raises(Exception, match="naive"), session_scope(factory) as s:
        repo.count_live_requests_since(s, "x", datetime(2026, 1, 1))


def test_postgres_ddl_compiles():
    ddl = postgres_ddl()
    for table in ("papers", "analyses", "disputes", "live_usage", "live_requests"):
        assert f"CREATE TABLE {table}" in ddl
    assert "JSONB" in ddl and "TIMESTAMP WITH TIME ZONE" in ddl
    assert "uq_analyses_paper_fp_mode" in ddl and "REFERENCES papers (id)" in ddl
    assert "AUTOINCREMENT" not in ddl.upper() and "SERIAL" not in ddl.upper()


def test_committed_pg_schema_is_current():
    from onus.config import repo_root

    text = (repo_root() / "db" / "schema.sql").read_text()
    assert postgres_ddl() in text, "db/schema.sql is stale: run `onus db export-pg`"
