"""Readers for a split root's `labels/` (eval and bundle only).

`papers.csv`: one row per paper (forum_id, status, code_exec_tier, code_audit, ai_overall_mean,
human_overall_mean, title, ...). `conference/codebase_audits_results.csv`: the conference's code audit
("Audit Result": PASSED, PARTIAL, CRITICAL, FAILED, ERROR, NO CODEBASE). `code_tests.csv`: our execution test
of the submitted code. Missing files or columns read as empty, never as an error.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from . import label_paths

AUDIT_OK = {"PASSED"}
AUDIT_MISMATCH = {"PARTIAL", "CRITICAL", "FAILED"}
AUDIT_EXCLUDED = {"ERROR", "NO CODEBASE", ""}


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def _float(x: str | None) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except ValueError:
        return None


@dataclass
class SplitLabels:
    root: Path
    papers: dict[str, dict[str, str]] = field(default_factory=dict)
    audits: dict[str, str] = field(default_factory=dict)  # forum_id -> audit result (upper case)
    code_tests: dict[str, dict[str, str]] = field(default_factory=dict)
    reviews: set[str] = field(default_factory=set)

    @property
    def present(self) -> bool:
        return bool(self.papers)

    def audit_class(self, pid: str) -> int | None:
        """1 = mismatch (PARTIAL/CRITICAL/FAILED), 0 = PASSED, None = excluded (ERROR, NO CODEBASE, missing)."""
        a = self.audits.get(pid, "")
        if a in AUDIT_MISMATCH:
            return 1
        if a in AUDIT_OK:
            return 0
        return None

    def score(self, pid: str, col: str) -> float | None:
        return _float(self.papers.get(pid, {}).get(col))

    def status(self, pid: str) -> str | None:
        s = self.papers.get(pid, {}).get("status", "").lower()
        return s or None

    def title(self, pid: str) -> str | None:
        return self.papers.get(pid, {}).get("title") or None


def read_labels(split_root: Path) -> SplitLabels:
    lp = label_paths(split_root)
    out = SplitLabels(root=lp.root)
    for r in _rows(lp.papers_csv):
        if r.get("forum_id"):
            out.papers[r["forum_id"]] = r
    for r in _rows(lp.conference_dir / "codebase_audits_results.csv"):
        if r.get("forum_id"):
            out.audits[r["forum_id"]] = r.get("Audit Result", "").upper()
    for pid, r in out.papers.items():  # fall back to papers.csv's copy of the audit
        if pid not in out.audits and r.get("code_audit", "-") not in ("", "-"):
            out.audits[pid] = r["code_audit"].upper()
    for r in _rows(lp.code_tests_csv):
        if r.get("forum_id"):
            out.code_tests[r["forum_id"]] = r
    if lp.reviews_dir.is_dir():
        out.reviews = {p.stem for p in lp.reviews_dir.glob("*.json")}
    return out
