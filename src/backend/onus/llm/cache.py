"""On-disk LLM response cache: `$ONUS_VAR_DIR/llm_cache/<aa>/<sha256>.json`.

Keyed on (model, messages, params, run_index), so the same input and version give the same output and
dev re-runs cost nothing. The cache holds paper text: for held-out runs it must live outside the repo.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def cache_key(model: str, messages: list[dict] | list[str], params: dict[str, Any], run_index: int) -> str:
    payload = {"model": model, "messages": messages, "params": params, "run_index": run_index}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class LLMCache:
    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict | None:
        p = self._path(key)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None  # a torn write is a miss, not a crash

    def put(self, key: str, value: dict) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
        os.replace(tmp, p)  # atomic: concurrent writers never leave a half file
