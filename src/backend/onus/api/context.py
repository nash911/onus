"""Per-app state shared by the routes: settings, DB session factory, job runner, version info, limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from onus.api.jobs import JobRunner
from onus.api.limits import LiveLimits
from onus.config import Settings


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

    @property
    def live_enabled(self) -> bool:
        return bool(self.settings.berget_api_key.get_secret_value())

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
