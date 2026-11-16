"""Serving-only settings (Phase 11). Kept out of `onus/config.py`, which is part of the pipeline fingerprint.

Read from the process environment (Railway variables), never from `.env`:

| Variable | Default | Use |
| --- | --- | --- |
| ONUS_FRONTEND_DIST | `<repo>/src/frontend/dist` | built SPA to serve |
| ONUS_EVAL_LOG | `<repo>/docs/public/eval_log.md` | eval-run count on the Trust page |
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from onus.config import repo_root


class ServingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    onus_frontend_dist: Path | None = None
    onus_eval_log: Path | None = None

    @property
    def frontend_dist(self) -> Path:
        return self.onus_frontend_dist or repo_root() / "src" / "frontend" / "dist"

    @property
    def eval_log(self) -> Path:
        return self.onus_eval_log or repo_root() / "docs" / "public" / "eval_log.md"


@lru_cache(maxsize=1)
def get_serving_settings() -> ServingSettings:
    return ServingSettings()
