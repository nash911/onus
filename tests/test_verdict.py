"""Phase 6: verdict rules (every B.5 row), adjudication limits, stability, ledger, the pipeline and the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from onus.config import get_settings
from onus.llm.client import LLMCallError
from onus.llm.fake import FakeLLM
from onus.llm.prompts import PromptRegistry
from onus.pipeline import analyze_paper, renumber, summarise
from onus.schemas.result import ChecklistAudit, CheckResult, Claim, Evidence, Location, RunResult, Wording
from onus.verdict.adjudicate import adjudicate
from onus.verdict.ledger import build_ledger
from onus.verdict.report import render_cost_report, render_results_report
from onus.verdict.rules import RuleVerdict, audit_check, checklist_verdict, rule_verdict
from onus.verdict.stability import cluster, majority, merge_runs
from tests.pdfgen import make_paper, sample_pages

LOC = Location(page=1, paragraph=1, sentence=1, start=0, end=10)
W = Wording(strength="plain", scope="specific")


def ev(eid: str, relation: str | None, label: str = "Table 1", kind: str = "table") -> Evidence:
    return Evidence(id=eid, kind=kind, label=label, snippet="...", link_method="llm_selected", relation=relation)  # type: ignore[arg-type]


def ck(check: str, outcome: str, severity: str = "none", detail: str = "Detail one. Detail two. Three.") -> CheckResult:
    return CheckResult(check=check, outcome=outcome, severity=severity, detail=detail)  # type: ignore[arg-type]


LINK_FAIL = ck("link", "fail", detail="No supporting evidence located (none of 12 candidates).")
LINK_PASS = ck("link", "pass", detail="1 supporting")


# ---------------------------------------------------------------- rules: every row


def test_row1_contradicts_beats_everything():
    r = rule_verdict([ev("ev1", "supports")], [LINK_PASS, ck("number", "fail", "contradicts", "Claimed 91.0%; "
                                                                                              "recomputed 85.2.")], [])
    assert (r.verdict, r.row) == ("contradicted", 1) and r.reason == "Claimed 91.0%; recomputed 85.2."


def test_row2_untraced_no_evidence_or_own_dangling():
    r = rule_verdict([ev("ev1", "unrelated")], [LINK_FAIL], [])
    assert (r.verdict, r.row) == ("untraced", 2) and "none of 12 candidates" in r.reason
    r = rule_verdict([ev("ev1", "supports")], [LINK_PASS], ["Table 5"])
    assert r.verdict == "untraced" and "Table 5" in r.reason


def test_row3_overstated_including_pp_caveat_on_a_pass():
    r = rule_verdict([ev("ev1", "supports")], [LINK_PASS, ck("scope", "fail", "overstates", "Limits say X.")], [])
    assert (r.verdict, r.row) == ("overstated", 3)
    r = rule_verdict([ev("ev1", "supports")], [LINK_PASS, ck("number", "pass", "overstates", "Matches pp.")], [])
    assert r.verdict == "overstated"


def test_row4_partial_only_and_nothing_passed():
    r = rule_verdict([ev("ev1", "partial")], [LINK_PASS, ck("number", "inconclusive"), ck("scope", "not_run")], [])
    assert (r.verdict, r.row) == ("not_checkable", 4)
    r = rule_verdict([ev("ev1", "partial")], [LINK_PASS, ck("dominance", "pass", detail="Beats all.")], [])
    assert r.verdict == "supported"  # a passing check confirms partial evidence


def test_row5_supported_with_contradicting_evidence_caveat():
    r = rule_verdict([ev("ev1", "supports"), ev("ev2", "contradicts", "Table 2")],
                     [LINK_PASS, ck("number", "pass", detail="85.2% = 85.2 (Table 1).")], [])
    assert (r.verdict, r.row) == ("supported", 5)
    assert r.reason.startswith("Supported by Table 1, Table 2") or r.reason.startswith("Supported by Table 1.")
    assert "85.2%" in r.reason and r.caveat and "Table 2" in r.caveat


def test_checklist_mapping():
    def audit(status):
        return ChecklistAudit(key="error_bars", title="E", answer="Yes", justification="j", status=status, reason="R.")
    assert checklist_verdict(audit("backed")).verdict == "supported"
    assert checklist_verdict(audit("unbacked")).verdict == "untraced"
    assert checklist_verdict(audit("contradicted")).verdict == "contradicted"
    assert checklist_verdict(audit("not_checkable")).verdict == "not_checkable"
    assert checklist_verdict(audit("not_applicable")) is None
    assert checklist_verdict(None).verdict == "not_checkable"
    c = audit_check(audit("contradicted"))
    assert (c.check, c.outcome, c.severity) == ("checklist", "fail", "contradicts")


# ---------------------------------------------------------------- adjudication


RV = RuleVerdict("contradicted", 1, "Claimed 91%; recomputed 85.2%.")


async def _adj(response, rv=RV, evidence=None):
    fake = FakeLLM({"adjudicate": response}, prompts=PromptRegistry())
    return await adjudicate(quote="Ours reaches 91% accuracy.", section="Abstract", wording=W,
                            evidence=evidence if evidence is not None else [ev("ev1", "supports")],
                            checks=[LINK_PASS, ck("number", "fail", "contradicts")], rule=rv, llm=fake, run_index=0,
                            paper_id="p", metas=[])


async def test_adjudicate_confirm_uses_the_judges_reason():
    v, reason, caveat, rec = await _adj({"decision": "confirm", "reason": "Table 1 gives 85.2, not 91.",
                                         "caveat": "Only one table was parsed."})
    assert v == "contradicted" and reason == "Table 1 gives 85.2, not 91." and caveat and rec["applied"]


async def test_adjudicate_supported_needs_a_valid_evidence_id():
    v, _, _, rec = await _adj({"decision": "supported", "evidence_ids": ["ev1"], "reason": "Row 2 shows 91."})
    assert v == "supported" and rec["evidence_ids"] == ["ev1"]
    v, reason, _, rec = await _adj({"decision": "supported", "evidence_ids": ["ev9"], "reason": "trust me"})
    assert v == "contradicted" and reason == RV.reason and not rec["applied"]
    v, _, _, _ = await _adj({"decision": "supported", "evidence_ids": ["ev1"]}, evidence=[])
    assert v == "contradicted"


async def test_adjudicate_not_checkable_needs_a_parse_problem_and_never_upgrades():
    v, _, _, rec = await _adj({"decision": "not_checkable", "reason": "x"})
    assert v == "contradicted" and "parse problem" in rec["note"]
    v, _, _, _ = await _adj({"decision": "not_checkable", "parse_problem": "columns shifted", "reason": "x"})
    assert v == "not_checkable"
    nc = RuleVerdict("not_checkable", 4, "Only partial.")
    v, _, _, rec = await _adj({"decision": "not_checkable", "parse_problem": "p"}, rv=nc)
    assert v == "not_checkable" and not rec["applied"]
    v, _, _, rec = await _adj({"decision": "contradicted"}, rv=nc)  # unknown decision: no upgrade possible
    assert v == "not_checkable" and not rec["applied"]


async def test_adjudicate_failure_keeps_the_rule_verdict():
    v, reason, _, rec = await _adj(LLMCallError("down"))
    assert v == "contradicted" and reason == RV.reason and rec["decision"] is None


# ---------------------------------------------------------------- stability


def claim(quote: str, verdict: str, cid: str = "c", ctype: str = "result", key: str | None = None,
          evidence: list[Evidence] | None = None) -> Claim:
    from onus.schemas.result import RISK

    return Claim(id=cid, type=ctype, quote=quote, location=LOC, is_main=False, wording=W, verdict=verdict,  # type: ignore[arg-type]
                 reason="r", risk=RISK[verdict], checklist_key=key, evidence=evidence or [])


Q = "Our method improves accuracy by 4.2 points on both benchmarks"


def test_majority_and_tie_goes_to_less_severe():
    assert majority(["overstated", "overstated", "supported"]) == "overstated"
    assert majority(["contradicted", "untraced", "supported"]) == "supported"
    assert majority(["contradicted", "untraced"]) == "untraced"


def test_merge_runs_clusters_majority_and_drops_singletons():
    runs = [[claim(Q, "overstated"), claim("A claim found only once in the whole set of runs", "untraced")],
            [claim(Q + ".", "overstated")],
            [claim("Our method improves the accuracy by 4.2 points on both benchmarks", "supported")]]
    merged, dropped = merge_runs(runs)
    assert dropped == 1 and len(merged) == 1
    m = merged[0]
    assert m.verdict == "overstated" and (m.stability.runs, m.stability.agree) == (3, 2)
    assert m.stability.verdicts == ["overstated", "overstated", "supported"]


def test_checklist_claims_cluster_by_item():
    a = claim("We report standard deviations over 5 seeds.", "supported", ctype="checklist", key="error_bars")
    b = claim("We report standard deviations over 5 seeds.", "supported", ctype="checklist", key="compute")
    cl = cluster([[a, b], [a, b]])
    assert sorted(len(c) for c in cl) == [2, 2] and all(len({x.checklist_key for _, x in c}) == 1 for c in cl)


def test_single_run_has_no_stability():
    merged, dropped = merge_runs([[claim(Q, "supported")]])
    assert dropped == 0 and merged[0].stability is None


def test_renumber_makes_ids_unique_and_follows_references():
    c1 = claim(Q, "supported", evidence=[ev("ev1", "supports"), ev("ev2", "partial")])
    c1 = c1.model_copy(update={"checks": [CheckResult(check="number", outcome="pass", detail="", evidence_ids=["ev2"])],
                               "adjudication": {"evidence_ids": ["ev1"]}})
    c2 = claim("Another", "supported", evidence=[ev("ev1", "supports"),
                                                 ev("ck-limitations-1", "supports", kind="text")])
    out = renumber([c1, c2])
    assert [e.id for e in out[0].evidence] == ["ev1", "ev2"] and [e.id for e in out[1].evidence] == ["ev3",
                                                                                                    "ck-limitations-1"]
    assert out[0].checks[0].evidence_ids == ["ev2"] and out[0].adjudication["evidence_ids"] == ["ev1"]


def test_ledger_and_summary():
    c = claim(Q, "overstated").model_copy(update={"is_main": True, "checks": [
        ck("link", "pass"), CheckResult(check="number", outcome="not_run", detail="no number",
                                        data={"reason": "no number in the claim"})]})
    k = claim("Checklist", "supported", ctype="checklist", key="limitations")
    audits = [ChecklistAudit(key="theory", title="T", answer="NA", justification="", status="not_applicable",
                             reason="Answered NA.")]
    led = {e.check: e for e in build_ledger([c, k], audits, "batch")}
    assert led["link"].ran_on == 1 and led["number"].reasons == {
        "no number in the claim": 1, "checklist claim (checked by the checklist audit)": 1}
    assert led["checklist"].reasons == {"answered No or NA": 1}
    assert led["code_static"].reasons == {"code checks are not part of this version": 2}
    s = summarise([c, k], audits)
    assert s["counts"]["overstated"] == 1 and s["flagged_share"] == 0.5 and s["main_flagged_share"] == 1.0


# ---------------------------------------------------------------- pipeline


CLAIM_ABS = "We study widgets and show they improve accuracy by 4.2 points on two benchmarks"


def _fake(adjudicate_response=None) -> FakeLLM:
    from tests.test_evidence import _selector

    return FakeLLM({
        "extract_claims": {"items": [{"quote": CLAIM_ABS, "type": "result", "is_main": True},
                                     {"quote": "Our method is efficient and simple.", "type": "method"}]},
        "classify_scope": {"scope": "specific"}, "select_evidence": _selector,
        "map_number": {"items": [{"number_id": "N1", "kind": "not_in_tables"}]},
        "map_comparison": {"applicable": False}, "rubric_counts": {"datasets": []},
        "extract_limitations": {"items": []}, "scope_conflict": {"items": []},
        "checklist_item": {"status": "not_checkable", "reason": "n/a"},
        "adjudicate": adjudicate_response or {"decision": "confirm", "reason": "Confirmed by the judge."},
    }, prompts=PromptRegistry(), strict=False)


@pytest.fixture
def paper(tmp_path: Path) -> Path:
    d = tmp_path / "split/papers/p1"
    make_paper(d / "paper.pdf", sample_pages(), line_numbers=True)
    return d


async def test_analyze_paper_three_runs(paper: Path, tmp_path: Path):
    stages: list[str] = []
    r = await analyze_paper(paper, runs=3, var_dir=tmp_path / "var", llm=_fake(),
                            progress=lambda stage, info: stages.append(stage))
    assert r.runs == 3 and r.mode == "batch" and r.paper["id"] == "p1"
    assert stages[0] == "ingest" and stages[-1] == "done" and {"claims", "evidence", "checks", "verdicts"} <= set(stages)
    assert r.claims and all(c.stability is not None and c.stability.runs == 3 for c in r.claims)
    assert [c.risk for c in r.claims] == sorted((c.risk for c in r.claims), reverse=True)
    ids = [e.id for c in r.claims for e in c.evidence if e.id.startswith("ev")]
    assert len(ids) == len(set(ids))
    assert all(c.reason for c in r.claims)
    non_supported = [c for c in r.claims if c.type != "checklist" and c.rule_verdict != "supported"]
    assert all(c.adjudication is not None for c in non_supported)
    assert {e.check for e in r.ledger} >= {"link", "number", "dominance", "strength", "scope", "checklist",
                                           "code_static", "code_rerun"}
    assert r.summary["n_claims"] == len(r.claims) and r.cost["calls"] > 0 and r.timings["wall_s"] >= 0
    for c in r.claims:  # every claim's checks: link + 4 checks, or one checklist check
        assert len(c.checks) == (1 if c.type == "checklist" else 5)
    RunResult.model_validate_json(r.model_dump_json())  # round-trips through the contract


async def test_derived_claims_item_and_live_mode(paper: Path, tmp_path: Path):
    r = await analyze_paper(paper, runs=3, mode="live", var_dir=tmp_path / "var", llm=_fake())
    assert r.runs == 1 and all(c.stability is None for c in r.claims)
    assert any(x.what == "Submitted code" and "live" in x.reason for x in r.not_checked) or not r.code.available
    claims_item = next((a for a in r.checklist if a.key == "claims"), None)
    if claims_item is not None and claims_item.answer == "Yes":
        assert claims_item.status in ("backed", "unbacked")


# ---------------------------------------------------------------- CLI and reports


def test_cli_analyze_resumable_and_report(paper: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from onus.cli import app
    from onus.llm.client import set_client

    monkeypatch.setenv("ONUS_VAR_DIR", str(tmp_path / "var"))
    get_settings.cache_clear()
    fake = _fake()
    set_client(fake)
    out = tmp_path / "var/results/dev"
    try:
        res = CliRunner().invoke(app, ["analyze", "--data-root", str(tmp_path / "split"), "--out", str(out),
                                       "--runs", "2"])
        assert res.exit_code == 0, res.output
        calls = len(fake.calls)
        res2 = CliRunner().invoke(app, ["analyze", "--data-root", str(tmp_path / "split"), "--out", str(out),
                                        "--runs", "2"])
        assert res2.exit_code == 0 and "1 up to date, 0 to analyse" in res2.output and len(fake.calls) == calls
        md = tmp_path / "dev_report.md"
        res3 = CliRunner().invoke(app, ["report", str(out), "--out", str(md)])
        assert res3.exit_code == 0, res3.output
    finally:
        get_settings.cache_clear()
    r = RunResult.model_validate_json((out / "p1.json").read_text())
    assert r.runs == 2
    text = md.read_text()
    assert "| p1 |" in text and "## Ledger" in text


def test_cost_report_prices_cache_hits():
    r = RunResult(onus_version="0", fingerprint="f", models={"extract": "zai-org/GLM-5.3-Flash"}, mode="batch",
                  runs=1, paper={"id": "p1"}, created_at="2026-01-01T00:00:00Z",
                  code={"available": False, "files": 0}, cost={"by_stage": {"claims": 0.01}},
                  timings={"wall_s": 120})
    rows = [{"paper_id": "p1", "role": "extract", "model": "zai-org/GLM-5.3-Flash", "tokens_in": 1_000_000,
             "tokens_out": 0, "cached_in": 0, "eur": 0.25, "cache_hit": False},
            {"paper_id": "p1", "role": "judge", "model": "moonshotai/Kimi-K3.1-Pro", "tokens_in": 0,
             "tokens_out": 100_000, "cached_in": 0, "eur": 0.0, "cache_hit": True},
            {"paper_id": "other", "role": "judge", "model": "x", "eur": 5.0}]
    text = render_cost_report([r], rows)
    assert "| p1 | 0.2500 | 1.0000 |" in text and "€1.250 per paper per run" in text
    assert "over: propose cheaper role assignments" in text and "741 paper-runs" in text


def test_results_report_counts():
    c = claim(Q, "contradicted")
    r = RunResult(onus_version="0", fingerprint="f", models={}, mode="batch", runs=3, paper={"id": "p1"},
                  created_at="2026-01-01T00:00:00Z", code={"available": False, "files": 0}, claims=[c],
                  summary=summarise([c], []), cost={"eur": 0.1}, timings={"wall_s": 60})
    text = render_results_report([r])
    assert "| p1 | 1 | 1 | 0 | 0 | 0 | 0 | 100% |" in text and "Highest-risk" in text
