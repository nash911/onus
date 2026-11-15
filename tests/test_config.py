from __future__ import annotations

import os
from pathlib import Path

import pytest

from onus.config import (
    PRICES_EUR_PER_M,
    Settings,
    UnsafePathsError,
    assert_safe_paths,
    ensure_modal_auth,
    is_within,
    repo_root,
)


def test_defaults_without_env_file():
    s = Settings(_env_file=None)
    assert s.berget_api_base == "https://api.berget.ai/v1"
    assert s.models == {
        "extract": "zai-org/GLM-5.3-Flash",
        "judge": "moonshotai/Kimi-K3.1-Pro",
        "fast": "google/gemma-4-31B-it",
        "embed": "intfloat/multilingual-e5-large-instruct",
    }
    assert s.models_config["judge_reasoning"] == "high"
    assert s.onus_dev_budget_eur == 25 and s.onus_max_eur_per_paper == 1.5
    assert s.live_max_per_day == 40 and s.onus_rerun_max_minutes == 30


def test_every_default_model_has_a_price():
    s = Settings(_env_file=None)
    for model in s.models.values():
        assert model in PRICES_EUR_PER_M


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("ONUS_MODEL_FAST", "mistralai/Mistral-Small-3.2-24B-Instruct-2506")
    monkeypatch.setenv("FEATURED_PAPERS", " a, b ,,c ")
    s = Settings(_env_file=None)
    assert s.model_for("fast") == "mistralai/Mistral-Small-3.2-24B-Instruct-2506"
    assert s.featured_paper_ids == ["a", "b", "c"]


def test_secrets_are_hidden_in_repr():
    s = Settings(_env_file=None, berget_api_key="sk-very-secret", modal_token_secret="as-very-secret")
    assert "sk-very-secret" not in repr(s) and "as-very-secret" not in repr(s)


def test_repo_root_and_relative_paths():
    root = repo_root()
    assert (root / "pyproject.toml").is_file()
    s = Settings(_env_file=None)
    assert s.data_root == root / "data" / "dev"
    assert s.var_dir == root / "var"


def test_ensure_modal_auth_copies_tokens_without_overriding(monkeypatch):
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    s = Settings(_env_file=None, modal_token_id="id-from-settings", modal_token_secret="secret-from-settings")
    assert ensure_modal_auth(s) is True
    assert os.environ["MODAL_TOKEN_ID"] == "id-from-settings"
    assert os.environ["MODAL_TOKEN_SECRET"] == "secret-from-settings"

    monkeypatch.setenv("MODAL_TOKEN_ID", "already-set")
    ensure_modal_auth(s)
    assert os.environ["MODAL_TOKEN_ID"] == "already-set"


def test_ensure_modal_auth_reports_missing(monkeypatch, capsys):
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    assert ensure_modal_auth(Settings(_env_file=None)) is False
    assert capsys.readouterr().out == ""


# ---- path guard (hard rule 4)


def test_dev_root_inside_repo_is_always_fine():
    root = repo_root()
    assert_safe_paths(root / "data" / "dev", root / "var")


def test_heldout_root_with_var_inside_repo_is_refused(tmp_path: Path):
    root = repo_root()
    with pytest.raises(UnsafePathsError):
        assert_safe_paths(tmp_path / "heldout" / "eval", root / "var")


def test_heldout_root_with_outputs_outside_repo_is_fine(tmp_path: Path):
    assert_safe_paths(tmp_path / "heldout" / "eval", tmp_path / "runs" / "var", extra_outputs=(tmp_path / "out",))


def test_heldout_root_with_out_dir_inside_repo_is_refused(tmp_path: Path):
    with pytest.raises(UnsafePathsError):
        assert_safe_paths(tmp_path / "heldout", tmp_path / "var", extra_outputs=(repo_root() / "var" / "results",))


def test_is_within_handles_prefix_names(tmp_path: Path):
    assert is_within(tmp_path / "a" / "b", tmp_path / "a")
    assert not is_within(tmp_path / "ab", tmp_path / "a")
