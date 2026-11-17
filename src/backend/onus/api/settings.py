"""Serving-only settings (Phase 11). Kept out of `onus/config.py`, which is part of the pipeline fingerprint.

Read from the process environment (Railway variables), never from `.env`:

| Variable | Default | Use |
| --- | --- | --- |
| ONUS_FRONTEND_DIST | `<repo>/src/frontend/dist` | built SPA to serve |
| ONUS_EVAL_LOG | `<repo>/docs/public/eval_log.md` | eval-run count on the Trust page; its folder is served at /api/public |
| ONUS_FROZEN_MANIFEST | `<repo>/frozen.json` | the freeze (version, fingerprints) on the Trust page |
| LIVE_CODE_CHECKS | off | `1` turns on code checks for live analyses (also needs MODAL_TOKEN_ID/SECRET) |
| LIVE_MAX_RERUNS_PER_DAY | 10 | code re-runs (Modal sandboxes) per UTC day, all visitors |
| LIVE_MAX_MODAL_MINUTES_PER_DAY | 120 | Modal minutes per UTC day, all visitors |
| LIVE_MAX_RERUNS_PER_IP_PER_DAY | 2 | code re-runs per visitor (salted IP hash) per UTC day |
| LIVE_MAX_CODE_ZIP_MB | 25 | largest code .zip a visitor may upload (default in `limits.py`) |
| ONUS_ANALYTICS | on | `0` turns off the request log and `POST /api/events` (see `analytics.py`) |
| ANALYTICS_RETENTION_DAYS | 90 | analytics rows older than this are deleted |
| ANALYTICS_TOKEN | unset | bearer token for `GET /api/admin/analytics` (the author's dashboard); unset = 404 |
| ONUS_ANCHORS_WARM | on | `0` turns off computing PDF anchors in the background (on finish and at startup; `anchors_warm.py`) |
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from onus.api.limits import CODE_ZIP_MAX_MB
from onus.config import repo_root


class ServingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    onus_frontend_dist: Path | None = None
    onus_eval_log: Path | None = None
    onus_frozen_manifest: Path | None = None

    # Live code checks (Phase 11b). Off unless LIVE_CODE_CHECKS=1 and the Modal tokens are set.
    live_code_checks: bool = False
    live_max_reruns_per_day: int = 10
    live_max_modal_minutes_per_day: float = 120.0
    live_max_reruns_per_ip_per_day: int = 2
    live_max_code_zip_mb: int = CODE_ZIP_MAX_MB

    # Product analytics (no cookies; see onus.api.analytics)
    onus_analytics: bool = True
    analytics_retention_days: int = 90
    analytics_token: SecretStr | None = None

    # PDF anchors computed in the background (Phase 14; see onus.api.anchors_warm)
    onus_anchors_warm: bool = True

    @property
    def frontend_dist(self) -> Path:
        return self.onus_frontend_dist or repo_root() / "src" / "frontend" / "dist"

    @property
    def eval_log(self) -> Path:
        return self.onus_eval_log or repo_root() / "docs" / "public" / "eval_log.md"

    @property
    def frozen_manifest(self) -> Path:
        return self.onus_frozen_manifest or repo_root() / "frozen.json"


@lru_cache(maxsize=1)
def get_serving_settings() -> ServingSettings:
    return ServingSettings()
