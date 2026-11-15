"""Version and configuration fingerprint.

fingerprint = sha256(ONUS_VERSION + models config + every prompts/*.md + every rubrics/*.yaml)[:12]

Only the top level of `prompts/` and `rubrics/` counts. The code checks (P1, code-v1) keep their own
prompts and rubrics in `prompts/code/` and `rubrics/code/` with a separate fingerprint, so adding them
after the P0 freeze does not move the P0 fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from onus.config import Settings, get_settings, prompts_dir, repo_root, rubrics_dir

ONUS_VERSION = "0.1.0"
CODE_VERSION: str | None = None

FINGERPRINT_LEN = 12


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hashes(directory: Path, pattern: str) -> dict[str, str]:
    """sha256 of each matching file (top level only), keyed by file name, sorted."""
    if not directory.is_dir():
        return {}
    return {p.name: _sha256(p.read_bytes()) for p in sorted(directory.glob(pattern)) if p.is_file()}


def fingerprint_parts(
    version: str | None = None,
    settings: Settings | None = None,
    prompts: Path | None = None,
    rubrics: Path | None = None,
) -> dict:
    s = settings or get_settings()
    return {
        "version": version or ONUS_VERSION,
        "models": s.models_config,
        "prompts": file_hashes(prompts or prompts_dir(), "*.md"),
        "rubrics": file_hashes(rubrics or rubrics_dir(), "*.yaml"),
    }


def fingerprint_from_parts(parts: dict) -> str:
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return _sha256(canonical.encode())[:FINGERPRINT_LEN]


def fingerprint(
    version: str | None = None,
    settings: Settings | None = None,
    prompts: Path | None = None,
    rubrics: Path | None = None,
) -> str:
    return fingerprint_from_parts(fingerprint_parts(version, settings, prompts, rubrics))


def frozen_path() -> Path:
    return repo_root() / "frozen.json"


def load_frozen() -> dict | None:
    p = frozen_path()
    return json.loads(p.read_text()) if p.is_file() else None


_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_VERSION_LINE = re.compile(r'^ONUS_VERSION = "[^"]*"$', re.MULTILINE)


def _set_version_in_source(new_version: str) -> None:
    src = Path(__file__)
    text = src.read_text()
    new_text, n = _VERSION_LINE.subn(f'ONUS_VERSION = "{new_version}"', text, count=1)
    if n != 1:
        raise RuntimeError("Could not find the ONUS_VERSION line in version.py")
    src.write_text(new_text)


def freeze(new_version: str, settings: Settings | None = None) -> dict:
    """Set ONUS_VERSION and pin the fingerprint in frozen.json. Returns the frozen record."""
    global ONUS_VERSION
    if not _SEMVER.match(new_version):
        raise ValueError(f"Version must look like X.Y.Z, got {new_version!r}")
    parts = fingerprint_parts(new_version, settings)
    record = {
        "version": new_version,
        "fingerprint": fingerprint_from_parts(parts),
        "models": parts["models"],
        "prompts": parts["prompts"],
        "rubrics": parts["rubrics"],
        "created_at": datetime.now(UTC).isoformat(),
    }
    _set_version_in_source(new_version)
    ONUS_VERSION = new_version
    existing = load_frozen() or {}
    if "code" in existing:  # keep a code-v1 freeze if one exists
        record["code"] = existing["code"]
    frozen_path().write_text(json.dumps(record, indent=2) + "\n")
    return record
