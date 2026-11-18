"""Eval-run count for the Trust page, read from a fixture copy of docs/public/eval_log.md's format."""

from __future__ import annotations

from pathlib import Path

from onus.api.eval_log import parse_eval_log, read_eval_runs

FIXTURE = """# Held-out run log

Intro text | with a pipe that is not a table.

| # | Split | Started (UTC) | Git commit | Pipeline fingerprint | Harness fingerprint | Changes since last run | Result (aggregate) | € |
| --- | --- | --- | --- | --- | --- | --- | --- | ---: |
| eval-1 | eval | 2026-11-15 17:07 | `172ed05` | `a1c46b3e07ad` | `a010dfd35251` | fixes | Recall 23/30 | 14.41 |
| eval-2 | eval | 2026-11-15 22:40 | `abc1234` | `b2c3d4e5f6a7` | `a010dfd35251` | more fixes | Recall 27/30 | 13.90 |
| test_1 | test_1 | 2026-11-16 01:00 | `def5678` | `b2c3d4e5f6a7` | `a010dfd35251` | frozen | — | 20.00 |
| eval-final | eval-final | 2026-11-16 09:00 | `def5678` | `b2c3d4e5f6a7` | `a010dfd35251` | R4 | — | 9.00 |

Text after the table.
"""


def test_counts_only_eval_rows():
    runs = parse_eval_log(FIXTURE)
    assert [r["run"] for r in runs] == ["eval-1", "eval-2"]
    assert runs[0] == {"run": "eval-1", "started": "2026-11-15 17:07", "commit": "172ed05",
                       "pipeline_fingerprint": "a1c46b3e07ad", "harness_fingerprint": "a010dfd35251"}
    assert "Recall" not in str(runs)  # free-text columns are not returned


def test_header_only_and_missing(tmp_path: Path):
    head = FIXTURE.split("| eval-1")[0]
    assert parse_eval_log(head) == []
    assert parse_eval_log("no table here") == []
    assert read_eval_runs(tmp_path / "missing.md") == []


def test_meta_returns_eval_runs(tmp_path: Path):
    from fastapi.testclient import TestClient

    from onus.api.main import create_app
    from onus.config import Settings

    log = tmp_path / "eval_log.md"
    log.write_text(FIXTURE)
    s = Settings(_env_file=None, onus_var_dir=tmp_path / "var", database_url=f"sqlite:///{tmp_path}/t.db",
                 onus_results_import_dir=tmp_path / "none")

    async def never(*_a):  # no live jobs in this test
        raise AssertionError

    with TestClient(create_app(s, analyzer=never, fingerprint="fp", frontend_dist=tmp_path / "dist",
                               eval_log=log)) as c:
        er = c.get("/api/meta").json()["eval_runs"]
        assert er["count"] == 2 and er["max"] == 3 and er["runs"][1]["commit"] == "abc1234"
    with TestClient(create_app(s, analyzer=never, fingerprint="fp", frontend_dist=tmp_path / "dist",
                               eval_log=tmp_path / "nope.md")) as c:
        assert c.get("/api/meta").json()["eval_runs"] == {"count": 0, "max": 3, "runs": []}


def test_seed_capabilities(tmp_path: Path):
    from onus.api.main import seed_capabilities

    seed = tmp_path / "repo" / "berget_capabilities.json"
    seed.parent.mkdir()
    seed.write_text('{"roles": {}}')
    var = tmp_path / "data" / "var"
    assert seed_capabilities(var, seed) is True
    assert (var / "berget_capabilities.json").read_text() == '{"roles": {}}'
    assert seed_capabilities(var, seed) is False  # unchanged
    seed.write_text('{"roles": {"x": 1}}')
    assert seed_capabilities(var, seed) is True  # the shipped file wins
    assert seed_capabilities(var, tmp_path / "missing.json") is False
    assert seed_capabilities(seed.parent, seed) is False  # var dir is the repo's own var/


def test_seed_capabilities_per_provider(tmp_path: Path):
    from onus.api.main import seed_capabilities

    seed = tmp_path / "repo" / "gemini_capabilities.json"
    seed.parent.mkdir()
    seed.write_text('{"roles": {}}')
    var = tmp_path / "data" / "var"
    assert seed_capabilities(var, seed, provider="gemini") is True
    assert (var / "gemini_capabilities.json").is_file()
    assert not (var / "berget_capabilities.json").exists()  # the Berget file is not touched
