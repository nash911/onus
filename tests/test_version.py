from __future__ import annotations

import json
from pathlib import Path

import pytest

from onus import version as v
from onus.config import Settings
from tests.conftest import write_prompt


@pytest.fixture
def dirs(tmp_path: Path):
    prompts, rubrics = tmp_path / "prompts", tmp_path / "rubrics"
    prompts.mkdir()
    rubrics.mkdir()
    write_prompt(prompts, "a", "hello {{x}}")
    (rubrics / "lexicon.yaml").write_text("absolute: [always]\n")
    return prompts, rubrics


def fp(dirs, settings=None, version="1.0.0"):
    return v.fingerprint(version, settings or Settings(_env_file=None), dirs[0], dirs[1])


def test_fingerprint_is_stable_and_short(dirs):
    a, b = fp(dirs), fp(dirs)
    assert a == b and len(a) == 12 and int(a, 16) >= 0


def test_fingerprint_changes_with_each_input(dirs):
    base = fp(dirs)
    assert fp(dirs, version="1.0.1") != base
    assert fp(dirs, Settings(_env_file=None, onus_model_extract="google/gemma-4-31B-it")) != base
    assert fp(dirs, Settings(_env_file=None, onus_judge_reasoning="max")) != base
    write_prompt(dirs[0], "a", "hello {{x}}!")
    assert fp(dirs) != base


def test_fingerprint_changes_with_rubrics_and_new_files(dirs):
    base = fp(dirs)
    (dirs[1] / "tolerances.yaml").write_text("rel: 0.005\n")
    assert fp(dirs) != base


def test_fingerprint_ignores_code_subdirs_and_other_files(dirs):
    base = fp(dirs)
    (dirs[0] / "code").mkdir()
    write_prompt(dirs[0] / "code", "impl_facts", "x")
    (dirs[0] / "notes.txt").write_text("not a prompt")
    (dirs[1] / "code").mkdir()
    (dirs[1] / "code" / "x.yaml").write_text("a: 1\n")
    assert fp(dirs) == base


def test_freeze_writes_record_and_bumps_version(dirs, tmp_path, monkeypatch):
    src = tmp_path / "version_src.py"
    src.write_text('X = 1\nONUS_VERSION = "0.1.0"\n')
    frozen = tmp_path / "frozen.json"
    monkeypatch.setattr(v, "__file__", str(src))
    monkeypatch.setattr(v, "frozen_path", lambda: frozen)
    monkeypatch.setattr(v, "prompts_dir", lambda: dirs[0])
    monkeypatch.setattr(v, "rubrics_dir", lambda: dirs[1])
    monkeypatch.setattr(v, "ONUS_VERSION", "0.1.0")
    s = Settings(_env_file=None)

    rec = v.freeze("1.0.0", s)

    assert 'ONUS_VERSION = "1.0.0"' in src.read_text()
    assert v.ONUS_VERSION == "1.0.0"
    on_disk = json.loads(frozen.read_text())
    assert on_disk == rec
    assert rec["fingerprint"] == v.fingerprint("1.0.0", s, dirs[0], dirs[1])
    assert set(rec) == {"version", "fingerprint", "models", "prompts", "rubrics", "created_at"}
    assert list(rec["prompts"]) == ["a.md"] and list(rec["rubrics"]) == ["lexicon.yaml"]


def test_freeze_rejects_bad_versions():
    with pytest.raises(ValueError):
        v.freeze("v1")
