"""Loaders for the Phase 5 rule tables: `rubrics/strength_rules.yaml`, `tolerances.yaml`, `checklist_map.yaml`."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from onus.claims.wording import _compile
from onus.config import rubrics_dir

REQUIREMENTS = {"stat_test", "baselines>=2", "settings>=2", "dominance", "dominance_if_comparison",
                "variance_or_3_seeds"}
CHECKLIST_KINDS = {"spread_or_pattern", "code_or_repo", "limitations_present", "pattern", "any_of", "derived"}


class RubricError(ValueError):
    pass


@dataclass(frozen=True)
class StrengthRule:
    id: str
    title: str
    markers: tuple[tuple[str, re.Pattern[str]], ...]
    requires: tuple[str, ...]
    when: str | None = None  # "absolute_or_universal": fires on the claim's wording, not on markers

    def hits(self, text: str) -> list[str]:
        out: list[str] = []
        for _, pat in self.markers:
            out += [m.group(0).lower() for m in pat.finditer(text)]
        return list(dict.fromkeys(out))


@dataclass(frozen=True)
class StrengthRules:
    rules: tuple[StrengthRule, ...]
    comparison: tuple[tuple[str, re.Pattern[str]], ...]
    claim_types: tuple[str, ...] = ("result", "comparison", "generality", "causal")

    def comparison_hits(self, text: str) -> list[str]:
        out: list[str] = []
        for _, pat in self.comparison:
            out += [m.group(0).lower() for m in pat.finditer(text)]
        return list(dict.fromkeys(out))


@dataclass(frozen=True)
class Tolerances:
    abs_half_unit: float = 0.5
    rel: float = 0.005
    percent_scaling: bool = True

    def tol(self, claimed: float, decimals: int) -> float:
        return max(self.abs_half_unit * 10 ** (-decimals), self.rel * abs(claimed))


@dataclass(frozen=True)
class ChecklistRule:
    key: str
    check: str
    pattern: re.Pattern[str] | None = None
    roles: tuple[str, ...] | None = None
    also_justification: bool = False
    of: tuple[str, ...] = ()
    messages: dict[str, str] = field(default_factory=dict)


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_strength_rules(path: Path | None = None) -> StrengthRules:
    raw = _yaml(path or rubrics_dir() / "strength_rules.yaml")
    rules = []
    for r in raw.get("rules") or []:
        req = tuple(r.get("requires") or ())
        bad = set(req) - REQUIREMENTS
        if bad:
            raise RubricError(f"strength rule {r.get('id')}: unknown requirement(s) {sorted(bad)}")
        rules.append(StrengthRule(id=r["id"], title=r.get("title", r["id"]), requires=req, when=r.get("when"),
                                  markers=tuple((str(m), _compile(str(m))) for m in r.get("markers") or ())))
    comp = tuple((str(m), _compile(str(m))) for m in raw.get("comparison_markers") or ())
    types = tuple(raw.get("claim_types") or ("result", "comparison", "generality", "causal"))
    return StrengthRules(rules=tuple(rules), comparison=comp, claim_types=types)


def load_tolerances(path: Path | None = None) -> Tolerances:
    raw = _yaml(path or rubrics_dir() / "tolerances.yaml")
    return Tolerances(abs_half_unit=float(raw.get("abs_half_unit", 0.5)), rel=float(raw.get("rel", 0.005)),
                      percent_scaling=bool(raw.get("percent_scaling", True)))


def load_checklist_map(path: Path | None = None) -> dict[str, ChecklistRule]:
    raw = _yaml(path or rubrics_dir() / "checklist_map.yaml")
    out: dict[str, ChecklistRule] = {}
    for key, r in (raw.get("items") or {}).items():
        if r.get("check") not in CHECKLIST_KINDS:
            raise RubricError(f"checklist item {key}: unknown check {r.get('check')!r}")
        pat = None
        if r.get("pattern"):
            flags = 0 if r.get("case_sensitive") else re.IGNORECASE
            pat = re.compile(r["pattern"], flags)
        msgs = {k: str(r[k]) for k in ("unbacked", "contradicted", "note") if k in r}
        out[key] = ChecklistRule(key=key, check=r["check"], pattern=pat,
                                 roles=tuple(r["roles"]) if r.get("roles") else None,
                                 also_justification=bool(r.get("also_justification")), of=tuple(r.get("of") or ()),
                                 messages=msgs)
    return out


@lru_cache(maxsize=1)
def default_strength_rules() -> StrengthRules:
    return load_strength_rules()


@lru_cache(maxsize=1)
def default_tolerances() -> Tolerances:
    return load_tolerances()


@lru_cache(maxsize=1)
def default_checklist_map() -> dict[str, ChecklistRule]:
    return load_checklist_map()
