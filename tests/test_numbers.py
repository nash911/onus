"""`onus export-numbers` (Phase 12): every number copied from the inputs, with CI, n, source and command."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from onus.config import repo_root
from onus.eval.entry import app
from onus.eval.metrics import merge_metrics, rate, wilson
from onus.eval.mutate import TYPES
from onus.eval.numbers import NA, NumbersError, render

runner = CliRunner()


def _split(fp: str = "aaaaaaaaaaaa", runs: int = 3, base: bool = True) -> dict:
    rec = {t: {"attempted": 4, "applied": 3, "papers": 3, "target_missing": 0, "onus": rate(2, 3, 3),
               "baseline": rate(1, 3, 3) if base else None} for t in TYPES}
    rec["checklist_flip"] = {"attempted": 2, "applied": 0, "papers": 0, "target_missing": 0, "onus": rate(0, 0, 0),
                             "baseline": rate(0, 0, 0) if base else None}  # no paper qualified: n = 0
    rec["all"] = {"attempted": 18, "applied": 12, "papers": 6, "target_missing": 0, "onus": rate(8, 12, 6),
                  "baseline": rate(4, 12, 6) if base else None}
    d = {"papers": 6, "claims": 100, "non_checklist_claims": 60, "verdicts": {}, "flagged": rate(12, 100, 6),
         "main_flagged": rate(3, 20, 6), "checklist": {}}
    if base:
        d["baseline"] = {"papers": 6, "labels": {}, "not_supported": rate(30, 100, 6)}
    stab = {"runs": runs, "claims": 90, "papers": 6, "full_agreement": rate(70, 90, 6), "mean_agreement": 0.9123,
            "matrix": {}} if runs > 1 else {"runs": 1, "na": "n/a (1 run)"}
    return {"fingerprint": fp, "harness_fingerprint": "b6018f89f149", "created_at": "t", "seed": 7,
            "mutation_plan": "30x5", "runs": [runs], "labels": {"papers_labelled": 6}, "distributions": d,
            "stability": stab,
            "code_agreement": {"labelled": 4, "excluded": 2, "n": 4, "accuracy": 0.75, "kappa": 0.5,
                               "confusion": {"pred_mismatch_audit_mismatch": 1, "pred_mismatch_audit_passed": 0,
                                             "pred_ok_audit_mismatch": 1, "pred_ok_audit_passed": 2}},
            "mutations": {"recall": rec, "collateral": {"onus": rate(5, 200, 6), "baseline": rate(7, 200, 6)},
                          "failed": 0},
            "cost": {"eur": 3.21, "minutes": 42.0, "estimate_eur": 4.0}}


FINDINGS = {"seed": 7, "bootstrap": 2000, "papers": 12, "splits": {"test_1": 6, "test_2": 6},
            "rq1": {"ai_score": {"n": 12, "rho": 0.31, "ci95": [-0.2, 0.7], "points": []},
                    "human_score": {"n": 9, "rho": -0.1, "ci95": None, "points": []}},
            "rq2": {"n_accepted": 6, "n_rejected": 6, "mean_accepted": 0.1, "mean_rejected": 0.2, "difference": 0.1,
                    "ci95": [0.01, 0.19], "mann_whitney_p": 0.0412, "series": {}},
            "rq3": {"items": [{"key": "error_bars", "yes": 8, "unbacked": 2, "contradicted": 1, "unbacked_share": 0.375},
                              {"key": "theory", "yes": 0, "unbacked": 0, "contradicted": 0, "unbacked_share": None}]},
            "rq4": {"funnel": {"papers": 12, "with_code": 9, "executable": 5, "rerun_attempted": 5, "consistent": 2,
                               "note": ""}}}

LABELS = {"systems": {"onus": {"all": {"correct": 40, "incorrect": 10, "unsure": 5, "precision": 0.8,
                                       "ci95": wilson(40, 50)},
                               "overstated": {"correct": 15, "incorrect": 5, "unsure": 1, "precision": 0.75,
                                              "ci95": wilson(15, 20)}},
                      "baseline": {"all": {"correct": 12, "incorrect": 12, "unsure": 2, "precision": 0.5,
                                           "ci95": wilson(12, 24)}}}}

SRC = {"metrics": "m.json", "findings": "f.json", "labels": None}


def _merged(labels: dict | None = None) -> dict:
    return json.loads(json.dumps(merge_metrics({"test_1": _split(), "test_2": _split()}, labels)))


def _section(text: str, title: str) -> str:
    return text.split(f"## {title}", 1)[1].split("\n## ", 1)[0]


def test_sections_and_how_to_read():
    t = render(_merged(), FINDINGS, LABELS, {**SRC, "labels": "l.json"}, "out.md")
    heads = [ln for ln in t.splitlines() if ln.startswith("## ")]
    assert heads == ["## Inputs", "## 1. Recall on planted errors",
                     "## 2. Flag rate on unmutated papers and collateral flags", "## 3. Precision (blind hand labels)",
                     "## 4. Run-to-run stability", "## 5. Code checks", "## 6. Cost and time",
                     "## 7. Corpus findings (RQ1–RQ4)"]
    assert t.index("**How to read this.**") < t.index("## Inputs")
    assert "onus export-numbers --metrics m.json --findings f.json --labels l.json --out out.md" in t
    # every section names its source file and the command behind it
    for h in heads[1:]:
        assert "Source: `" in _section(t, h[3:]) or "source `" in _section(t, h[3:]), h


def test_recall_pooled_with_n_and_not_applicable():
    t = _section(render(_merged(), FINDINGS, None, SRC, "o.md"), "1. Recall on planted errors")
    # pooled over two splits by merge-metrics: 4/6, CI exactly as stored
    ci = wilson(4, 6)
    assert f"| abstract_number | 4/6 = 66.7% | {ci[0]:.1%} – {ci[1]:.1%} | 6 | 2/6 = 33.3%" in t
    row = next(ln for ln in t.splitlines() if ln.startswith("| checklist_flip"))
    assert row.count(NA) == 2  # n = 0 for both systems
    assert "| **all types** | 16/24 = 66.7%" in t
    assert "onus eval merge-metrics <test_1/metrics> <test_2/metrics> --out m.json" in t
    assert "| test_2 | widen_scope | 4 | 3 | 0 | 3 |" in t


def test_values_are_copied_not_recomputed():
    m = _merged()
    m["pooled"]["recall"]["remove_table"]["onus"].update(rate=0.123, ci95=[0.011, 0.222])  # inconsistent on purpose
    t = render(m, FINDINGS, None, SRC, "o.md")
    assert "4/6 = 12.3% | 1.1% – 22.2%" in t
    assert "†" not in _section(t, "1. Recall on planted errors")


def test_flag_rates_and_collateral_both_systems():
    t = _section(render(_merged(), FINDINGS, None, SRC, "o.md"), "2. Flag rate on unmutated papers and collateral flags")
    assert "| test_1 | Onus | claims flagged (contradicted / overstated / untraced) | 12/100 = 12.0%" in t
    assert "| test_1 | Baseline | claims judged not_supported | 30/100 = 30.0%" in t
    assert "| test_2 | Baseline | 7/200 = 3.5%" in t
    assert "| all splits (pooled) | Onus | 10/400 = 2.5%" in t


def test_no_labels_says_not_measured():
    t = render(_merged(), FINDINGS, None, SRC, "o.md")
    sec = _section(t, "3. Precision (blind hand labels)")
    assert "not measured" in sec and "|" not in sec
    assert "- labels: not given" in t


def test_precision_from_labels_file_and_from_merged_metrics():
    for m, labels in ((_merged(), LABELS), (_merged(LABELS), None)):
        sec = _section(render(m, FINDINGS, labels, {**SRC, "labels": "l.json" if labels else None}, "o.md"),
                       "3. Precision (blind hand labels)")
        ci = wilson(40, 50)
        assert f"| onus | **all** | 40/50 = 80.0% | {ci[0]:.1%} – {ci[1]:.1%} | 50 | 40 | 10 | 5 |" in sec
        assert "| onus | overstated | 15/20 = 75.0%" in sec
        assert "| baseline | **all** | 12/24 = 50.0%" in sec
        assert sec.index("| onus | overstated") < sec.index("| onus | **all**")  # per verdict, then all


def test_missing_ci_gets_marked_wilson_interval():
    m = _merged()
    m["splits"]["test_1"]["distributions"]["flagged"]["ci95"] = None
    t = render(m, FINDINGS, None, SRC, "o.md")
    ci = wilson(12, 100)
    assert f"| 12/100 = 12.0% | {ci[0]:.1%} – {ci[1]:.1%}† |" in t
    assert "computed here as a Wilson 95% interval" in t
    # RQ3 shares carry no interval in findings.json: Wilson over (unbacked + contradicted) / yes, marked
    rq3 = wilson(3, 8)
    assert f"| error_bars | 3/8 = 37.5% | {rq3[0]:.1%} – {rq3[1]:.1%}† | 8 | 2 | 1 |" in t
    assert f"| theory | {NA} |" in t


def test_stability_code_cost_findings():
    m = _merged()
    m["splits"]["test_2"] = _split(runs=1)
    t = render(m, FINDINGS, None, SRC, "o.md")
    st = _section(t, "4. Run-to-run stability")
    assert "| test_1 | 3 | 70/90 = 77.8%" in st and "| 0.91 |" in st and "| test_2 | 1 | n/a (1 run)" in st
    code = _section(t, "5. Code checks")
    assert "| test_1 | 75.0% | 0.50 | 4 | 1/0/1/2 | 4 | 2 |" in code
    assert "outcome counts: not measured" in code and "attempted on 5 papers, consistent with the paper on 2" in code
    assert "| test_1 | 6 | €3.21 | 42.0 | €4.00 |" in _section(t, "6. Cost and time")
    f = _section(t, "7. Corpus findings (RQ1–RQ4)")
    assert "| mean AI reviewer score | 0.31 | -0.20 – 0.70 | 12 |" in f
    assert "| human reviewer score | -0.10 | — | 9 |" in f  # a missing bootstrap CI is not invented
    assert "| 10.0% | 20.0% | 10.0% | 1.0% – 19.0% | 0.0412 | 6 | 6 |" in f
    assert "| 12 | 9 | 5 | 5 | 2 |" in f
    assert "--runs <test_1 results> <test_2 results>" in f


def test_code_outcome_counts_when_present():
    m = _merged()
    m["splits"]["test_1"]["code_checks"] = {"static": {"mismatch": 3, "consistent": 5}, "reruns": {"runs": 2}}
    code = _section(render(m, FINDINGS, None, SRC, "o.md"), "5. Code checks")
    assert "| test_1 | static | consistent | 5 |" in code and "| test_1 | reruns | runs | 2 |" in code


def test_fingerprint_mismatch_is_flagged():
    m = json.loads(json.dumps(merge_metrics({"a": _split("111111111111"), "b": _split("222222222222")}, None)))
    assert "different pipeline fingerprints" in render(m, FINDINGS, None, SRC, "o.md")
    assert "different pipeline fingerprints" not in render(_merged(), FINDINGS, None, SRC, "o.md")


def test_single_split_metrics_accepted_and_no_baseline():
    t = render(_split(base=False), FINDINGS, None, SRC, "o.md", metrics_name="test_1")
    assert "| test_1 |" in _section(t, "Inputs")
    rec = _section(t, "1. Recall on planted errors")
    assert "| abstract_number | 2/3 = 66.7%" in rec and "baseline not run" in rec
    assert "--mutations 30x5" in rec and "--baseline" not in rec


def test_bad_inputs_raise():
    with pytest.raises(NumbersError):
        render({"nothing": 1}, FINDINGS, None, SRC, "o.md")
    with pytest.raises(NumbersError):
        render(_merged(), {"rq1": {}}, None, SRC, "o.md")
    with pytest.raises(NumbersError):
        render(_merged(), FINDINGS, {"precision": 1}, SRC, "o.md")


def _files(tmp_path: Path, labels: bool) -> list[str]:
    (tmp_path / "metrics.json").write_text(json.dumps(_merged()))
    (tmp_path / "findings.json").write_text(json.dumps(FINDINGS))
    args = ["export-numbers", "--metrics", str(tmp_path / "metrics.json"), "--findings",
            str(tmp_path / "findings.json"), "--out", str(tmp_path / "out" / "numbers.md")]
    if labels:
        (tmp_path / "labels.json").write_text(json.dumps(LABELS))
        args += ["--labels", str(tmp_path / "labels.json")]
    return args


@pytest.fixture
def no_llm(monkeypatch):
    from onus.llm import client

    def boom(*a, **k):
        raise AssertionError("export-numbers must not create an LLM client")

    monkeypatch.setattr(client, "get_client", boom)


def test_cli_no_labels(tmp_path, no_llm):
    r = runner.invoke(app, _files(tmp_path, labels=False))
    assert r.exit_code == 0, r.output
    text = (tmp_path / "out" / "numbers.md").read_text()
    assert "not measured" in _section(text, "3. Precision (blind hand labels)")
    assert "8 sections" in " ".join(r.output.split())


def test_cli_with_labels(tmp_path, no_llm):
    r = runner.invoke(app, _files(tmp_path, labels=True))
    assert r.exit_code == 0, r.output
    assert "| onus | **all** | 40/50 = 80.0%" in (tmp_path / "out" / "numbers.md").read_text()


def test_cli_missing_and_bad_files(tmp_path):
    args = _files(tmp_path, labels=False)
    (tmp_path / "findings.json").write_text("[1, 2]")
    r = runner.invoke(app, args)
    assert r.exit_code == 1 and "findings" in r.output
    (tmp_path / "findings.json").unlink()
    r = runner.invoke(app, args)
    assert r.exit_code == 1 and "no file" in r.output
    assert not (tmp_path / "out" / "numbers.md").exists()


def test_console_script_keeps_every_cli_command():
    # cli.py is in the frozen pipeline fingerprint, so the script points at eval/entry.py, which adds to cli's app.
    scripts = tomllib.loads((repo_root() / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["onus"] == "onus.eval.entry:app"
    from onus.cli import app as cli_app

    assert cli_app is app
    names = {c.name or c.callback.__name__ for c in app.registered_commands}
    assert {"version", "freeze", "analyze", "findings", "bundle", "export-numbers"} <= names
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0 and "matches" in r.output, r.output
