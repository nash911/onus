"""Shared helpers for the code checks: rubrics, the paper's code tree, the code-v1 LLM client, key masking.

Pipeline rule 2 applies: everything here reads `<data_root>/papers/<id>/` (or the Document built from it), never
labels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from onus.codecheck.version import code_prompts_dir, code_rubrics_dir
from onus.schemas.document import Document

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "env", "node_modules", ".ipynb_checkpoints", ".mypy_cache",
             "site-packages"}


@lru_cache(maxsize=8)
def _rubric(name: str, mtime_ns: int) -> dict:
    return yaml.safe_load((code_rubrics_dir() / name).read_text()) or {}


def rubric(name: str) -> dict:
    p = code_rubrics_dir() / name
    return _rubric(name, p.stat().st_mtime_ns)


def code_root(doc: Document, paper_dir: Path | None) -> Path | None:
    """The directory the code index was built from: the extracted work dir when the code came as zips, else
    `<paper_dir>/code`."""
    if doc.code.root and Path(doc.code.root).is_dir():
        return Path(doc.code.root)
    if paper_dir is not None and (paper_dir / "code").is_dir():
        return paper_dir / "code"
    return None


@dataclass
class CodeFile:
    path: str  # relative to the code root, posix
    lines: list[str]


def load_code_files(root: Path, exts: set[str], max_bytes: int) -> list[CodeFile]:
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or SKIP_DIRS & set(p.relative_to(root).parts):
            continue
        if p.suffix not in exts and p.suffix.lower() not in exts:
            continue
        if p.stat().st_size > max_bytes:
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if "\x00" in text[:2000]:
            continue
        out.append(CodeFile(p.relative_to(root).as_posix(), text.splitlines()))
    return out


def code_llm(var_dir: Path | None = None):
    """An LLMClient whose prompts come from prompts/code/ (same cache, ledger and budget guard as P0)."""
    from onus.llm.client import LLMClient
    from onus.llm.prompts import PromptRegistry

    return LLMClient(var_dir=var_dir, prompts=PromptRegistry(code_prompts_dir()))


# ---------------------------------------------------------------- key masking (CLAUDE.md rule 7)

_KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"(?:ak|as)-[A-Za-z0-9]{20,}"),  # Modal token id / secret
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)((?:api[_-]?key|secret|token|password|authorization)\s*[\"']?\s*[:=]\s*[\"']?(?:Bearer\s+)?)"
               r"([A-Za-z0-9_\-\.]{16,})"),
    re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9_\-\.]{16,})"),
]
MASK = "***"


def mask_secrets(text: str, known: list[str] | tuple[str, ...] = ()) -> str:
    """Replace known secret values and anything shaped like a key. Applied to every string that leaves the
    sandbox: tool outputs shown to the agent, stored commands, modifications and stdout tails."""
    if not text:
        return text
    for k in known:
        if k and len(k) >= 6:
            text = text.replace(k, MASK)
    for pat in _KEY_PATTERNS:
        if pat.groups == 2:
            text = pat.sub(lambda m: m.group(1) + MASK, text)
        else:
            text = pat.sub(MASK, text)
    return text


def known_secret_values() -> list[str]:
    """Every secret Onus holds (never printed): masked wherever they might appear."""
    from onus.config import get_settings

    s = get_settings()
    vals = [s.berget_api_key, s.modal_token_id, s.modal_token_secret, s.sandbox_openai_api_key,
            s.sandbox_anthropic_api_key, s.sandbox_gemini_api_key]
    out = [v.get_secret_value() for v in vals if v.get_secret_value()]
    import os

    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "BERGET_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"):
        if os.environ.get(name):
            out.append(os.environ[name])
    return out
