"""Serve the built SPA (`src/frontend/dist`) with an index.html fallback; `/api/*` is never shadowed.

The directory comes from `onus.api.settings` (`ONUS_FRONTEND_DIST`, default `<repo>/src/frontend/dist`).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from onus.api.settings import get_serving_settings

NO_BUILD = ("<!doctype html><meta charset=utf-8><title>Onus</title>"
            "<p>The frontend is not built. Run <code>npm run build</code> in <code>src/frontend</code>, "
            "or use the Vite dev server. The API is under <a href=/api/meta>/api</a>.</p>")


def frontend_dist() -> Path:
    return get_serving_settings().frontend_dist


def mount_spa(app: FastAPI, dist: Path | None = None) -> None:
    root = (dist or frontend_dist()).resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str, request: Request) -> Response:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Not found.")
        index = root / "index.html"
        if not index.is_file():
            return HTMLResponse(NO_BUILD)
        if path:
            target = (root / path).resolve()
            if target.is_relative_to(root) and target.is_file():
                cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
                return FileResponse(target, headers={"Cache-Control": cache})
            if "." in path.rsplit("/", 1)[-1]:
                raise HTTPException(404, "Not found.")  # a missing asset, not a client-side route
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
