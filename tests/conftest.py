"""Shared fixtures. Tests never read the real .env and never call Berget (live tests are marked)."""

from __future__ import annotations

from pathlib import Path

import pytest

from onus.config import Settings
from onus.llm.client import set_client


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, onus_var_dir=tmp_path / "var", berget_api_key="test-key-not-real")


@pytest.fixture
def prompts_tmp(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _reset_client():
    yield
    set_client(None)


def write_prompt(directory: Path, pid: str, body: str, role: str = "extract", temperature: float = 0.0,
                 max_tokens: int = 256, version: str = "1") -> Path:
    p = directory / f"{pid}.md"
    p.write_text(
        f"---\nid: {pid}\nversion: {version}\nrole: {role}\ntemperature: {temperature}\n"
        f"max_tokens: {max_tokens}\n---\n{body}\n"
    )
    return p
