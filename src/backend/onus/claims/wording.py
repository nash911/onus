"""Wording strength (lexicon) and scope classification. (Phase 3)

- `strength` comes only from `rubrics/lexicon.yaml` (Appendix B.1): hedged > absolute > strong > plain.
- `scope` comes from the lexicon's scope markers when any fire; otherwise from prompt `classify_scope`
  (fast role). Deterministic markers win when they disagree with the model. If the model call fails, the
  scope is "specific" (the less severe reading) and the caller records a warning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from onus.config import rubrics_dir
from onus.llm.client import CallMeta, LLMError
from onus.schemas.result import Wording

Strength = Literal["hedged", "plain", "strong", "absolute"]
Scope = Literal["specific", "general", "universal"]

STRENGTH_ORDER: tuple[Strength, ...] = ("hedged", "absolute", "strong")
SCOPE_ORDER: tuple[Scope, ...] = ("specific", "universal", "general")


def _compile(entry: str) -> re.Pattern[str]:
    if entry.startswith("re:"):
        body = entry[3:]
    else:
        parts = [re.escape(w) for w in entry.split()]
        body = r"\s+".join(parts).replace(r"\-", r"[-\s]?")
    return re.compile(rf"(?<![\w-])(?:{body})(?![\w-])", re.IGNORECASE)


@dataclass(frozen=True)
class Lexicon:
    strength: dict[str, list[tuple[str, re.Pattern[str]]]]
    scope: dict[str, list[tuple[str, re.Pattern[str]]]]

    @staticmethod
    def _markers(table: dict[str, list[tuple[str, re.Pattern[str]]]], text: str) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for level, entries in table.items():
            hits = []
            for _, pat in entries:
                hits += [m.group(0).lower() for m in pat.finditer(text)]
            if hits:
                found[level] = list(dict.fromkeys(hits))
        return found

    def strength_of(self, text: str) -> tuple[Strength, list[str]]:
        found = self._markers(self.strength, text)
        markers = [m for level in STRENGTH_ORDER for m in found.get(level, [])]
        for level in STRENGTH_ORDER:
            if level in found:
                return level, markers
        return "plain", markers

    def scope_of(self, text: str) -> tuple[Scope | None, list[str]]:
        found = self._markers(self.scope, text)
        markers = [m for level in SCOPE_ORDER for m in found.get(level, [])]
        for level in SCOPE_ORDER:
            if level in found:
                return level, markers
        return None, markers


def _load(path: Path) -> Lexicon:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    def table(section: str, levels: tuple[str, ...]) -> dict[str, list[tuple[str, re.Pattern[str]]]]:
        sec = raw.get(section) or {}
        return {lv: [(str(e), _compile(str(e))) for e in sec.get(lv) or []] for lv in levels}
    return Lexicon(strength=table("strength", STRENGTH_ORDER), scope=table("scope", SCOPE_ORDER))


@lru_cache(maxsize=8)
def _load_cached(path: Path, mtime_ns: int) -> Lexicon:
    return _load(path)


def load_lexicon(path: Path | None = None) -> Lexicon:
    p = path or rubrics_dir() / "lexicon.yaml"
    return _load_cached(p, p.stat().st_mtime_ns)


class ScopeOut(BaseModel):
    scope: Scope


async def classify_scope(llm, quote: str, *, run_index: int = 0,
                         paper_id: str | None = None) -> tuple[Scope, CallMeta]:
    """Prompt `classify_scope` (fast role). Raises `LLMError` on failure."""
    out, meta = await llm.complete_json("classify_scope", {"quote": quote}, ScopeOut, run_index=run_index,
                                        paper_id=paper_id)
    return out.scope, meta


@dataclass
class WordingResult:
    wording: Wording
    scope_source: Literal["markers", "model", "default"]
    model_scope: Scope | None = None
    error: str | None = None
    meta: CallMeta | None = None


async def wording_for(quote: str, *, llm=None, lexicon: Lexicon | None = None, run_index: int = 0,
                      paper_id: str | None = None) -> WordingResult:
    """Strength from the lexicon; scope from lexicon markers, else the model (when `llm` is given)."""
    lex = lexicon or load_lexicon()
    strength, s_markers = lex.strength_of(quote)
    det_scope, c_markers = lex.scope_of(quote)
    markers = list(dict.fromkeys(s_markers + c_markers))
    model_scope: Scope | None = None
    error = None
    meta = None
    if llm is not None:
        try:
            model_scope, meta = await classify_scope(llm, quote, run_index=run_index, paper_id=paper_id)
        except LLMError as e:
            error = str(e)[:200]
    if det_scope is not None:
        scope, source = det_scope, "markers"
    elif model_scope is not None:
        scope, source = model_scope, "model"
    else:
        scope, source = "specific", "default"
    return WordingResult(Wording(strength=strength, scope=scope, markers=markers), source, model_scope, error,
                         meta)
