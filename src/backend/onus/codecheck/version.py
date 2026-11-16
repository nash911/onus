"""code-v1 version and fingerprint (BUILD_PLAN §7.4, Phase 10).

code_fingerprint = sha256(CODE_VERSION + models config + prompts/code/*.md + rubrics/code/*.yaml
                          + the shared tolerance rubric (rubrics/tolerances.yaml, read-only here)
                          + every .py under codecheck/)[:12]

The P0 pipeline fingerprint counts only the top level of prompts/ and rubrics/ and excludes codecheck/, so
nothing here moves it. `onus freeze --code-version X.Y.Z` (codecheck/cli.py:freeze_code) rewrites the
CODE_VERSION line below and records the result under "code" in frozen.json; `onus.version.freeze` keeps that
record when the P0 version is re-frozen. `onus.version.CODE_VERSION` stays as it is: this module owns the code
version so that version.py (inside the frozen P0 hash) is never edited for code-v1.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from onus.config import Settings, get_settings, prompts_dir, rubrics_dir
from onus.version import _sha256, file_hashes, fingerprint_from_parts, frozen_path, load_frozen

CODE_VERSION = "1.0.0"
SHARED_RUBRICS = ("tolerances.yaml",)


def code_prompts_dir() -> Path:
    return prompts_dir() / "code"


def code_rubrics_dir() -> Path:
    return rubrics_dir() / "code"


def codecheck_dir() -> Path:
    return Path(__file__).resolve().parent


def code_source_hashes(pkg: Path | None = None) -> dict[str, str]:
    pkg = pkg or codecheck_dir()
    return {p.relative_to(pkg).as_posix(): _sha256(p.read_bytes()) for p in sorted(pkg.rglob("*.py"))
            if "__pycache__" not in p.relative_to(pkg).parts}


def code_parts(version: str | None = None, settings: Settings | None = None, prompts: Path | None = None,
               rubrics: Path | None = None, source: Path | None = None) -> dict:
    s = settings or get_settings()
    shared = {k: v for k, v in file_hashes(rubrics_dir(), "*.yaml").items() if k in SHARED_RUBRICS}
    return {"version": version or CODE_VERSION, "models": s.models_config,
            "prompts": file_hashes(prompts or code_prompts_dir(), "*.md"),
            "rubrics": file_hashes(rubrics or code_rubrics_dir(), "*.yaml"),
            "shared_rubrics": shared, "source": code_source_hashes(source)}


def code_fingerprint(**kw) -> str:
    return fingerprint_from_parts(code_parts(**kw))


def code_version_label() -> str:
    """What RunResult.code.code_version records: "<version>+<fingerprint>"."""
    return f"{CODE_VERSION}+{code_fingerprint()}"


_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_LINE = re.compile(r'^CODE_VERSION = "[^"]*"$', re.MULTILINE)


def freeze_code_version(new_version: str) -> dict:
    """Set CODE_VERSION and record the code-v1 fingerprint under "code" in frozen.json (P0 keys untouched)."""
    global CODE_VERSION
    if not _SEMVER.match(new_version):
        raise ValueError(f"Version must look like X.Y.Z, got {new_version!r}")
    frozen = load_frozen()
    if frozen is None:
        raise RuntimeError("frozen.json does not exist: freeze the P0 pipeline first (onus freeze --version X.Y.Z)")
    src = Path(__file__)
    text, n = _LINE.subn(f'CODE_VERSION = "{new_version}"', src.read_text(), count=1)
    if n != 1:
        raise RuntimeError("Could not find the CODE_VERSION line in codecheck/version.py")
    src.write_text(text)  # first, so the recorded source hashes are those on disk afterwards
    CODE_VERSION = new_version
    parts = code_parts(new_version)
    frozen["code"] = {"version": new_version, "fingerprint": fingerprint_from_parts(parts), **parts,
                      "created_at": datetime.now(UTC).isoformat()}
    frozen_path().write_text(json.dumps(frozen, indent=2) + "\n")
    return frozen["code"]
