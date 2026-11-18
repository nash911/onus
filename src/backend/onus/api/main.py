"""FastAPI app (BUILD_PLAN Phase 8): `uvicorn onus.api.main:app`.

Startup: create tables (SQLite: WAL), import bundles from ONUS_RESULTS_IMPORT_DIR and `$ONUS_VAR_DIR/bundles`,
mark stale live jobs failed, start the live job worker. `create_app()` takes overrides so tests can use a temp
database, a fake pipeline and a fake fetcher.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from onus.api.analytics import AnalyticsSink, RequestLogMiddleware
from onus.api.analytics import router as analytics_router
from onus.api.anchors_warm import AnchorWarmer
from onus.api.code_stage import CodeDeps, CodeRunner, default_deps
from onus.api.context import AppContext
from onus.api.jobs import (
    Analyzer,
    Fetcher,
    JobRunner,
    TitleFetcher,
    default_fetcher,
    default_title_fetcher,
    make_pipeline_analyzer,
    no_title_fetcher,
)
from onus.api.limits import CodeLimits, LiveLimits, load_salt
from onus.api.routes_analyses import router as analyses_router
from onus.api.routes_anchors import router as anchors_router
from onus.api.routes_meta import router as meta_router
from onus.api.routes_papers import router as papers_router
from onus.api.settings import ServingSettings, get_serving_settings
from onus.api.static import mount_spa
from onus.config import Settings, get_settings
from onus.db.importer import import_bundles
from onus.db.session import init_db, make_engine, make_sessionmaker

log = logging.getLogger("onus.api")


def seed_capabilities(var_dir: Path, seed: Path | None = None, provider: str = "berget") -> bool:
    """Copy the probed `<provider>_capabilities.json` shipped with the app into `$ONUS_VAR_DIR` when it is missing or
    differs. Without it the LLM client falls back to safe defaults (no judge reasoning parameter, json_object
    instead of json_schema), so a deployed live analysis would not run the way the evaluated version ran."""
    from onus.config import repo_root
    from onus.llm.capabilities import caps_file

    name = caps_file(provider)
    seed = seed or repo_root() / "var" / name
    target = var_dir / name
    try:
        if not seed.is_file() or seed.resolve() == target.resolve():
            return False
        data = seed.read_bytes()
        if target.is_file() and target.read_bytes() == data:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        return True
    except OSError as e:
        log.warning("could not seed %s: %s", name, e)
        return False


def import_dirs(settings: Settings) -> list[Path]:
    dirs = [settings.resolve(settings.onus_results_import_dir), settings.var_dir / "bundles"]
    return list(dict.fromkeys(d.resolve() for d in dirs))


def modal_ready(settings: Settings) -> bool:
    """Both Modal tokens set (in settings or the environment); never reads or prints the values."""
    import os

    return bool((settings.modal_token_id.get_secret_value() or os.environ.get("MODAL_TOKEN_ID"))
                and (settings.modal_token_secret.get_secret_value() or os.environ.get("MODAL_TOKEN_SECRET")))


def make_code_runner(factory, settings: Settings, serving: ServingSettings, deps: CodeDeps | None,
                     tmp_root: Path | None) -> CodeRunner:
    from onus.config import is_within

    enabled = serving.live_code_checks and modal_ready(settings)
    import tempfile

    tmp = Path(tmp_root or tempfile.gettempdir())
    if enabled and is_within(tmp.resolve(), settings.var_dir):
        log.warning("live code checks off: the temporary folder %s is inside ONUS_VAR_DIR", tmp)
        enabled = False
    limits = CodeLimits(serving.live_max_reruns_per_day, serving.live_max_modal_minutes_per_day,
                        serving.live_max_reruns_per_ip_per_day, float(settings.onus_rerun_max_minutes))
    return CodeRunner(factory, settings, limits, enabled, deps or default_deps(settings), tmp_root=tmp_root)


def create_app(settings: Settings | None = None, *, analyzer: Analyzer | None = None,
               fetcher: Fetcher | None = None, fingerprint: str | None = None,
               frontend_dist: Path | None = None, eval_log: Path | None = None,
               frozen_manifest: Path | None = None, serving: ServingSettings | None = None,
               code_deps: CodeDeps | None = None, title_fetcher: TitleFetcher | None = None,
               code_tmp: Path | None = None) -> FastAPI:
    settings = settings or get_settings()
    serving = serving or get_serving_settings()
    # The metadata title is fetched with the real PDF fetcher; an injected (test) fetcher gets no network.
    title_fetcher = title_fetcher or (default_title_fetcher if fetcher is None else no_title_fetcher)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from onus.version import ONUS_VERSION
        from onus.version import fingerprint as compute_fingerprint

        fp = fingerprint or compute_fingerprint(settings=settings)
        from onus.llm.capabilities import PROVIDER_REASONING_STYLES, Capabilities, caps_file

        for prov in PROVIDER_REASONING_STYLES:  # every shipped provider file, so switching provider needs no redeploy
            if seed_capabilities(settings.var_dir, provider=prov):
                log.info("seeded %s into %s", caps_file(prov), settings.var_dir)
        provider = settings.onus_llm_provider
        probed_at = Capabilities.load(settings.var_dir, provider).probed_at
        if not probed_at:
            log.warning("no %s in %s: live analyses use the client's safe defaults", caps_file(provider),
                        settings.var_dir)
        engine = make_engine(settings.database_url)
        init_db(engine)
        factory = make_sessionmaker(engine)
        code = make_code_runner(factory, settings, serving, code_deps, code_tmp)
        warmer = AnchorWarmer(factory, settings.var_dir, enabled=serving.onus_anchors_warm)
        code.anchors = warmer
        runner = JobRunner(factory, settings, analyzer=analyzer or make_pipeline_analyzer(settings, fp),
                           fetcher=fetcher or default_fetcher, title_fetcher=title_fetcher, code=code,
                           anchors=warmer)
        dirs = import_dirs(settings)
        report = import_bundles(factory, dirs).as_dict()
        log.info("bundles imported: %s", report)
        stale = runner.recover()
        if stale:
            log.info("marked %d stale live job(s) failed", stale)
        stale = code.recover()
        if stale:
            log.info("marked %d stale code stage(s) failed", stale)
        log.info("live code checks: %s", "on" if code.enabled else "off")
        analytics = AnalyticsSink(factory, enabled=serving.onus_analytics,
                                  retention_days=serving.analytics_retention_days)
        ctx = AppContext(settings=settings, factory=factory, runner=runner, onus_version=ONUS_VERSION,
                         fingerprint=fp, models=settings.models_config, limits=LiveLimits.from_settings(settings),
                         salt=load_salt(settings.var_dir), import_dirs=dirs, import_report=report,
                         eval_log=eval_log or serving.eval_log, capabilities_probed_at=probed_at,
                         frozen_manifest=frozen_manifest or serving.frozen_manifest, serving=serving, code=code,
                         analytics=analytics)
        app.state.ctx = ctx
        runner.start()
        code.start()
        analytics.start()
        warmer.warm_existing()  # stale or missing anchors caches (e.g. after an ANCHORS_VERSION bump), one at a time
        try:
            yield
        finally:
            await runner.stop()
            await code.stop()
            await analytics.stop()
            await asyncio.to_thread(warmer.shutdown)
            engine.dispose()

    app = FastAPI(title="Onus", version="1", lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)
    app.include_router(meta_router)
    app.include_router(papers_router)
    app.include_router(analyses_router)
    app.include_router(anchors_router)
    app.include_router(analytics_router)
    app.add_middleware(RequestLogMiddleware)
    mount_spa(app, frontend_dist)
    return app


app = create_app()
