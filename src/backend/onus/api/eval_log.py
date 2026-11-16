"""Read the public held-out run log (`docs/public/eval_log.md`) for the Trust page's eval-run count.

The log is a markdown table written by the author before each held-out run. Only rows whose Split column is
exactly "eval" are counted (the go/no-go runs before the freeze; at most 3). Only the run id, start time, git
commit and the two fingerprints are returned; the free-text columns are not.

A missing or unreadable log, or one without the expected header, gives an empty list (the page then says that
at most 3 runs are allowed).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_COLUMNS = {
    "run": ("#",),
    "split": ("split",),
    "started": ("started (utc)", "started"),
    "commit": ("git commit", "commit"),
    "pipeline_fingerprint": ("pipeline fingerprint",),
    "harness_fingerprint": ("harness fingerprint",),
}


def _cells(line: str) -> list[str]:
    s = line.strip()
    s = s.removeprefix("|")
    s = s.removesuffix("|")
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def _clean(v: str) -> str | None:
    v = v.strip().strip("`").strip()
    return v or None


def parse_eval_log(text: str, split: str = "eval") -> list[dict[str, Any]]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        head = [h.lower() for h in _cells(line)]
        idx = {key: next((head.index(n) for n in names if n in head), None) for key, names in _COLUMNS.items()}
        if idx["split"] is None or idx["run"] is None:
            continue
        rows = []
        for row_line in lines[i + 1:]:
            if not row_line.lstrip().startswith("|"):
                break
            cells = _cells(row_line)
            if all(set(c) <= set("-: ") for c in cells):
                continue  # the |---| separator
            get = {k: (_clean(cells[j]) if j is not None and j < len(cells) else None) for k, j in idx.items()}
            if (get["split"] or "").lower() == split:
                rows.append({k: v for k, v in get.items() if k != "split"})
        return rows
    return []


def read_eval_runs(path: Path) -> list[dict[str, Any]]:
    try:
        return parse_eval_log(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return []
