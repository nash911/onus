"""Phase 7: offset-preserving edits, the five mutations, detection, metrics, the harness (aggregate-only output,
path guards, --check, n<2, runs 1, no code results), baseline, labels, findings and bundle."""

from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from onus.config import get_settings, repo_root
from onus.eval.baseline import run_baseline
from onus.eval.harness import EvalError, guard_paths, parse_plan, plan_types
from onus.eval.labels import export_sheet, key_path, score_sheet
from onus.eval.metrics import cohen_kappa, detect, render_aggregate, wilson
from onus.eval.mutate import ERR_INLINE, apply_edits, mutate, offset_mapper
from onus.ingest.tables import parse_cell
from onus.llm.fake import FakeLLM
from onus.llm.prompts import PromptRegistry
from onus.schemas.document import RefMention, Table
from onus.schemas.result import RISK, CheckResult, Claim, Location, RunResult, Wording
from tests.test_claims import make_doc

W = Wording(strength="plain", scope="specific", markers=[])
ABSTRACT = "We present a ranker. It reaches 85.2% accuracy on the benchmark, as shown later."
RESULTS = ["Table 1 reports 85.2 ± 0.4 accuracy for our method over five seeds.",
           "Our ranker beats the baseline on the benchmark by a clear margin in accuracy.",
           "We report the standard deviation across seeds for every metric."]


def _doc():
    d = make_doc([("Abstract", "abstract", [ABSTRACT]), ("3 Results", "results", RESULTS),
                  ("References", "references", ["[1] A. Smith. Widgets. 2020."])],
                 checklist=[("error_bars", "Yes", "We report standard deviations in Table 1."),
                            ("compute", "No", "We do not report compute.")])
    grid = [[parse_cell(x) for x in ("Method", "Acc (%)")], [parse_cell(x) for x in ("Baseline", "81.0 ± 0.5")],
            [parse_cell(x) for x in ("Ours", "85.2 ± 0.4")]]
    d.tables = [Table(id="Table 1", caption="Table 1: Accuracy (mean ± std over five seeds).", page=1,
                      bbox=(0, 0, 1, 1), header_rows=1, rows=grid, parsed=True)]
    s = d.text.index("Table 1 reports")
    d.mentions = [RefMention(kind="table", label="Table 1", start=s, end=s + 7, resolved_id="Table 1")]
    return d


def _loc(doc, quote: str) -> Location:
    s = doc.text.index(quote)
    return Location(page=1, paragraph=1, sentence=1, start=s, end=s + len(quote))


def _claim(doc, quote, verdict="supported", cid="c1", ctype="result", key=None, evidence=None, checks=None):
    loc = _loc(doc, quote) if quote in doc.text else Location(page=1, paragraph=1, sentence=1, start=0, end=1)
    return Claim(id=cid, type=ctype, quote=quote, location=loc, is_main=True, wording=W, verdict=verdict,  # type: ignore[arg-type]
                 reason="r", risk=RISK[verdict], checklist_key=key, evidence=evidence or [], checks=checks or [])


def _result(doc, claims, pid="p1", runs=1, checklist=None):
    return RunResult(onus_version="0", fingerprint="fp", models={}, mode="batch", runs=runs,
                     paper={"id": pid, "title": "T"}, created_at="2026-01-01T00:00:00Z",
                     code={"available": False, "files": 0}, claims=claims, checklist=checklist or [],
                     summary={"counts": {}, "flagged_share": 0.0})


# ---------------------------------------------------------------- edits


def test_offset_mapper_shifts_and_collapses():
    m = offset_mapper([(10, 15, "ab"), (20, 20, "XYZ")])
    assert m(5) == 5 and m(10) == 10 and m(12) == 12 and m(14) == 12 and m(15) == 12
    assert m(18) == 15 and m(20) == 20 and m(25) == 25 + 3 - 3


def test_apply_edits_keeps_offsets_consistent():
    d = _doc()
    s = d.text.index("85.2%")
    nd, m = apply_edits(d, [(s, s + 4, "97.85")])
    assert nd.text[m(s):m(s) + 5] == "97.85"
    for p in nd.paragraphs:
        assert nd.text[p.start:p.end].strip()
        for a, b in p.sentences:
            assert p.start <= a < b <= p.end
    for it in nd.checklist:
        assert nd.text[it.start:it.end].startswith(("1.", "2."))
    mm = nd.mentions[0]
    assert nd.text[mm.start:mm.end] == "Table 1"
    with pytest.raises(ValueError):
        apply_edits(d, [(0, 5, ""), (3, 8, "")])


def test_inline_spread_regex():
    assert ERR_INLINE.sub("", "12.3 (11.9–12.7) and 0.81 ± 0.02") == "12.3 and 0.81"
    assert ERR_INLINE.sub("", "points (1, 2) and rates (0.001-0.01)") == "points (1, 2) and rates (0.001-0.01)"


# ---------------------------------------------------------------- the five mutations


async def test_abstract_number_mutation_and_detection():
    d = _doc()
    q = "It reaches 85.2% accuracy on the benchmark, as shown later."
    orig = _result(d, [_claim(d, q)])
    mut, md = await mutate(d, orig, "abstract_number", 7)
    assert mut.applied and md is not None and md.paper_id == "p1~abstract_number"
    assert mut.original == "85.2" and mut.replacement == "88.2" and "88.2%" in md.text
    mq = q.replace("85.2", "88.2")
    fail = CheckResult(check="number", outcome="fail", severity="contradicts", detail="x")
    hit = _result(md, [_claim(md, mq, "contradicted", checks=[fail])], pid=md.paper_id)
    miss = _result(md, [_claim(md, mq, "supported")], pid=md.paper_id)
    assert detect(mut, hit, orig).onus and not detect(mut, miss, orig).onus
    assert not detect(mut, _result(md, [], pid=md.paper_id), orig).target_found


async def test_checklist_flip_changes_answer_and_text():
    d = _doc()
    mut, md = await mutate(d, _result(d, []), "checklist_flip", 7)
    assert mut.applied and mut.checklist_key == "compute"
    it = next(x for x in md.checklist if x.key == "compute")
    assert it.answer == "Yes" and "Answer: [Yes]" in md.text[it.start:it.end]


async def test_remove_table_deletes_object_and_dangles_mentions():
    d = _doc()
    q = RESULTS[0]
    orig = _result(d, [_claim(d, q)])
    mut, md = await mutate(d, orig, "remove_table", 7)
    assert mut.applied and not md.tables and mut.target_claims
    assert all(m.resolved_id is None for m in md.mentions if m.label == "Table 1")
    flagged = _result(md, [_claim(md, q, "untraced")], pid=md.paper_id)
    assert detect(mut, flagged, orig).onus


async def test_widen_scope_uses_the_model_and_verifies_wording():
    d = _doc()
    q = RESULTS[1]
    orig = _result(d, [_claim(d, q, ctype="comparison")])
    wide = "Our ranker beats every baseline on all benchmarks by a clear margin in accuracy."
    fake = FakeLLM({"widen_scope": {"rewritten": wide}}, prompts=PromptRegistry())
    mut, md = await mutate(d, orig, "widen_scope", 7, fake)
    assert mut.applied and wide in md.text and q not in md.text
    fake2 = FakeLLM({"widen_scope": {"rewritten": q}}, prompts=PromptRegistry())
    mut2, md2 = await mutate(d, orig, "widen_scope", 7, fake2)
    assert not mut2.applied and md2 is None and "verification" in mut2.reason


async def test_delete_errorbars_removes_spreads_but_keeps_the_checklist():
    d = _doc()
    mut, md = await mutate(d, _result(d, []), "delete_errorbars", 7)
    assert mut.applied
    body = md.text[: md.checklist[0].start]
    assert "±" not in body and "standard deviation" not in body
    assert all(c.spread is None and "±" not in c.raw for row in md.tables[0].rows for c in row)
    assert "±" not in md.tables[0].caption
    assert "standard deviations" in md.text[md.checklist[0].start:]  # checklist justification untouched
    assert next(x for x in md.checklist if x.key == "error_bars").answer == "Yes"


async def test_preconditions_fail_cleanly():
    d = make_doc([("Abstract", "abstract", ["We study widgets."])])
    for t in ("abstract_number", "checklist_flip", "remove_table", "widen_scope", "delete_errorbars"):
        mut, md = await mutate(d, _result(d, []), t, 7, None)
        assert not mut.applied and md is None and mut.reason


# ---------------------------------------------------------------- metrics and table


def test_wilson_and_kappa():
    lo, hi = wilson(8, 10)
    assert 0.44 < lo < 0.5 and 0.94 < hi < 0.97
    assert wilson(0, 0) is None
    assert cohen_kappa([(1, 1), (0, 0), (1, 0), (0, 0)]) == pytest.approx(0.5)


def _metrics(**over):
    m = {"fingerprint": "abc123", "seed": 7, "mutation_plan": "10x5", "runs": [1],
         "labels": {"papers_labelled": 2},
         "distributions": {"papers": 2, "claims": 4, "non_checklist_claims": 3,
                           "verdicts": {"contradicted": 1, "overstated": 0, "untraced": 0, "not_checkable": 0,
                                        "supported": 3},
                           "flagged": {"k": 1, "n": 4, "rate": 0.25, "ci95": (0.05, 0.7), "papers": 2},
                           "main_flagged": {"k": 0, "n": 2, "rate": 0.0, "ci95": (0, 0.66), "papers": 2},
                           "checklist": {"backed": 1, "unbacked": 0, "contradicted": 0, "not_checkable": 0,
                                         "not_applicable": 0}},
         "stability": {"runs": 1, "na": "n/a (1 run)"},
         "code_agreement": {"labelled": 1, "excluded": 1, "na": "n/a (no code-check results yet)"},
         "mutations": {"recall": {t: {"attempted": 0, "applied": 0, "papers": 0, "target_missing": 0,
                                      "onus": None, "baseline": None}
                                  for t in ("abstract_number", "checklist_flip", "remove_table", "widen_scope",
                                            "delete_errorbars", "all")},
                       "collateral": {"onus": None, "baseline": None}, "failed": 0},
         "cost": {"eur": 0.5, "minutes": 3}}
    m["mutations"]["recall"]["abstract_number"] = {"attempted": 1, "applied": 1, "papers": 1, "target_missing": 0,
                                                   "onus": {"k": 1, "n": 1, "rate": 1.0, "ci95": (0.2, 1), "papers": 1},
                                                   "baseline": None}
    m.update(over)
    return m


def test_aggregate_table_has_no_ids_and_marks_single_paper_cells():
    t = render_aggregate(_metrics())
    assert "`abc123`" in t and "n/a (1 run)" in t and "n/a (no code-check results yet)" in t
    import re

    assert re.search(r"\| abstract_number \| 1 / n<2 / n<2 \| n<2 \|", t)
    assert "Flag rate on unmutated papers" in t and "Collateral flag rate" in t
    assert "1/4 = 25% [5%–70%]" in t  # Onus flag rate on the unmutated papers, beside recall
    assert "p1" not in t


# ---------------------------------------------------------------- guards and plan


def test_plan_parsing_and_types():
    assert parse_plan("10x5") == (10, 5) and parse_plan(None) is None
    with pytest.raises(EvalError):
        parse_plan("10by5")
    assert plan_types(5, 7) == ["abstract_number", "checklist_flip", "remove_table", "widen_scope",
                                "delete_errorbars"]
    assert len(plan_types(2, 7)) == 2 and plan_types(2, 7) == plan_types(2, 7)


def test_guard_refuses_out_in_repo_for_external_root_and_writes_into_split_root(tmp_path: Path):
    ext = tmp_path / "heldout/eval"
    ext.mkdir(parents=True)
    with pytest.raises(EvalError, match="outside the repository"):
        guard_paths(ext, tmp_path / "var", repo_root() / "var/eval-x")
    with pytest.raises(EvalError, match="outside the repository"):
        guard_paths(ext, repo_root() / "var", tmp_path / "out")
    with pytest.raises(EvalError, match="inside the split root"):
        guard_paths(ext, tmp_path / "var", ext / "metrics")
    guard_paths(ext, tmp_path / "var", tmp_path / "out")  # fine


# ---------------------------------------------------------------- baseline


async def test_baseline_validates_ids_and_marks_skipped_claims():
    d = _doc()
    r = _result(d, [_claim(d, RESULTS[0], cid="a"), _claim(d, RESULTS[1], cid="b"),
                    _claim(d, "We do not report compute.", cid="k", ctype="checklist", key="compute")])
    fake = FakeLLM({"baseline_support": {"items": [{"claim_id": "C1", "label": "not supported", "reason": "x"},
                                                   {"claim_id": "C9", "label": "supported"},
                                                   {"claim_id": "C3", "label": "supported", "reason": "ok"}]}},
                   prompts=PromptRegistry())
    b = await run_baseline(d, r, fake, fingerprint="fp")
    assert [c.label for c in b.claims] == ["not_supported", "not_judged", "supported"]
    assert "checklist item: compute" in fake.calls[0]["variables"]["claims"]
    assert "[1] A. Smith" not in fake.calls[0]["variables"]["paper"]  # references are dropped


# ---------------------------------------------------------------- end to end on a split root


def _split(tmp_path: Path) -> Path:
    from tests.pdfgen import make_paper, sample_pages

    root = tmp_path / "heldout/eval"
    for pid in ("p1", "p2"):
        make_paper(root / f"papers/{pid}/paper.pdf", sample_pages(), line_numbers=True)
    (root / "labels/conference").mkdir(parents=True)
    with (root / "labels/papers.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["forum_id", "status", "code_exec_tier", "code_audit", "ai_overall_mean", "human_overall_mean",
                    "title"])
        w.writerow(["p1", "accepted", "executable", "PASSED", "4.0", "3.0", "Secret Title One"])
        w.writerow(["p2", "rejected", "no-code", "NO CODEBASE", "2.0", "", "Secret Title Two"])
    with (root / "labels/conference/codebase_audits_results.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Paper", "Audit Result", "forum_id"])
        w.writerow(["x.pdf", "PASSED", "p1"])
        w.writerow(["y.pdf", "NO CODEBASE", "p2"])
    return root


def _fake_all():
    from tests.test_verdict import _fake

    f = _fake()
    f.responses["widen_scope"] = {"rewritten": "We study widgets and show they always improve accuracy by 4.2 points "
                                               "on every benchmark"}
    f.responses["baseline_support"] = {"items": [{"claim_id": "C1", "label": "not_supported", "reason": "r"}]}
    return f


def test_eval_cli_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from onus.cli import app
    from onus.llm.client import set_client

    root = _split(tmp_path)
    monkeypatch.setenv("ONUS_VAR_DIR", str(tmp_path / "runs/var"))
    get_settings.cache_clear()
    set_client(_fake_all())
    runs = tmp_path / "runs/eval-1"
    try:
        r = CliRunner().invoke(app, ["analyze", "--data-root", str(root), "--out", str(runs), "--runs", "1"])
        assert r.exit_code == 0, r.output
        chk = CliRunner().invoke(app, ["eval", "--check", "--split-root", str(root), "--results", str(runs)])
        assert chk.exit_code == 0, chk.output
        assert "papers: 2 with paper.pdf" in chk.output and "results: 2 of 2" in chk.output
        assert "1 usable for code agreement" in chk.output
        bad = CliRunner().invoke(app, ["eval", "--split-root", str(root), "--results", str(runs), "--out",
                                       str(repo_root() / "var/should-not-exist")])
        assert bad.exit_code == 1 and "outside the repository" in bad.output
        args = ["eval", "--split-root", str(root), "--results", str(runs), "--out", str(runs / "metrics"),
                "--mutations", "2x5", "--baseline"]
        res = CliRunner().invoke(app, args)
        assert res.exit_code == 0, res.output
        out = res.output
        assert "fingerprint" in out and "expected cost" in out
        for secret in ("p1", "p2", "Secret Title", "widgets", "Widget"):
            assert secret not in out, secret
        assert "n/a (1 run)" in out and "n/a (no code-check results yet)" in out
        m = json.loads((runs / "metrics/metrics.json").read_text())
        assert m["mutations"] is not None and (runs / "metrics/per_paper.jsonl").is_file()
        assert (runs / "metrics/baseline").is_dir()
        assert not (root / "metrics").exists()
        res2 = CliRunner().invoke(app, args)  # resumable: same numbers
        assert res2.exit_code == 0
        m2 = json.loads((runs / "metrics/metrics.json").read_text())
        assert m2["mutations"]["recall"] == m["mutations"]["recall"]

        # labels export / score, findings, bundle, merge-metrics
        sheet = tmp_path / "runs/labels_todo.csv"
        info = export_sheet(runs, runs / "metrics/baseline", 5, 5, 7, sheet)
        rows = list(csv.DictReader(sheet.open()))
        assert len(rows) == info["items"] and "system" not in rows[0] if rows else True
        key = json.loads(key_path(sheet).read_text())["items"]
        done = tmp_path / "runs/labels_done.csv"
        with done.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["item_id", "label"])
            w.writeheader()
            for row in rows:
                w.writerow(row | {"label": "correct"})
        sc = score_sheet(done, key_path(sheet))
        assert all(v["all"]["precision"] == 1.0 for v in sc["systems"].values()) and len(key) == len(rows)
        fj = tmp_path / "runs/findings.json"
        fr = CliRunner().invoke(app, ["findings", "--runs", str(runs), "--split-roots", str(root), "--out", str(fj)])
        assert fr.exit_code == 0, fr.output
        f = json.loads(fj.read_text())
        assert f["rq2"]["n_accepted"] == 1 and f["rq2"]["n_rejected"] == 1 and "funnel" in f["rq4"]
        bj = tmp_path / "runs/bundle.jsonl.gz"
        br = CliRunner().invoke(app, ["bundle", "--results", str(runs), "--split-root", str(root), "--out", str(bj)])
        assert br.exit_code == 0, br.output
        with gzip.open(bj, "rt") as fh:
            lines = fh.read().splitlines()
        assert len(lines) == 2 and json.loads(lines[0])["display"]["decision"] in ("accepted", "rejected")
        mm = tmp_path / "runs/merged.json"
        mr = CliRunner().invoke(app, ["eval", "merge-metrics", str(runs / "metrics"), "--out", str(mm)])
        assert mr.exit_code == 0, mr.output and "pooled" in json.loads(mm.read_text())
    finally:
        get_settings.cache_clear()


async def test_abstract_number_avoids_values_printed_in_tables():
    d = _doc()
    d.tables[0].rows.append([parse_cell("Other"), parse_cell("88.2")])  # +3 would collide with this cell
    mut, _md = await mutate(d, _result(d, []), "abstract_number", 7)
    assert mut.applied and mut.replacement == "82.2"


async def test_checklist_flip_needs_the_paper_to_really_lack_it():
    d = make_doc([("3 Results", "results", ["Each run took 3 GPU hours on one A100."])],
                 checklist=[("compute", "No", "Not reported.")])
    mut, _md = await mutate(d, _result(d, []), "checklist_flip", 7)
    assert not mut.applied and "really lacks" in mut.reason


async def test_abstract_number_avoids_values_that_round_to_a_cell():
    d = _doc()
    d.tables[0].rows.append([parse_cell("Other"), parse_cell("88.24")])  # 88.2 at the claim's precision
    mut, _ = await mutate(d, _result(d, []), "abstract_number", 7)
    assert mut.applied and mut.replacement == "82.2"


# ---------------------------------------------------------------- harness v2


# One example per form the audit's error_bars wording accepts. The first test fails if an example stops being
# accepted (keep the list in step with rubrics/checklist_map.yaml); the second fails if the mutation leaves any
# accepted form where the audit looks (body text, table cells incl. headers, table and figure captions).
# ("s.d.", "s.e.m." before a space are not accepted by the audit's pattern: a trailing "\\b" after "." fails.)
AUDIT_FORMS = ["std", "stdev", "standard deviations", "standard error", "sem",
               "confidence interval", "95% CI", "CI", "error bars", "interquartile", "IQR",
               "variance across folds", "variance over repetitions", "variance between seeds", "credible intervals",
               "p-values", "t-test", "Wilcoxon", "Mann-Whitney", "bootstrap confidence", "bootstrapped intervals",
               "permutation tests", "ANOVA", "chi-squared", "Bonferroni", "FDR", "Benjamini",
               "significance tests", "statistically significant", "±", "+/-", "p < .05", "p<0.01", "p ≤ .001"]


def test_every_audit_form_is_accepted_by_the_audit():
    from onus.eval.mutate import audit_spread_pattern

    missing = [f for f in AUDIT_FORMS if not audit_spread_pattern().search(f"we report {f} here")]
    assert missing == []


def test_mutation_source_is_the_audit_wording():
    from onus.checks.rubrics import default_checklist_map
    from onus.eval.mutate import audit_spread_pattern

    assert audit_spread_pattern() is default_checklist_map()["error_bars"].pattern


@pytest.mark.parametrize("form", AUDIT_FORMS)
async def test_delete_errorbars_removes_every_form_the_audit_accepts(form):
    from onus.checks.checklist_audit import Auditor
    from onus.checks.context import PaperContext
    from onus.schemas.document import Figure

    results = [f"Our ranker gains 3 points and we report {form} for it.", "Our ranker is fast on the benchmark."]
    d = make_doc([("Abstract", "abstract", ["We present a ranker for widgets."]), ("3 Results", "results", results),
                  ("References", "references", ["[1] A. Smith. Widgets. 2020."])],
                 checklist=[("error_bars", "Yes", f"We report {form}.")])
    grid = [[parse_cell(x) for x in ("Method", f"Acc ({form})")], [parse_cell(x) for x in ("Ours", "85.2")]]
    d.tables = [Table(id="Table 1", caption=f"Table 1: Accuracy. Values with {form}.", page=1, bbox=(0, 0, 1, 1),
                      header_rows=1, rows=grid, parsed=True)]
    d.figures = [Figure(id="Figure 1", caption=f"Figure 1: Accuracy. Shaded: {form}.", page=1)]
    assert Auditor(PaperContext(d)).condition("error_bars")[0]  # backed before
    mut, md = await mutate(d, _result(d, []), "delete_errorbars", 7)
    assert mut.applied, mut.reason
    ok, _, why = Auditor(PaperContext(md)).condition("error_bars")
    assert not ok, f"{form!r} survives the mutation: {why}"


async def test_baseline_sees_claims_in_paper_order_not_by_verdict():
    d = _doc()
    a = _claim(d, RESULTS[0], "supported", "a")
    b = _claim(d, RESULTS[1], "overstated", "b")
    c = _claim(d, RESULTS[2], "untraced", "c")
    seen = []

    def judge(variables: dict, run_index: int) -> dict:
        seen.append(variables["claims"])
        return {"items": []}

    fake = FakeLLM({"baseline_support": judge}, prompts=PromptRegistry())
    r1 = _result(d, [c, b, a])  # Onus order: highest risk first
    r2 = _result(d, [a, b, c])
    await run_baseline(d, r1, fake, fingerprint="fp")
    await run_baseline(d, r2, fake, fingerprint="fp")
    assert seen[0] == seen[1] and seen[0].index(RESULTS[0][:20]) < seen[0].index(RESULTS[2][:20])
