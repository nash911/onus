# Onus web app (BUILD_PLAN Phase 11): the SPA plus the FastAPI API, one process, SQLite on a volume.
#
#   docker build -t onus .
#   docker run --rm -p 8000:8000 -e PORT=8000 -v "$PWD/var/bundles:/bundles:ro" \
#     -e ONUS_RESULTS_IMPORT_DIR=/bundles -e DATABASE_URL=sqlite:////tmp/onus.db -e ONUS_VAR_DIR=/tmp/var onus
#
# Stages: 1) node builds src/frontend -> dist; 2) optional inputs (deploy_data/, frozen.json);
# 3) Poetry installs the main dependencies into a venv; 4) a slim runtime with the venv, the backend source and
# the data the app serves. Poetry and node are not in the final image.

# ---------------------------------------------------------------- 1. frontend
FROM node:20-alpine AS frontend
WORKDIR /build/src/frontend
COPY src/frontend/package.json src/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY src/frontend/ ./
# src/types/result.ts is generated from schema/result.schema.json and committed; `npm run gen:types` is not rerun here.
RUN npm run build

# ---------------------------------------------------------------- 2. optional inputs
# deploy_data/ (author-only, filled at R5), frozen.json (after the freeze) and var/<provider>_capabilities.json (the
# probed model capabilities; var/ is git-ignored, so it must be force-added) may be absent. Copying the
# filtered build context here lets the runtime stage take them when present and an empty folder otherwise.
FROM python:3.12-slim AS inputs
COPY . /ctx
RUN mkdir -p /ctx/deploy_data /ctx/optional/var \
 && (cp /ctx/frozen.json /ctx/optional/ 2>/dev/null || true) \
 && (cp /ctx/var/*_capabilities.json /ctx/optional/var/ 2>/dev/null || true)

# ---------------------------------------------------------------- 3. python dependencies
FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1
RUN python -m venv /opt/venv && pip install --root-user-action=ignore "poetry>=2.0,<3"
ENV VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root

# ---------------------------------------------------------------- 4. runtime
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src/backend \
    ONUS_RESULTS_IMPORT_DIR=/app/deploy_data \
    PORT=8000
COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
# pyproject.toml marks the repo root (onus.config.repo_root); prompts, rubrics and the backend source are hashed
# into the pipeline fingerprint, so they are copied exactly as in the repo. frozen.json and
# var/*_capabilities.json come along when present; the app copies those into $ONUS_VAR_DIR at startup.
COPY pyproject.toml ./
COPY --from=inputs /ctx/optional/ ./
COPY src/backend/ src/backend/
COPY prompts/ prompts/
COPY rubrics/ rubrics/
COPY schema/ schema/
COPY docs/public/ docs/public/
COPY --from=inputs /ctx/deploy_data/ deploy_data/
COPY --from=frontend /build/src/frontend/dist/ src/frontend/dist/

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health', timeout=4)"
# One worker only: live jobs use an in-process queue and the database is SQLite on a single volume.
CMD ["sh", "-c", "exec uvicorn onus.api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
