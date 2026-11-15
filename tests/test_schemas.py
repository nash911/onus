from __future__ import annotations

import json
from datetime import UTC, datetime

from onus.schemas.document import Document, Line, Paragraph
from onus.schemas.result import (
    RISK,
    Claim,
    CodeReport,
    Location,
    RunResult,
    Wording,
    export_schema,
    result_json_schema,
)


def loc(**kw) -> Location:
    return Location(**{"page": 7, "paragraph": 3, "sentence": 2, "start": 0, "end": 10, **kw})


def test_location_citation():
    assert loc().cite() == "p. 7, ¶ 3, s. 2"
    assert loc(line_start=322, line_end=322).cite() == "p. 7, ¶ 3, s. 2, line 322"
    assert loc(line_start=322, line_end=324).cite() == "p. 7, ¶ 3, s. 2, lines 322–324"
    assert loc(line_start=322).cite() == "p. 7, ¶ 3, s. 2, line 322"


def sample_result() -> RunResult:
    claim = Claim(
        id="abc123def456", type="result", quote="We reach 81% accuracy.", location=loc(), is_main=True,
        wording=Wording(strength="plain", scope="specific"), verdict="supported", reason="Table 2 shows 0.81.",
        risk=RISK["supported"],
    )
    return RunResult(
        onus_version="0.1.0", fingerprint="0" * 12, models={"extract": "m"}, mode="batch", runs=1,
        paper={"id": "x"}, created_at=datetime(2026, 11, 15, tzinfo=UTC), claims=[claim],
        code=CodeReport(available=False, files=0),
    )


def test_run_result_round_trip():
    r = sample_result()
    again = RunResult.model_validate_json(r.model_dump_json())
    assert again == r and again.schema_version == "1"


def test_document_round_trip():
    d = Document(
        paper_id="p", source="agents4science", pdf_sha256="0" * 64, title="T", pages=1, text="Hello world.",
        lines=[Line(page=1, text="Hello world.", bbox=(0, 0, 1, 1), start=0, end=12)],
        paragraphs=[Paragraph(page=1, index=1, start=0, end=12, sentences=[(0, 12)])],
    )
    assert Document.model_validate_json(d.model_dump_json()) == d
    assert d.lines[0].line_no is None


def test_schema_export(tmp_path):
    p = export_schema(tmp_path / "schema" / "result.schema.json")
    schema = json.loads(p.read_text())
    assert schema["title"] == "RunResult" and schema["$schema"].endswith("2020-12/schema")
    defs = schema["$defs"]
    for name in ("Claim", "Evidence", "CheckResult", "Location", "ChecklistAudit", "LedgerEntry", "CodeReport"):
        assert name in defs
    assert set(defs["Location"]["required"]) >= {"page", "paragraph", "sentence", "start", "end"}
    assert "line_start" not in defs["Location"]["required"]
    assert defs["Claim"]["properties"]["verdict"]["enum"] == [
        "supported", "overstated", "untraced", "contradicted", "not_checkable"
    ]


def test_committed_schema_is_current():
    from onus.config import repo_root

    p = repo_root() / "schema" / "result.schema.json"
    assert p.is_file(), "run `onus schema export`"
    assert json.loads(p.read_text()) == result_json_schema(), "schema/result.schema.json is stale: run `onus schema export`"
