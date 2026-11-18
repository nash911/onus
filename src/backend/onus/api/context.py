"""Per-app state shared by the routes: settings, DB session factory, job runner, version info, limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from onus.api.code_stage import CodeRunner
from onus.api.jobs import JobRunner
from onus.api.limits import LiveLimits
from onus.api.settings import ServingSettings
from onus.config import Settings

if TYPE_CHECKING:
    from onus.api.analytics import AnalyticsSink


def llm_key(settings: Settings) -> str:
    """The API key of the provider every model role runs on (a value, never to be printed). Read defensively, so
    a configuration without a provider setting behaves as Berget did in v1.0."""
    provider = getattr(settings, "onus_llm_provider", "berget") or "berget"
    key = getattr(settings, f"{provider}_api_key", None)
    return key.get_secret_value() if key is not None else ""


@dataclass
class AppContext:
    settings: Settings
    factory: sessionmaker[Session]
    runner: JobRunner
    onus_version: str
    fingerprint: str
    models: dict[str, str]
    limits: LiveLimits
    salt: bytes
    import_dirs: list[Path] = field(default_factory=list)
    import_report: dict = field(default_factory=dict)
    eval_log: Path | None = None
    frozen_manifest: Path | None = None
    capabilities_probed_at: str | None = None
    serving: ServingSettings = field(default_factory=ServingSettings)
    code: CodeRunner | None = None
    analytics: AnalyticsSink | None = None

    @property
    def live_enabled(self) -> bool:
        """On when the key of the configured LLM provider (ONUS_LLM_PROVIDER: berget or gemini) is set."""
        return bool(llm_key(self.settings))

    @property
    def code_checks_enabled(self) -> bool:
        """LIVE_CODE_CHECKS=1, the Modal tokens set, and live analysis on (the static check needs the LLM)."""
        return self.live_enabled and self.code is not None and self.code.enabled

    def data_file(self, name: str) -> Path | None:
        """`findings.json` / `metrics.json`: the first copy found in the import directories."""
        for d in self.import_dirs:
            p = d / name
            try:
                if p.is_file():
                    return p
            except OSError:
                continue
        return None


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]
