from __future__ import annotations

import json

from typer.testing import CliRunner

from onus.cli import app

runner = CliRunner()


def test_version_prints_fingerprint():
    from onus import version as v

    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0, r.output
    assert v.ONUS_VERSION in r.output and v.fingerprint() in r.output


def test_schema_export_to_path(tmp_path):
    out = tmp_path / "s.json"
    r = runner.invoke(app, ["schema", "export", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert json.loads(out.read_text())["title"] == "RunResult"


def test_db_init(tmp_path):
    r = runner.invoke(app, ["db", "init", "--url", f"sqlite:///{tmp_path / 'x.db'}"])
    assert r.exit_code == 0 and (tmp_path / "x.db").is_file()


def test_codecheck_requires_its_options():
    # Phase 10 is built; `freeze --code-version` is tested in test_codecheck.py against a temporary frozen.json
    # (invoking it here would rewrite the real one).
    r = runner.invoke(app, ["codecheck"])
    assert r.exit_code == 2 and "--data-root" in r.output, r.output
    r = runner.invoke(app, ["codecheck", "version"])
    assert r.exit_code == 0 and "code-v1" in r.output, r.output


def test_every_subcommand_group_exists():
    r = runner.invoke(app, ["--help"])
    for name in ("llm", "ingest", "claims", "analyze", "report", "eval", "labels", "findings", "bundle",
                 "codecheck", "freeze", "schema", "version", "db"):
        assert name in r.output
