"""Prompt registry and renderer.

One file per prompt: `prompts/<id>.md`. YAML front matter (`id, version, role, temperature, max_tokens`),
then the template. The body may be split into `# system` and `# user` sections (a line holding exactly
that heading); without them the whole body is the user message.

Variables are `{{name}}`, substituted in a single pass: substituted values (paper text) are never
re-scanned, so `{{` inside a paper cannot inject a variable. A missing variable is an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from onus.config import CHAT_ROLES, prompts_dir

_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_SECTION = re.compile(r"^#\s*(system|user)\s*$", re.MULTILINE | re.IGNORECASE)


class PromptError(ValueError):
    pass


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    role: str
    temperature: float
    max_tokens: int
    system: str | None
    user: str
    path: Path | None = None

    def variables(self) -> set[str]:
        return set(_VAR.findall(self.system or "")) | set(_VAR.findall(self.user))

    def render(self, variables: dict[str, object]) -> list[dict[str, str]]:
        messages = []
        if self.system:
            messages.append({"role": "system", "content": render_template(self.system, variables)})
        messages.append({"role": "user", "content": render_template(self.user, variables)})
        return messages


def render_template(template: str, variables: dict[str, object]) -> str:
    missing = sorted({m for m in _VAR.findall(template) if m not in variables})
    if missing:
        raise PromptError(f"Missing template variables: {', '.join(missing)}")
    return _VAR.sub(lambda m: str(variables[m.group(1)]), template)


def parse_prompt(text: str, path: Path | None = None) -> Prompt:
    m = _FRONT.match(text)
    if not m:
        raise PromptError(f"{path or 'prompt'}: missing YAML front matter")
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2)
    for key in ("id", "version", "role", "temperature", "max_tokens"):
        if key not in meta:
            raise PromptError(f"{path or 'prompt'}: front matter lacks {key!r}")
    if meta["role"] not in CHAT_ROLES:
        raise PromptError(f"{path or 'prompt'}: role must be one of {CHAT_ROLES}, got {meta['role']!r}")
    if path is not None and path.stem != meta["id"]:
        raise PromptError(f"{path}: id {meta['id']!r} does not match the file name")

    system, user = None, body.strip()
    parts = _SECTION.split(body)
    if len(parts) > 1:
        sections = {name.lower(): content.strip() for name, content in zip(parts[1::2], parts[2::2], strict=True)}
        system = sections.get("system") or None
        user = sections.get("user", "")
    if not user:
        raise PromptError(f"{path or 'prompt'}: empty user message")
    return Prompt(
        id=str(meta["id"]), version=str(meta["version"]), role=str(meta["role"]),
        temperature=float(meta["temperature"]), max_tokens=int(meta["max_tokens"]),
        system=system, user=user, path=path,
    )


class PromptRegistry:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or prompts_dir()

    def get(self, prompt_id: str) -> Prompt:
        return _load(self.directory / f"{prompt_id}.md")

    def ids(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.md"))


@lru_cache(maxsize=256)
def _load_cached(path: Path, mtime_ns: int) -> Prompt:
    return parse_prompt(path.read_text(encoding="utf-8"), path)


def _load(path: Path) -> Prompt:
    if not path.is_file():
        raise PromptError(f"No prompt file {path}")
    return _load_cached(path, path.stat().st_mtime_ns)
