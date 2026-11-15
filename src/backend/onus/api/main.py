"""FastAPI app (BUILD_PLAN Phase 8): `uvicorn onus.api.main:app`.

Startup: create tables (SQLite: WAL), import bundles from ONUS_RESULTS_IMPORT_DIR and `$ONUS_VAR_DIR/bundles`,
mark stale live jobs failed, start the live job worker. `create_app()` takes overrides so tests can use a temp
database, a fake pipeline and a fake fetcher.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from onus.api.context import AppContext
from onus.api.jobs import Analyzer, Fetcher, JobRunner, default_fetcher, make_pipeline_analyzer
from onus.api.limits import LiveLimits, load_salt
from onus.api.routes_analyses import router as analyses_router
from onus.api.routes_meta import router as meta_router
from onus.api.routes_papers import router as papers_router
from onus.api.static import mount_spa
from onus.config import Settings, get_settings
from onus.db.importer import import_bundles
from onus.db.session import init_db, make_engine, make_sessionmaker

log = logging.getLogger("onus.api")


def import_dirs(settings: Settings) -> list[Path]:
    dirs = [settings.resolve(settings.onus_results_import_dir), settings.var_dir / "bundles"]
    return list(dict.fromkeys(d.resolve() for d in dirs))


def create_app(settings: Settings | None = None, *, analyzer: Analyzer | None = None,
               fetcher: Fetcher | None = None, fingerprint: str | None = None,
               frontend_dist: Path | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from onus.version import ONUS_VERSION
        from onus.version import fingerprint as compute_fingerprint

        fp = fingerprint or compute_fingerprint(settings=settings)
        engine = make_engine(settings.database_url)
        init_db(engine)
        factory = make_sessionmaker(engine)
        runner = JobRunner(factory, settings, analyzer=analyzer or make_pipeline_analyzer(settings, fp),
                           fetcher=fetcher or default_fetcher)
        dirs = import_dirs(settings)
        report = import_bundles(factory, dirs).as_dict()
        log.info("bundles imported: %s", report)
        stale = runner.recover()
        if stale:
            log.info("marked %d stale live job(s) failed", stale)
        ctx = AppContext(settings=settings, factory=factory, runner=runner, onus_version=ONUS_VERSION,
                         fingerprint=fp, models=settings.models_config, limits=LiveLimits.from_settings(settings),
                         salt=load_salt(settings.var_dir), import_dirs=dirs, import_report=report)
        app.state.ctx = ctx
        runner.start()
        try:
            yield
        finally:
            await runner.stop()
            engine.dispose()

    app = FastAPI(title="Onus", version="1", lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)
    app.include_router(meta_router)
    app.include_router(papers_router)
    app.include_router(analyses_router)
    mount_spa(app, frontend_dist)
    return app


app = create_app()
