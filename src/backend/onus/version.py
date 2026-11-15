"""Version, pipeline fingerprint and harness fingerprint (BUILD_PLAN §7.4).

fingerprint = sha256(ONUS_VERSION + models config + prompts/*.md (except the harness prompts) + rubrics/*.yaml
                     + pipeline source)[:12]
Pipeline source = every .py under src/backend/onus that can change an analysis result, so code-only changes
change the fingerprint. Excluded (they never change a RunResult):
- eval/ (the harness has its own fingerprint), api/ and db/ (serving), codecheck/ (code-v1 has its own fingerprint);
- bundle.py (display metadata), version.py (its only moving part, ONUS_VERSION, is hashed as "version";
  `onus freeze` rewrites that line);
- ingest/fetch.py (live-mode PDF download, used only by the API), llm/fake.py (test double), and the Markdown
  dev-report writers checks/, claims/, evidence/ and verdict/report.py.

harness_fingerprint = sha256(eval/{__init__,mutate,baseline,metrics,truth,harness}.py + the harness prompts
baseline_support and widen_scope + the checklist audit's error_bars wording, which delete_errorbars removes)[:12]. It is fixed from eval #1 (docs/public/eval_protocol.md §4).

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
HARNESS_PROMPTS = ("baseline_support.md", "widen_scope.md")
HARNESS_MODULES = ("__init__.py", "mutate.py", "baseline.py", "metrics.py", "truth.py", "harness.py")
SOURCE_EXCLUDED_DIRS = ("eval", "api", "db", "codecheck")
SOURCE_EXCLUDED_FILES = ("bundle.py", "version.py", "ingest/fetch.py", "llm/fake.py", "checks/report.py",
                         "claims/report.py", "evidence/report.py", "verdict/report.py")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hashes(directory: Path, pattern: str) -> dict[str, str]:
    """sha256 of each matching file (top level only), keyed by file name, sorted."""
    if not directory.is_dir():
        return {}
    return {p.name: _sha256(p.read_bytes()) for p in sorted(directory.glob(pattern)) if p.is_file()}


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def source_hashes(pkg: Path | None = None) -> dict[str, str]:
    """sha256 of every pipeline .py file, keyed by its path relative to the package."""
    pkg = pkg or package_dir()
    out = {}
    for p in sorted(pkg.rglob("*.py")):
        rel = p.relative_to(pkg)
        if "__pycache__" in rel.parts or rel.parts[0] in SOURCE_EXCLUDED_DIRS or rel.as_posix() in SOURCE_EXCLUDED_FILES:
            continue
        out[rel.as_posix()] = _sha256(p.read_bytes())
    return out


def fingerprint_parts(
    version: str | None = None,
    settings: Settings | None = None,
    prompts: Path | None = None,
    rubrics: Path | None = None,
    source: Path | None = None,
) -> dict:
    s = settings or get_settings()
    pr = {k: v for k, v in file_hashes(prompts or prompts_dir(), "*.md").items() if k not in HARNESS_PROMPTS}
    return {
        "version": version or ONUS_VERSION,
        "models": s.models_config,
        "prompts": pr,
        "rubrics": file_hashes(rubrics or rubrics_dir(), "*.yaml"),
        "source": source_hashes(source),
    }


def harness_parts(prompts: Path | None = None, pkg: Path | None = None) -> dict:
    ev = (pkg or package_dir()) / "eval"
    pr = file_hashes(prompts or prompts_dir(), "*.md")
    from onus.checks.rubrics import load_checklist_map

    cmap = rubrics_dir() / "checklist_map.yaml"
    rules = load_checklist_map(cmap) if cmap.is_file() else {}
    eb = rules["error_bars"].pattern if "error_bars" in rules else None
    return {"modules": {m: _sha256((ev / m).read_bytes()) for m in HARNESS_MODULES if (ev / m).is_file()},
            "prompts": {k: v for k, v in pr.items() if k in HARNESS_PROMPTS},
            # delete_errorbars removes what the audit accepts (harness v2), so that wording scores the harness too
            "audit_error_bars": _sha256(eb.pattern.encode()) if eb else None}


def harness_fingerprint(prompts: Path | None = None, pkg: Path | None = None) -> str:
    return fingerprint_from_parts(harness_parts(prompts, pkg))


def fingerprint_from_parts(parts: dict) -> str:
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return _sha256(canonical.encode())[:FINGERPRINT_LEN]


def fingerprint(
    version: str | None = None,
    settings: Settings | None = None,
    prompts: Path | None = None,
    rubrics: Path | None = None,
    source: Path | None = None,
) -> str:
    return fingerprint_from_parts(fingerprint_parts(version, settings, prompts, rubrics, source))


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
    _set_version_in_source(new_version)  # first, so the recorded source hashes are those on disk afterwards
    ONUS_VERSION = new_version
    parts = fingerprint_parts(new_version, settings)
    record = {
        "version": new_version,
        "fingerprint": fingerprint_from_parts(parts),
        "models": parts["models"],
        "prompts": parts["prompts"],
        "rubrics": parts["rubrics"],
        "source": parts["source"],
        "harness_fingerprint": harness_fingerprint(),
        "harness": harness_parts(),
        "created_at": datetime.now(UTC).isoformat(),
    }
    existing = load_frozen() or {}
    if "code" in existing:  # keep a code-v1 freeze if one exists
        record["code"] = existing["code"]
    frozen_path().write_text(json.dumps(record, indent=2) + "\n")
    return record
