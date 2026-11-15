"""`RunResult`: the shared contract between pipeline, API and frontend (BUILD_PLAN §5.3).

Change this model first, then run `onus schema export` and regenerate the TypeScript types.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1"

Verdict = Literal["supported", "overstated", "untraced", "contradicted", "not_checkable"]
CheckName = Literal["link", "strength", "number", "dominance", "checklist", "scope", "code_static", "code_rerun"]

RISK: dict[str, int] = {"contradicted": 4, "overstated": 3, "untraced": 2, "not_checkable": 1, "supported": 0}


class Location(BaseModel):
    page: int  # always set: page, paragraph and sentence are the citation every paper gets
    paragraph: int
    sentence: int
    line_start: int | None = None  # only when the PDF has review line numbers
    line_end: int | None = None
    section: str | None = None
    start: int
    end: int

    def cite(self) -> str:
        """Render as "p. 7, ¶ 3, s. 2" or "p. 7, ¶ 3, s. 2, line 322" (or "lines 322–324")."""
        out = f"p. {self.page}, ¶ {self.paragraph}, s. {self.sentence}"
        if self.line_start is not None:
            if self.line_end is not None and self.line_end != self.line_start:
                out += f", lines {self.line_start}–{self.line_end}"
            else:
                out += f", line {self.line_start}"
        return out


class Evidence(BaseModel):
    id: str  # stable within the result, e.g. "ev3"
    kind: Literal["table", "table_cell", "figure", "text", "checklist", "code", "rerun_output"]
    label: str  # "Table 2, row 'Ours', col 'Acc'" / "src/train.py:41"
    snippet: str
    location: Location | None = None
    link_method: Literal["explicit_ref", "retrieval", "llm_selected", "code_search", "sandbox"]
    relation: Literal["supports", "partial", "contradicts", "unrelated"] | None = None


class CheckResult(BaseModel):
    check: CheckName
    outcome: Literal["pass", "fail", "inconclusive", "not_run"]
    severity: Literal["contradicts", "overstates", "none"] = "none"
    detail: str  # human-readable, e.g. "Claimed 23.0%; recomputed (0.81−0.72)/0.72 = 12.5%"
    data: dict = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)


class Wording(BaseModel):
    strength: Literal["hedged", "plain", "strong", "absolute"]
    scope: Literal["specific", "general", "universal"]
    markers: list[str] = Field(default_factory=list)


class Stability(BaseModel):
    runs: int
    agree: int
    verdicts: list[Verdict]


class Claim(BaseModel):
    id: str  # sha1(normalised quote)[:12]
    type: Literal["result", "comparison", "generality", "causal", "method", "availability", "checklist"]
    quote: str
    location: Location
    is_main: bool
    wording: Wording
    evidence: list[Evidence] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    verdict: Verdict
    reason: str
    caveat: str | None = None
    risk: int  # contradicted 4 · overstated 3 · untraced 2 · not_checkable 1 · supported 0
    stability: Stability | None = None


class ChecklistAudit(BaseModel):
    key: str
    title: str
    answer: str
    justification: str
    status: Literal["backed", "unbacked", "contradicted", "not_applicable", "not_checkable"]
    reason: str
    evidence: list[Evidence] = Field(default_factory=list)


class LedgerEntry(BaseModel):
    check: CheckName
    ran_on: int
    not_run_on: int
    reasons: dict[str, int] = Field(default_factory=dict)


class NotChecked(BaseModel):
    what: str
    reason: str
    count: int = 1


class CodeReport(BaseModel):
    available: bool
    files: int
    static: list[dict] = Field(default_factory=list)  # per implementation fact: {fact, status, citations[]}
    reruns: list[dict] = Field(default_factory=list)  # per attempt: {status, commands[], modifications[], ...}
    code_version: str | None = None


class RunResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    onus_version: str
    fingerprint: str
    models: dict[str, str]
    mode: Literal["batch", "live"]
    runs: int
    paper: dict  # {id, source, external_id, title, pdf_sha256, pages}
    created_at: datetime
    claims: list[Claim] = Field(default_factory=list)
    checklist: list[ChecklistAudit] = Field(default_factory=list)
    code: CodeReport
    ledger: list[LedgerEntry] = Field(default_factory=list)
    not_checked: list[NotChecked] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)  # {counts: {verdict: n}, flagged_share, main_flagged_share}
    cost: dict = Field(default_factory=dict)  # {eur, tokens_in, tokens_out, calls}
    timings: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def result_json_schema() -> dict:
    schema = RunResult.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://onus/schema/result.v{SCHEMA_VERSION}.json"
    return schema


def export_schema(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result_json_schema(), indent=2, ensure_ascii=False) + "\n")
    return path
