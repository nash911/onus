"""Phase 5: regex features, tolerance maths (incl. pp vs %), dominance, strength rules, scope, checklist audit,
the orchestrator, the report and the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from onus.checks.checklist_audit import audit_checklist
from onus.checks.context import ClaimContext, PaperContext
from onus.checks.dominance import ComparisonMap, check_dominance, evaluate
from onus.checks.models import not_run
from onus.checks.numbers import (
    MapNumberOut,
    NumberMap,
    Operand,
    check_mention,
    check_numbers,
    compute,
    extract_numbers,
)
from onus.checks.report import render_checks_report
from onus.checks.rubrics import RubricError, Tolerances, load_strength_rules, load_tolerances
from onus.checks.run import load_checks, run_checks
from onus.checks.scope import check_scope, extract_limitations
from onus.checks.strength import PaperFeatures, check_strength, fired_rules, text_features, verify_labels
from onus.checks.tables import TableView, find_table
from onus.claims.models import ClaimSet, DraftClaim
from onus.config import get_settings
from onus.evidence.models import ClaimEvidence, EvidenceSet
from onus.ingest.tables import parse_cell
from onus.llm.client import LLMCallError
from onus.llm.fake import FakeLLM
from onus.llm.prompts import PromptRegistry
from onus.schemas.document import CodeIndex, Document, Table
from onus.schemas.result import CheckResult, Evidence, LedgerEntry, Location, Wording
from tests.pdfgen import make_paper, sample_pages
from tests.test_claims import make_doc

FP = "test"
LOC = Location(page=1, paragraph=1, sentence=1, start=0, end=10)


def table(tid: str, header: list[str], rows: list[list[str]], caption: str = "") -> Table:
    grid = [[parse_cell(h) for h in header]] + [[parse_cell(c) for c in r] for r in rows]
    return Table(id=tid, caption=caption or f"{tid}: Results.", page=1, bbox=(0, 0, 1, 1), header_rows=1,
                 rows=grid, parsed=True)


T1 = table("Table 1", ["Method", "Acc", "F1", "Params"],
           [["Baseline A", "80.1", "0.70", "10M"], ["Baseline B", "82.4", "0.72", "12M"],
            ["Ours", "85.2 ± 0.4", "0.74", "11M"]])
T2 = table("Table 2", ["Metric", "Ours", "Greedy"],
           [["Area burned (avg)", "45.6", "88.7"], ["Time (avg)", "6.1", "1.0"]])


def draft(quote: str, ctype: str = "result", strength: str = "plain", scope: str = "specific",
          markers: list[str] | None = None, cid: str = "c1") -> DraftClaim:
    return DraftClaim(id=cid, type=ctype, quote=quote, location=LOC, origin="llm",  # type: ignore[arg-type]
                      wording=Wording(strength=strength, scope=scope, markers=markers or []))  # type: ignore[arg-type]


def tev(tid: str, eid: str = "ev1", relation: str = "supports") -> Evidence:
    return Evidence(id=eid, kind="table", label=tid, snippet=tid, link_method="llm_selected", relation=relation)  # type: ignore[arg-type]


def cctx(claim: DraftClaim, tables: list[Table] = (), texts: list[str] = ()) -> ClaimContext:
    linked = [(tev(t.id, f"ev{i}"), TableView(t).render()) for i, t in enumerate(tables, 1)]
    linked += [(Evidence(id=f"tx{i}", kind="text", label=f"p. 1, ¶ {i}", snippet=t, link_method="llm_selected",
                         relation="supports"), t) for i, t in enumerate(texts, 1)]
    return ClaimContext(claim=claim, ce=None, linked=linked, tables=[TableView(t) for t in tables], context=claim.quote)


TOL = Tolerances()


# ---------------------------------------------------------------- tables


def test_table_view_labels_ids_and_resolution():
    v = TableView(T1)
    assert v.col_labels == ["Method", "Acc", "F1", "Params"]
    assert v.row_labels == ["Baseline A", "Baseline B", "Ours"]
    assert v.row("R3").index == 2 and v.row("ours").index == 2 and v.col("C2").index == 1
    assert v.row("Baseline").status == "ambiguous"  # A and B match equally
    assert v.row("Transformer").status == "missing" and v.row("R9").status == "missing"
    assert v.col("F-1").index == 2
    assert "| R3 | Ours | 85.2 ± 0.4 | 0.74 | 11M |" in v.render()
    assert v.has_spread() and v.numeric
    assert find_table([v, TableView(T2)], "table 2").id == "Table 2" and find_table([v], "Tab. 1") is v


# ---------------------------------------------------------------- regex features


def test_extract_numbers_units_and_skips():
    ms = extract_numbers("Ours reaches 85.2% (Table 2.1) and improves by 4.2 points, a 2.3x speedup and 3-fold gain, "
                         "with GPT-4.1 and p < 0.05 on 17 tasks and 0.74 F1.")
    assert [(m.text, m.unit) for m in ms] == [("85.2%", "percent"), ("4.2 points", "pp"), ("2.3x", "fold"),
                                              ("3-fold", "fold"), ("0.74", None)]
    assert ms[0].decimals == 1 and ms[3].decimals == 0
    assert extract_numbers("We use 17 LLMs in 2024.") == []


def test_text_features():
    f = text_features("Results are averaged over 5 random seeds with n = 1,200 questions; we report the standard "
                      "deviation and a paired t-test (p < 0.01). The ablation removes the term.")
    assert f == {"seeds": 5, "sample_size": 1200, "variance": True, "stat_test": True, "ablation": True}
    g = text_features("Our method is significantly better.")
    assert g == {"seeds": None, "sample_size": None, "variance": False, "stat_test": False, "ablation": False}
    assert text_features("The difference is statistically significant.", claim=True)["stat_test"] is False
    assert text_features("The difference is statistically significant.")["stat_test"] is True
    assert text_features("We ran 3 independent runs.")["seeds"] == 3


def test_strength_rule_markers():
    rules = load_strength_rules()
    ids = lambda q, **kw: [r.id for r, _ in fired_rules(cctx(draft(q, **kw)), rules)]
    assert ids("Our method significantly improves accuracy.") == ["significance"]
    assert ids("This is a significant challenge for the field.") == []
    assert ids("We achieve state-of-the-art results and outperform all prior methods.") == ["sota"]
    assert ids("Gains hold consistently across datasets.") == ["consistency"]
    assert ids("The approach is robust to noise.") == ["generality"]
    assert ids("It never fails.", strength="absolute") == ["absolute"]
    assert rules.comparison_hits("Ours is better than BM25 and the best overall.") == ["best", "better than"]


def test_unknown_requirement_is_rejected(tmp_path: Path):
    p = tmp_path / "r.yaml"
    p.write_text("rules:\n  - id: x\n    markers: [foo]\n    requires: [magic]\n")
    with pytest.raises(RubricError):
        load_strength_rules(p)


# ---------------------------------------------------------------- tolerance maths


def test_tolerance_formula_from_rubric():
    tol = load_tolerances()
    assert tol.tol(85.2, 1) == pytest.approx(0.426)  # 0.005 × 85.2 > 0.05
    assert tol.tol(4.2, 1) == pytest.approx(0.05)
    assert tol.tol(23, 0) == pytest.approx(0.5)


def test_compute_kinds():
    assert compute("value", [3.0]) == (3.0, "3")
    assert compute("abs_diff", [85.2, 82.4])[0] == pytest.approx(2.8)
    x, expr = compute("rel_change", [0.81, 0.72])
    assert x == pytest.approx(12.5) and expr == "(0.81 − 0.72) / 0.72 = 12.5%"
    assert compute("ratio", [6.0, 2.0])[0] == 3.0
    assert compute("mean", [1.0, 2.0, 3.0])[0] == 2.0 and compute("max", [1.0, 5.0])[0] == 5.0
    assert compute("abs_diff", [1.0])[0] is None and compute("rel_change", [1.0, 0.0])[0] is None


def _m(text: str):
    return extract_numbers(text)[0]


def _op(row: str, col: str, tid: str = "Table 1") -> Operand:
    return Operand(table_id=tid, row=row, col=col)


VIEWS = [TableView(T1), TableView(T2)]


def test_number_value_pass_and_rounding():
    r = check_mention(_m("reaches 85.2%"), NumberMap(number_id="N1", kind="value", operands=[_op("Ours", "Acc")]),
                      VIEWS, TOL)
    assert r["status"] == "pass"
    r = check_mention(_m("reaches 85%"), NumberMap(number_id="N1", kind="value", operands=[_op("R3", "C2")]), VIEWS,
                      TOL)
    assert r["status"] == "pass"  # d = 0: |85.2 − 85| ≤ 0.5


def test_number_abs_diff_pass_and_fail():
    nm = NumberMap(number_id="N1", kind="abs_diff", operands=[_op("Ours", "Acc"), _op("Baseline B", "Acc")])
    assert check_mention(_m("by 2.8 points"), nm, VIEWS, TOL)["status"] == "pass"
    r = check_mention(_m("by 4.2 points"), nm, VIEWS, TOL)
    assert r["status"] == "fail" and r["expr"] == "85.2 − 82.4 = 2.8"


def test_number_rel_change_and_pp_caveat_both_ways():
    ops = [_op("Ours", "Acc"), _op("Baseline A", "Acc")]  # 85.2 vs 80.1: +5.1 pp, +6.37 % relative
    rel = NumberMap(number_id="N1", kind="rel_change", operands=ops)
    assert check_mention(_m("a 6.4% gain"), rel, VIEWS, TOL)["status"] == "pass"
    r = check_mention(_m("a 5.1% gain"), rel, VIEWS, TOL)
    assert r["status"] == "caveat" and r["caveat"] == "reported as % but matches percentage points"
    diff = NumberMap(number_id="N1", kind="abs_diff", operands=ops)
    r = check_mention(_m("by 6.4 points"), diff, VIEWS, TOL)
    assert r["status"] == "caveat" and "relative change" in r["caveat"]


def test_number_percent_from_fractional_cells():
    r = check_mention(_m("an F1 of 74%"), NumberMap(number_id="N1", kind="value", operands=[_op("Ours", "F1")]),
                      VIEWS, TOL)
    assert r["status"] == "pass" and r["scale"] == 100.0


def test_number_inverse_ratio():
    nm = NumberMap(number_id="N1", kind="ratio", operands=[_op("R1", "Ours", "Table 2"),
                                                           _op("Area burned (avg)", "C3", "Table 2")])
    r = check_mention(_m("burns 1.9x less"), nm, VIEWS, TOL)  # 45.6 / 88.7 = 0.51; inverse 1.95
    assert r["status"] == "pass" and r["note"] == "inverse ratio"


def test_number_missing_ambiguous_and_guard():
    nm = NumberMap(number_id="N1", kind="value", operands=[_op("Transformer", "Acc")])
    assert check_mention(_m("85.2%"), nm, VIEWS, TOL)["status"] == "inconclusive"
    nm = NumberMap(number_id="N1", kind="value", operands=[_op("Baseline", "Acc")])
    assert "ambiguous" in check_mention(_m("85.2%"), nm, VIEWS, TOL)["why"]
    nm = NumberMap(number_id="N1", kind="value", operands=[_op("Ours", "Params")])
    assert "no number" in check_mention(_m("85.2%"), nm, VIEWS, TOL)["why"] or \
        check_mention(_m("85.2%"), nm, VIEWS, TOL)["status"] == "inconclusive"
    # mapped to the wrong cell, but 82.4 is printed in the table: suspect mapping, not a contradiction
    nm = NumberMap(number_id="N1", kind="value", operands=[_op("Ours", "Acc")])
    r = check_mention(_m("reaches 82.4%"), nm, VIEWS, TOL)
    assert r["status"] == "inconclusive" and "mapping suspect" in r["why"]
    assert check_mention(_m("reaches 91.0%"), nm, VIEWS, TOL)["status"] == "fail"
    assert check_mention(_m("on 4.5%"), NumberMap(number_id="N1"), VIEWS, TOL)["status"] == "skipped"


async def test_check_numbers_outcomes():
    claim = draft("Ours reaches 91.0% accuracy, 2.8 points above Baseline B.")
    fake = FakeLLM({"map_number": MapNumberOut(items=[
        NumberMap(number_id="N1", kind="value", operands=[_op("R3", "C2")]),
        NumberMap(number_id="N2", kind="abs_diff", operands=[_op("R3", "C2"), _op("R2", "C2")])]).model_dump()},
        prompts=PromptRegistry())
    r = await check_numbers(cctx(claim, [T1]), fake, run_index=0, paper_id="p", metas=[])
    assert r.outcome == "fail" and r.severity == "contradicts" and r.evidence_ids == ["ev1"]
    assert r.detail.startswith("Claimed 91.0%; recomputed 85.2 (Table 1, row 'Ours', col 'Acc')")
    assert (await check_numbers(cctx(draft("No numbers here at all."), [T1]), fake, run_index=0, paper_id="p",
                                metas=[])).data["reason"] == "no number in the claim"
    assert (await check_numbers(cctx(claim), fake, run_index=0, paper_id="p", metas=[])).data["reason"] == \
        "no linked table"
    bad = FakeLLM({"map_number": LLMCallError("down")})
    assert (await check_numbers(cctx(claim, [T1]), bad, run_index=0, paper_id="p", metas=[])).outcome == "inconclusive"


# ---------------------------------------------------------------- dominance


def test_dominance_rows_pass_and_fail():
    v = TableView(T1)
    ok = evaluate(v, ComparisonMap(table_id="Table 1", ours="Ours", baselines=["Baseline A", "Baseline B"],
                                   metrics=["Acc", "F1"], higher_is_better=[True]))
    assert ok["status"] == "pass" and ok["compared"] == 4
    t = table("Table 1", ["Method", "Acc", "F1"], [["A", "80.0", "0.75"], ["B", "85.2", "0.70"], ["Ours", "85.2", "0.74"]])
    res = evaluate(TableView(t), ComparisonMap(ours="Ours", baselines=["A", "B"], metrics=["Acc", "F1"],
                                               higher_is_better=[True, True]))
    assert res["status"] == "fail"
    assert {(f["baseline"], f["metric"]) for f in res["failures"]} == {("A", "F1"), ("B", "Acc")}  # a tie loses


def test_dominance_columns_and_lower_is_better():
    res = evaluate(TableView(T2), ComparisonMap(systems_in="columns", ours="Ours", baselines=["Greedy"],
                                                metrics=["Area burned (avg)", "Time (avg)"],
                                                higher_is_better=[False, False]))
    assert res["status"] == "fail" and [f["metric"] for f in res["failures"]] == ["Time (avg)"]  # 6.1 vs 1.0 loses
    assert [w["metric"] for w in res["wins"]] == ["Area burned (avg)"]  # 45.6 < 88.7 wins
    assert evaluate(TableView(T2), ComparisonMap(ours="Nope", baselines=["x"], metrics=["y"],
                                                 higher_is_better=[True]))["status"] == "inconclusive"
    assert evaluate(TableView(T1), ComparisonMap(ours="Ours", baselines=["Baseline A"], metrics=["Acc", "F1"],
                                                 higher_is_better=[True, True, False]))["status"] == "inconclusive"


async def test_check_dominance_severity_follows_wording():
    t = table("Table 1", ["Method", "Acc"], [["A", "90.0"], ["Ours", "85.2"]])
    fake = FakeLLM({"map_comparison": {"table_id": "Table 1", "ours": "R2", "baselines": ["R1"], "metrics": ["C2"],
                                       "higher_is_better": [True]}}, prompts=PromptRegistry())
    plain = await check_dominance(cctx(draft("Ours outperforms A.", "comparison"), [t]), fake, run_index=0,
                                  paper_id="p", metas=[])
    assert plain.outcome == "fail" and plain.severity == "overstates" and "'A' 90 vs 'Ours' 85.2 (<)" in plain.detail
    absolute = await check_dominance(cctx(draft("Ours always outperforms A.", "comparison", strength="absolute"), [t]),
                                     fake, run_index=0, paper_id="p", metas=[])
    assert absolute.severity == "contradicts"
    assert (await check_dominance(cctx(draft("Ours reaches 85.2."), [t]), fake, run_index=0, paper_id="p",
                                  metas=[])).data["reason"] == "not a comparison claim"
    na = FakeLLM({"map_comparison": {"applicable": False}})
    assert (await check_dominance(cctx(draft("Ours beats A.", "comparison"), [t]), na, run_index=0, paper_id="p",
                                  metas=[])).outcome == "not_run"


# ---------------------------------------------------------------- strength


class _PF:
    def __init__(self, **kw):
        self.features = {"seeds": None, "sample_size": None, "variance": False, "stat_test": False,
                         "ablation": False} | kw
        self.spread = False


DOM_NR = not_run("dominance", "not a comparison claim")


async def test_strength_hedged_and_no_markers_not_run():
    fake = FakeLLM({})
    r, rub = await check_strength(cctx(draft("This may significantly help.", strength="hedged")), _PF(), DOM_NR, fake,
                                  run_index=0, paper_id="p", metas=[])
    assert r.outcome == "not_run" and r.data["reason"] == "hedged wording" and "seeds" in rub
    r, _ = await check_strength(cctx(draft("We report accuracy.")), _PF(), DOM_NR, fake, run_index=0, paper_id="p",
                                metas=[])
    assert r.data["reason"] == "no strength markers"


async def test_strength_significance_needs_a_test():
    c = cctx(draft("Ours significantly improves accuracy.", strength="strong"), [T1])
    r, rub = await check_strength(c, _PF(), DOM_NR, FakeLLM({}), run_index=0, paper_id="p", metas=[])
    assert r.outcome == "fail" and r.severity == "overstates" and "no statistical test" in r.detail
    r, rub = await check_strength(c, _PF(stat_test=True), DOM_NR, FakeLLM({}), run_index=0, paper_id="p", metas=[])
    assert r.outcome == "pass" and rub["stat_test_source"] == "paper"
    assert rub["variance"] and rub["variance_source"] == "evidence"  # Table 1 has a ± cell


async def test_strength_sota_counts_and_dominance():
    claim = draft("We achieve state-of-the-art accuracy.", "comparison", strength="strong")
    counts = FakeLLM({"rubric_counts": {"datasets": ["Acc"], "baselines": ["Baseline A", "Baseline B", "GPT-9"],
                                        "settings": []}}, prompts=PromptRegistry())
    dom_pass = CheckResult(check="dominance", outcome="pass", detail="")
    r, rub = await check_strength(cctx(claim, [T1]), _PF(), dom_pass, counts, run_index=0, paper_id="p", metas=[])
    assert r.outcome == "pass" and rub["baselines"] == ["Baseline A", "Baseline B"] and rub["unverified_labels"] == 1
    dom_fail = CheckResult(check="dominance", outcome="fail", severity="overstates", detail="")
    r, _ = await check_strength(cctx(claim, [T1]), _PF(), dom_fail, counts, run_index=0, paper_id="p", metas=[])
    assert r.outcome == "fail" and r.severity == "contradicts"
    down = FakeLLM({"rubric_counts": LLMCallError("down")})
    r, rub = await check_strength(cctx(claim, [T1]), _PF(), dom_pass, down, run_index=0, paper_id="p", metas=[])
    assert r.outcome == "inconclusive" and rub["counts_source"] == "failed"


async def test_strength_absolute_needs_settings_and_variance():
    claim = draft("Our planner never fails.", strength="absolute", scope="universal")
    fake = FakeLLM({"rubric_counts": {"datasets": ["Area burned (avg)", "Time (avg)"], "baselines": [], "settings": []}})
    r, _ = await check_strength(cctx(claim, [T2]), _PF(), DOM_NR, fake, run_index=0, paper_id="p", metas=[])
    assert r.outcome == "fail" and "no variance reported and no seed(s)" in r.detail
    r, _ = await check_strength(cctx(claim, [T2]), _PF(seeds=5), DOM_NR, fake, run_index=0, paper_id="p", metas=[])
    assert r.outcome == "pass"


def test_verify_labels_from_tables_and_text():
    c = cctx(draft("x"), [T1], ["We evaluate on SciTab and on the TabFact benchmark."])
    kept, dropped = verify_labels(["ours", "TabFact", "ImageNet", "Baseline A", "baseline a"], c)
    assert kept == ["ours", "TabFact", "Baseline A"] and dropped == 1


# ---------------------------------------------------------------- checklist audit


def _cl_doc(checklist, *, body=None, code=None, limitations=True, tables=()):
    secs = body or [("Abstract", "abstract", ["We study widgets."]),
                    ("3 Experiments", "experiments", ["We train with a learning rate of 0.001 and batch size 32."]),
                    ("4 Results", "results", ["Accuracy is 85.2 on the test set."]),
                    ("5 Limitations", "limitations", ["We only test English data in this work."])]
    d = make_doc(secs, checklist=checklist)
    lim = [(s.start, s.end) for s in d.sections if s.role == "limitations"] if limitations else []
    return d.model_copy(update={"code": code or CodeIndex(), "limitations_spans": lim, "tables": list(tables)})


async def _audit(doc: Document, fake=None, stats=None):
    from collections import defaultdict

    audits, derived = await audit_checklist(PaperContext(doc), fake or FakeLLM({}), run_index=0, metas=[],
                                            stats=stats if stats is not None else defaultdict(int))
    return {a.key: a for a in audits}, derived


async def test_checklist_deterministic_rules():
    doc = _cl_doc([("claims", "Yes", "Abstract."), ("error_bars", "Yes", "We report std."),
                   ("open_access", "Yes", "Code is released."), ("limitations", "Yes", "See Section 5."),
                   ("compute", "Yes", "Each run takes 2 hours on one CPU."),
                   ("experimental_details", "Yes", "See Section 3."), ("reproducibility", "Yes", "Yes."),
                   ("theory", "NA", "No theory."), ("broader_impacts", "No", "None.")])
    a, derived = await _audit(doc)
    assert a["claims"].status == "not_checkable" and derived == ["claims"]
    assert a["error_bars"].status == "unbacked"
    assert a["open_access"].status == "contradicted" and "no source files" in a["open_access"].reason
    assert a["limitations"].status == "backed" and a["limitations"].evidence
    assert a["compute"].status == "backed" and a["compute"].evidence[0].kind == "checklist"  # from the justification
    assert a["experimental_details"].status == "backed" and "learning rate" in a["experimental_details"].reason
    assert a["reproducibility"].status == "backed"
    assert a["theory"].status == "not_applicable" and a["broader_impacts"].status == "not_applicable"


async def test_checklist_backed_by_code_repo_spread_and_theorem():
    code = CodeIndex(present=True, files=[{"path": "train.py", "ext": ".py", "lines": 10, "bytes": 100,
                                           "is_source": True}])
    body = [("2 Method", "method", ["Theorem 1. The loss is convex. Proof. Trivial."]),
            ("4 Results", "results", ["Accuracy is 85.2 on the test set."])]
    doc = _cl_doc([("error_bars", "Yes", "Table 1."), ("open_access", "Yes", "Supplement."),
                   ("theory", "Yes", "Section 2."), ("limitations", "Yes", "None stated.")],
                  body=body, code=code, limitations=False, tables=[T1])
    a, _ = await _audit(doc)
    assert a["error_bars"].status == "backed" and "Table 1 reports spreads" in a["error_bars"].reason
    assert a["open_access"].status == "backed" and a["open_access"].evidence[0].kind == "code"
    assert a["theory"].status == "backed"
    assert a["limitations"].status == "unbacked"
    url_doc = _cl_doc([("open_access", "Yes", "Online.")],
                      code=CodeIndex(repo_urls_in_paper=["https://github.com/x/y"]))
    a, _ = await _audit(url_doc)
    assert a["open_access"].status == "backed" and "github.com/x/y" in a["open_access"].reason


async def test_checklist_item_by_model_verified_and_not():
    from collections import defaultdict

    body = [("6 Ethics", "discussion", ["Our study uses only public data and involves no human participants at all."])]
    doc = _cl_doc([("ethics", "Yes", "We follow the code of ethics.")], body=body)
    good = FakeLLM({"checklist_item": {"status": "backed", "passage_id": "P1",
                                       "quote": "uses only public data and involves no human participants",
                                       "reason": "the paper states its data is public"}}, prompts=PromptRegistry())
    a, _ = await _audit(doc, good)
    assert a["ethics"].status == "backed" and "public data" in a["ethics"].reason and a["ethics"].evidence
    stats = defaultdict(int)
    bad = FakeLLM({"checklist_item": {"status": "backed", "passage_id": "P1", "quote": "we obtained IRB approval "
                                      "from the university board", "reason": "x"}})
    a, _ = await _audit(doc, bad, stats)
    assert a["ethics"].status == "not_checkable" and stats["checklist_unverified_quotes"] == 1
    wrong_id = FakeLLM({"checklist_item": {"status": "backed", "passage_id": "P7", "quote": "x"}})
    a, _ = await _audit(doc, wrong_id, stats)
    assert a["ethics"].status == "not_checkable" and stats["checklist_invalid_ids"] == 1
    unb = FakeLLM({"checklist_item": {"status": "unbacked", "reason": "no ethics discussion"}})
    a, _ = await _audit(doc, unb)
    assert a["ethics"].status == "unbacked"


# ---------------------------------------------------------------- scope


async def test_scope_extract_and_conflict():
    from collections import defaultdict

    doc = _cl_doc([("limitations", "Yes", "We only test English.")])
    pc = PaperContext(doc)
    stats = defaultdict(int)
    fake = FakeLLM({"extract_limitations": {"items": [{"quote": "We only test English data in this work."},
                                                      {"quote": "We never tested anything on Mars at all."}]},
                    "scope_conflict": {"items": [{"claim_id": "C1", "conflict": True, "limitation_id": "L1",
                                                  "explanation": "The claim covers every language"},
                                                 {"claim_id": "C2", "conflict": False}]}},
                   prompts=PromptRegistry())
    lims, why = await extract_limitations(pc, fake, run_index=0, metas=[], stats=stats)
    assert why is None and [x.quote for x in lims] == ["We only test English data in this work."]
    assert stats["limitations_unverified"] == 1
    c1 = cctx(draft("Our parser works for any language.", scope="universal", cid="a"))
    c2 = cctx(draft("Our parser is substantially faster.", strength="strong", cid="b"))
    c3 = cctx(draft("We parse English sentences.", cid="c"))
    c4 = cctx(draft("It may work everywhere.", strength="hedged", scope="universal", cid="d"))
    out = await check_scope([c1, c2, c3, c4], lims, None, fake, run_index=0, paper_id="p", metas=[], stats=stats)
    assert out["a"].outcome == "fail" and out["a"].severity == "overstates"
    assert "We only test English data in this work." in out["a"].detail and "p. " in out["a"].detail
    assert out["b"].outcome == "pass" and out["c"].outcome == "not_run" and out["d"].outcome == "not_run"
    none = await check_scope([c1], [], "no limitations stated in the paper", fake, run_index=0, paper_id="p", metas=[],
                             stats=stats)
    assert none["a"].data["reason"] == "no limitations stated in the paper"
    badid = FakeLLM({"scope_conflict": {"items": [{"claim_id": "C1", "conflict": True, "limitation_id": "L9"}]}})
    out = await check_scope([c1], lims, None, badid, run_index=0, paper_id="p", metas=[], stats=stats)
    assert out["a"].outcome == "inconclusive"


# ---------------------------------------------------------------- orchestrator, report, CLI


async def test_run_checks_end_to_end_and_ledger():
    doc = _cl_doc([("limitations", "Yes", "Only English."), ("compute", "No", "Not reported.")], tables=[T1])
    lo = next(p for p in doc.paragraphs if "Accuracy is 85.2" in doc.text[p.start:p.end])
    loc = Location(page=lo.page, paragraph=lo.index, sentence=1, start=lo.start, end=lo.end)
    claim = DraftClaim(id="c1", type="comparison", quote="Accuracy is 85.2 on the test set.", location=loc,
                       origin="llm", wording=Wording(strength="strong", scope="general", markers=["superior"]))
    ck = DraftClaim(id="k1", type="checklist", quote="Only English.", location=loc, origin="checklist",
                    wording=Wording(strength="plain", scope="specific"), checklist_key="limitations")
    cs = ClaimSet(paper_id="p1", fingerprint=FP, claims=[claim, ck])
    es = EvidenceSet(paper_id="p1", fingerprint=FP, ledger=LedgerEntry(check="link", ran_on=1, not_run_on=1),
                     claims=[ClaimEvidence(claim_id="c1", evidence=[tev("Table 1")],
                                           link=CheckResult(check="link", outcome="pass", detail=""))])
    fake = FakeLLM({
        "map_number": {"items": [{"number_id": "N1", "kind": "value",
                                  "operands": [{"table_id": "Table 1", "row": "R3", "col": "C2"}]}]},
        "map_comparison": {"table_id": "Table 1", "ours": "R3", "baselines": ["R1", "R2"], "metrics": ["C2"],
                           "higher_is_better": [True]},
        "extract_limitations": {"items": [{"quote": "We only test English data in this work."}]},
        "scope_conflict": {"items": [{"claim_id": "C1", "conflict": False}]},
    }, prompts=PromptRegistry())
    ks = await run_checks(doc, cs, es, fake, fingerprint=FP)
    cc = ks.for_claim("c1")
    assert [r.check for r in cc.checks] == ["number", "dominance", "strength", "scope"]
    assert cc.get("number").outcome == "pass" and cc.get("dominance").outcome == "pass"
    assert cc.get("scope").outcome == "pass" and cc.get("strength").outcome == "not_run"
    assert ks.for_claim("k1") is None and len(ks.limitations) == 1
    led = {e.check: e for e in ks.ledger}
    assert led["number"].ran_on == 1 and led["number"].reasons == {"checklist claim (checked by the checklist audit)": 1}
    assert led["checklist"].reasons == {"answered No or NA": 1} and led["checklist"].ran_on == 1
    assert {a.key: a.status for a in ks.checklist} == {"limitations": "backed", "compute": "not_applicable"}
    text = render_checks_report([(doc, cs, es, ks)])
    assert "## Every fail" in text and "| number | 1 | 0 | 0 | 0 |" in text


def test_cli_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from onus.cli import app
    from onus.llm.client import set_client
    from tests.test_evidence import CLAIM_ABS, _selector

    paper = tmp_path / "split/papers/p1"
    make_paper(paper / "paper.pdf", sample_pages(), line_numbers=False)
    monkeypatch.setenv("ONUS_VAR_DIR", str(tmp_path / "var"))
    get_settings.cache_clear()
    set_client(FakeLLM({"extract_claims": {"items": [{"quote": CLAIM_ABS, "type": "result"}]},
                        "classify_scope": {"scope": "specific"}, "select_evidence": _selector,
                        "map_number": {"items": [{"number_id": "N1", "kind": "not_in_tables"}]},
                        "map_comparison": {"applicable": False}, "rubric_counts": {"datasets": []},
                        "extract_limitations": {"items": []}, "scope_conflict": {"items": []},
                        "checklist_item": {"status": "not_checkable", "reason": "n/a"}},
                       prompts=PromptRegistry(), strict=False))
    out = tmp_path / "checks.md"
    try:
        res = CliRunner().invoke(app, ["checks", str(tmp_path / "split"), "--report", str(out)])
    finally:
        get_settings.cache_clear()
    assert res.exit_code == 0, res.output
    ks = load_checks(tmp_path / "var/checks/p1.r0.json")
    assert ks.claims and all(len(c.checks) == 4 for c in ks.claims)
    assert (tmp_path / "var/evidence/p1.r0.json").is_file()
    assert "# Checks on dev" in out.read_text()


_ = PaperFeatures  # imported for the public surface; exercised via run_checks


# ---------------------------------------------------------------- dev tuning regressions


def test_sota_and_across_markers_are_narrow():
    rules = load_strength_rules()
    ids = lambda q: [r.id for r, _ in fired_rules(cctx(draft(q)), rules)]
    assert ids("We evaluate 10 state-of-the-art LLMs on two tasks.") == []
    assert ids("Our model sets a new state-of-the-art on SciTab.") == ["sota"]
    assert rules.comparison_hits("We evaluate 10 state-of-theart LLMs.") == []
    assert ids("Reasoning must be balanced across participants.") == []
    assert ids("Gains hold across five configurations.") == ["consistency"]


async def test_strength_skips_method_and_availability_claims():
    for t in ("method", "availability"):
        r, rub = await check_strength(cctx(draft("All code is included in the zip file.", t, strength="absolute")),
                                      _PF(), DOM_NR, FakeLLM({}), run_index=0, paper_id="p", metas=[])
        assert r.outcome == "not_run" and "rubric covers results" in r.data["reason"] and "seeds" in rub


async def test_dominance_ties_only_overstate():
    t = table("Table 1", ["Method", "Acc"], [["A", "85.2"], ["Ours", "85.2"]])
    fake = FakeLLM({"map_comparison": {"table_id": "Table 1", "ours": "R2", "baselines": ["R1"], "metrics": ["C2"],
                                       "higher_is_better": [True]}})
    r = await check_dominance(cctx(draft("Ours always beats A.", "comparison", strength="absolute"), [t]), fake,
                              run_index=0, paper_id="p", metas=[])
    assert r.outcome == "fail" and r.severity == "overstates" and "(=)" in r.detail


async def test_error_bars_backed_by_significance_tests():
    body = [("4 Results", "results", ["We report permutation p-values with BH-FDR control for every candidate."])]
    a, _ = await _audit(_cl_doc([("error_bars", "Yes", "p-values.")], body=body))
    assert a["error_bars"].status == "backed"
    body = [("3 Setup", "method", ["The wind factor was set to 0.8 for all simulations we ran here."])]
    a, _ = await _audit(_cl_doc([("experimental_details", "Yes", "Appendix.")], body=body))
    assert a["experimental_details"].status == "backed"


def test_scope_explanation_hides_batch_ids():
    from onus.checks.scope import _scrub_ids

    assert _scrub_ids("C3 infers X, but L4 states Y (L2).") == "the claim infers X, but the limitation states Y."
    assert _scrub_ids("Limitation L1 narrows claim C2.") == "the limitation narrows the claim."


async def test_dominance_and_strength_gates():
    fake = FakeLLM({})
    r = await check_dominance(cctx(draft("We identify the best models per domain.", "method"), [T1]), fake,
                              run_index=0, paper_id="p", metas=[])
    assert r.outcome == "not_run" and "describes what was done" in r.data["reason"]
    c = draft("We ran experiments across five configurations.")
    c = c.model_copy(update={"origin": "checklist_number"})
    r, _ = await check_strength(cctx(c), _PF(), DOM_NR, fake, run_index=0, paper_id="p", metas=[])
    assert r.data["reason"].startswith("checklist justification")


async def test_check_numbers_falls_back_to_unlinked_tables():
    from onus.checks.context import fallback_tables

    claim = draft("Ours reaches 85.2 accuracy, the best result.")
    fake = FakeLLM({"map_number": [
        {"items": [{"number_id": "N1", "kind": "not_in_tables"}]},  # the linked Table 2 does not hold it
        {"items": [{"number_id": "N1", "kind": "value", "operands": [{"table_id": "Table 1", "row": "R3",
                                                                      "col": "C2"}]}]}]},
        prompts=PromptRegistry())
    ctx = cctx(claim, [T2])
    ctx.fallback_tables = [TableView(T1)]
    r = await check_numbers(ctx, fake, run_index=0, paper_id="p", metas=[])
    assert r.outcome == "pass" and r.data["fallback_tables"] == ["Table 1"] and r.evidence_ids == []
    assert len(fake.calls) == 2
    # no linked table at all: the fallback alone runs the check
    fake2 = FakeLLM({"map_number": {"items": [{"number_id": "N1", "kind": "value",
                                               "operands": [{"table_id": "Table 1", "row": "R3", "col": "C2"}]}]}},
                    prompts=PromptRegistry())
    ctx2 = cctx(claim)
    ctx2.fallback_tables = [TableView(T1)]
    assert (await check_numbers(ctx2, fake2, run_index=0, paper_id="p", metas=[])).outcome == "pass"
    # ranking ignores numbers and needs word overlap with the caption or labels
    doc = Document(paper_id="p", source="t", pdf_sha256="0" * 64, title="t", pages=1, text="x", lines=[],
                   paragraphs=[], sections=[], tables=[T1, T2], figures=[], mentions=[], checklist=[],
                   ai_checklist=[], limitations_spans=[], code=CodeIndex(present=False, root=None, files=[],
                                                                          languages={}, entry_candidates=[],
                                                                          dependency_files=[],
                                                                          repo_urls_in_paper=[]))
    pc = PaperContext(doc)
    assert [v.id for v in fallback_tables(pc, "Ours beats Baseline B on Acc and F1 (99.9)", set())] == ["Table 1"]
    assert fallback_tables(pc, "Ours beats Baseline B on Acc and F1", {"Table 1"}) == []
