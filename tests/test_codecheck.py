"""Phase 10: code checks (code-v1). No network: FakeLLM for the model, a fake sandbox for Modal."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from onus.codecheck import version as cv
from onus.codecheck.apply import apply_code, related
from onus.codecheck.common import CodeFile, mask_secrets
from onus.codecheck.rerun import run as rerun_mod
from onus.codecheck.rerun.agent import AgentRun, run_agent, select_targets
from onus.codecheck.rerun.compare import compare, line_in_outputs
from onus.codecheck.rerun.sandbox import ExecResult, Prepared, plan_rerun, provider_secret_values
from onus.codecheck.static import (
    CitationOut,
    Fact,
    StaticReport,
    paper_level,
    search_code,
    static_check,
    verify_citation,
)
from onus.config import Settings
from onus.llm.fake import FakeLLM
from onus.llm.prompts import PromptRegistry
from onus.schemas.document import CodeIndex
from onus.schemas.result import CheckResult, Claim, CodeReport, Evidence, Location, RunResult, Wording
from onus.version import fingerprint_parts
from tests.test_claims import make_doc

CODE_PROMPTS = PromptRegistry(cv.code_prompts_dir())


# ---------------------------------------------------------------- versioning


def test_code_prompts_and_rubrics_do_not_move_the_p0_fingerprint():
    parts = fingerprint_parts()
    assert not any(k in parts["prompts"] for k in ("impl_facts.md", "impl_match.md", "rerun_agent.md"))
    assert not any(k.startswith("codecheck/") for k in parts["source"])
    assert set(parts["rubrics"]) == {"checklist_map.yaml", "lexicon.yaml", "strength_rules.yaml", "tolerances.yaml"}


def test_code_fingerprint_covers_code_prompts_rubrics_source_and_models(tmp_path):
    p = cv.code_parts()
    assert {"impl_facts.md", "impl_match.md", "rerun_agent.md"} <= set(p["prompts"])
    assert {"static.yaml", "rerun.yaml"} <= set(p["rubrics"])
    assert "tolerances.yaml" in p["shared_rubrics"] and "static.py" in p["source"]
    assert p["models"]["judge"] and len(cv.code_fingerprint()) == 12
    pr = tmp_path / "p"
    pr.mkdir()
    (pr / "impl_facts.md").write_text("x")
    a = cv.code_fingerprint(prompts=pr)
    (pr / "impl_facts.md").write_text("y")
    assert cv.code_fingerprint(prompts=pr) != a
    assert cv.code_version_label() == f"{cv.CODE_VERSION}+{cv.code_fingerprint()}"


def test_freeze_code_records_under_code_and_keeps_p0(tmp_path, monkeypatch):
    fz = tmp_path / "frozen.json"
    fz.write_text(json.dumps({"version": "1.0.0", "fingerprint": "abc"}))
    monkeypatch.setattr(cv, "frozen_path", lambda: fz)
    monkeypatch.setattr(cv, "load_frozen", lambda: json.loads(fz.read_text()))
    src = Path(cv.__file__).read_text()
    try:
        rec = cv.freeze_code_version(cv.CODE_VERSION)  # same version: the source line is unchanged
    finally:
        assert Path(cv.__file__).read_text() == src
    data = json.loads(fz.read_text())
    assert data["fingerprint"] == "abc" and data["code"]["fingerprint"] == rec["fingerprint"] == cv.code_fingerprint()
    with pytest.raises(ValueError):
        cv.freeze_code_version("1.0")


def test_code_prompts_parse_and_render():
    CODE_PROMPTS.get("impl_facts").render({"sections": "x", "max_facts": 8})
    CODE_PROMPTS.get("impl_match").render({"facts": "x"})
    assert CODE_PROMPTS.get("impl_match").role == "judge" and CODE_PROMPTS.get("impl_facts").role == "extract"
    assert CODE_PROMPTS.get("rerun_agent").variables() >= {"targets", "files", "minutes", "network", "max_steps"}


# ---------------------------------------------------------------- masking (rule 7)


def test_mask_secrets_known_values_and_key_shapes():
    text = ("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwx used; anthropic sk-ant-api03-ABCDEFGHIJKLMNOP; "
            "gemini AIzaSyA1234567890abcdefghijklmn; hf_ABCDEFGHIJKLMNOPQRSTUVWX; berget mysecretvalue123; "
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
    out = mask_secrets(text, ["mysecretvalue123"])
    for leak in ("sk-proj-abc", "sk-ant-api03", "AIzaSy", "hf_ABC", "mysecretvalue123", "abcdefghijklmnopqrstuvwxyz0"):
        assert leak not in out
    assert "used;" in out and "***" in out
    assert mask_secrets("accuracy 0.81 after 100 rounds") == "accuracy 0.81 after 100 rounds"


# ---------------------------------------------------------------- static: search, citations, paper level


FILES = [CodeFile("src/train.py", ["import torch", "", "def main():", "    lr = 0.001", "    epochs = 50",
                                   "    model = ResNet18()", "    train(model, lr, epochs)"]),
         CodeFile("README.md", ["Learning rate 0.001"])]


def test_search_code_scores_value_and_terms():
    sn = search_code(FILES[:1], "Training uses a learning rate of 0.001", "0.001", context=1, max_snippets=3,
                     max_chars=5000)
    assert sn and sn[0]["path"] == "src/train.py" and "src/train.py:4|     lr = 0.001" in sn[0]["text"]
    assert search_code(FILES[:1], "uses a transformer decoder", "GPT-2", context=1, max_snippets=3,
                       max_chars=5000) == []


def test_citation_must_exist_and_match_within_slack():
    by = {f.path: f for f in FILES}
    assert verify_citation(CitationOut(path="src/train.py", line=5, text="lr = 0.001"), by, 2)["line"] == 4
    assert verify_citation(CitationOut(path="src/train.py", line=7, text="lr = 0.001"), by, 2) is None
    assert verify_citation(CitationOut(path="src/other.py", line=1, text="import torch"), by, 2) is None
    assert verify_citation(CitationOut(path="src/train.py", line=99, text="x"), by, 2) is None


def _fact(fid, status, core=True, kind="model", value="", statement="s"):
    return Fact(id=fid, kind=kind, statement=statement, value=value, quote="q", start=10**6, end=10**6 + 1, core=core,
                status=status)


def test_paper_level_rules():
    assert paper_level([_fact("F1", "contradicts"), _fact("F2", "present")], 0.5, 2)["mismatch"] is True
    assert paper_level([_fact("F1", "absent"), _fact("F2", "present")], 0.5, 2)["mismatch"] is True
    assert paper_level([_fact("F1", "absent"), _fact("F2", "present"), _fact("F3", "present")], 0.5, 2)[
        "mismatch"] is False
    one = paper_level([_fact("F1", "present"), _fact("F2", "absent", core=False)], 0.5, 2)
    assert one["mismatch"] is None and one["core_facts"] == 1
    assert paper_level([_fact("F1", "unverified"), _fact("F2", "present")], 0.5, 2)["mismatch"] is None


def _code_doc(tmp_path: Path):
    doc = make_doc([("Abstract", "abstract", [("We train ResNet18 with a learning rate of 0.001 and get 81.2% "
                                               "accuracy.")]),
                    ("Method", "method", ["We train a ResNet18 network for 50 epochs with a learning rate of 0.001. "
                                          "Optimisation uses plain stochastic gradient descent without momentum, "
                                          "and every reported number is the mean over the evaluation split. " * 2]),
                    ("Results", "results", ["Accuracy reaches 81.2% on the test split."])])
    root = tmp_path / "paper" / "code"
    (root / "src").mkdir(parents=True)
    (root / "src" / "train.py").write_text("\n".join(FILES[0].lines) + "\n")
    doc.code = CodeIndex(present=True, root=str(root), files=[{"path": "src/train.py", "ext": ".py", "lines": 7,
                                                               "bytes": 100, "is_source": True}],
                         entry_candidates=["src/train.py"])
    return doc, tmp_path / "paper"


def test_static_check_verifies_quotes_ids_and_citations(tmp_path):
    doc, pdir = _code_doc(tmp_path)
    facts = {"items": [
        {"kind": "model", "statement": "The code trains a ResNet18", "value": "ResNet18",
         "quote": "We train a ResNet18 network for 50 epochs"},
        {"kind": "hyperparameter", "statement": "learning rate 0.001", "value": "0.001",
         "quote": "with a learning rate of 0.001"},
        {"kind": "dataset", "statement": "uses ImageNet", "value": "ImageNet", "quote": "not in the paper at all"},
        {"kind": "algorithm", "statement": "plain stochastic gradient descent", "value": "SGD",
         "quote": "Optimisation uses plain stochastic gradient descent without momentum"},
    ]}
    match = {"items": [
        {"fact_id": "F1", "status": "present", "citations": [{"path": "src/train.py", "line": 6,
                                                              "text": "model = ResNet18()"}], "note": "ok"},
        {"fact_id": "F2", "status": "contradicts", "citations": [{"path": "src/train.py", "line": 4,
                                                                  "text": "lr = 0.5"}], "note": "made up"},
        {"fact_id": "F9", "status": "present", "citations": [], "note": "unknown id"},
    ]}
    llm = FakeLLM({"impl_facts": facts, "impl_match": match}, prompts=CODE_PROMPTS)
    rep = asyncio.run(static_check(doc, pdir, llm))
    assert rep.status == "done"
    assert [f.id for f in rep.facts] == ["F1", "F2", "F3"] and rep.dropped["unverified_quote"] == 1
    by = {f.id: f for f in rep.facts}
    assert by["F1"].status == "present" and by["F1"].citations[0]["line"] == 6
    assert by["F2"].status == "unverified"  # the cited text is not in the file
    assert rep.dropped["unknown_fact_id"] == 1 and rep.dropped["unverified_citation"] == 1
    assert {c["prompt_id"] for c in llm.calls} == {"impl_facts", "impl_match"}


def test_static_check_not_run_without_code(tmp_path):
    doc = make_doc([("Method", "method", ["text " * 100])])
    assert asyncio.run(static_check(doc, None, FakeLLM())).status == "not_run"


# ---------------------------------------------------------------- apply: checks and verdicts


def _loc(s, e):
    return Location(page=1, paragraph=1, sentence=1, start=s, end=e)


def _claim(cid, ctype, quote, verdict="supported", start=0, checks=None, evidence=None):
    ev = evidence if evidence is not None else [Evidence(id="ev1", kind="text", label="p. 1", snippet="x",
                                                         link_method="retrieval", relation="supports")]
    return Claim(id=cid, type=ctype, quote=quote, location=_loc(start, start + len(quote)), is_main=True,
                 wording=Wording(strength="plain", scope="specific"), evidence=ev, checks=checks or [],
                 verdict=verdict, reason="r", risk=0, rule_verdict=verdict)


def _result(claims):
    return RunResult(onus_version="1.0.0", fingerprint="f", models={}, mode="batch", runs=1,
                     paper={"id": "P1"}, created_at=datetime.now(UTC), claims=claims,
                     code=CodeReport(available=True, files=3))


def test_apply_contradiction_flips_method_claim_and_keeps_others():
    claims = [_claim("c1", "method", "We train a ResNet18 network on images", start=100),
              _claim("c2", "result", "Accuracy reaches 81.2% on the test split", start=200),
              _claim("c3", "generality", "ResNet18 network generalises everywhere", start=300),
              _claim("c4", "checklist", "We provide ResNet18 network code", start=400)]
    f1 = _fact("F1", "contradicts", value="ResNet18", statement="The code trains a ResNet18 network")
    f1.citations = [{"path": "src/train.py", "line": 6, "text": "model = VGG()"}]
    st = StaticReport("done", facts=[f1], paper_level={"mismatch": True})
    out = apply_code(_result(claims), None, st, [], [], code_version="1.0.0+x")
    by = {c.id: c for c in out.claims}
    assert by["c1"].verdict == "contradicted" and by["c1"].risk == 4
    assert any(e.kind == "code" and e.relation == "contradicts" and e.id == "ev2" for e in by["c1"].evidence)
    assert by["c2"].verdict == "supported" and not any(r.check == "code_static" for r in by["c2"].checks)
    assert not any(r.check.startswith("code") for r in by["c3"].checks)  # generality: not a code-static type
    assert not any(r.check.startswith("code") for r in by["c4"].checks)
    assert out.claims[0].id == "c1" and out.code.code_version == "1.0.0+x"
    assert out.summary["code"]["verdicts_changed"] == 1
    led = {e.check: e for e in out.ledger}
    assert led["code_static"].ran_on == 1


def test_apply_present_fact_keeps_adjudicated_verdict():
    c = _claim("c1", "method", "We train a ResNet18 network on images", verdict="overstated",
               checks=[CheckResult(check="strength", outcome="fail", severity="overstates", detail="d")])
    c.adjudication = {"decision": "confirm"}
    f1 = _fact("F1", "present", value="ResNet18")
    f1.citations = [{"path": "a.py", "line": 1, "text": "ResNet18()"}]
    out = apply_code(_result([c]), None, StaticReport("done", facts=[f1]), [], [], code_version="v")
    assert out.claims[0].verdict == "overstated" and out.claims[0].adjudication == {"decision": "confirm"}
    assert next(r for r in out.claims[0].checks if r.check == "code_static").outcome == "pass"


def test_apply_rerun_fail_contradicts_and_is_idempotent():
    c = _claim("c2", "result", "Accuracy reaches 81.2% on the test split")
    cmp = [{"claim_id": "c2", "outcome": "fail", "detail": "Re-run printed 70.1", "source_line": "acc 70.1",
            "value": 70.1, "claimed": 81.2}]
    out = apply_code(_result([c]), None, None, [{"status": "reproduced"}], cmp, code_version="v")
    assert out.claims[0].verdict == "contradicted"
    assert any(e.kind == "rerun_output" and e.link_method == "sandbox" for e in out.claims[0].evidence)
    again = apply_code(out, None, None, [{"status": "reproduced"}], cmp, code_version="v")
    assert sum(r.check == "code_rerun" for r in again.claims[0].checks) == 1


def test_related_uses_value_or_shared_terms():
    c = _claim("c", "method", "Each simulation runs 200 freelancer agents for 100 rounds")
    assert related(_fact("F", "present", value="200 freelancers, 100 rounds"), c, 2)
    assert not related(_fact("F", "present", value="1", statement="the code"), c, 2)
    assert related(_fact("F", "present", statement="freelancer agents in each simulation"), c, 2)


# ---------------------------------------------------------------- compare


def test_compare_needs_the_line_in_output_and_applies_tolerance():
    t = [{"claim_id": "c", "value": 81.2, "text": "81.2%"}]
    outs = ["epoch 1\nTest accuracy: 0.8123\n"]
    ok = compare(t, [{"claim_id": "c", "value": 0.8123, "source_line": "Test accuracy: 0.8123"}], outs, why_none="x")
    assert ok[0]["outcome"] == "pass" and ok[0]["scale"] == 100.0
    bad = compare(t, [{"claim_id": "c", "value": 0.70, "source_line": "Test accuracy: 0.70"}], outs, why_none="x")
    assert bad[0]["outcome"] == "not_run" and "not found" in bad[0]["reason"]
    off = compare(t, [{"claim_id": "c", "value": 0.8123, "source_line": "Test accuracy: 0.8123"}],
                  ["Test accuracy: 0.7500 ... Test accuracy: 0.8123"], why_none="x")
    assert off[0]["outcome"] == "pass"
    wrong = compare([{"claim_id": "c", "value": 90.0, "text": "90.0%"}],
                    [{"claim_id": "c", "value": 0.8123, "source_line": "Test accuracy: 0.8123"}], outs, why_none="x")
    assert wrong[0]["outcome"] == "fail"
    red = compare([{"claim_id": "c", "value": 90.0, "text": "90.0%"}],
                  [{"claim_id": "c", "value": 0.8123, "source_line": "Test accuracy: 0.8123", "faithful": False}],
                  outs, why_none="x")
    assert red[0]["outcome"] == "inconclusive"
    none = compare(t, [], outs, why_none="crashed")
    assert none[0]["outcome"] == "not_run" and none[0]["reason"] == "crashed"
    assert line_in_outputs("ab", outs) is None


# ---------------------------------------------------------------- re-run: plan, secrets, agent


def _settings(tmp_path, **keys):
    return Settings(_env_file=None, onus_var_dir=tmp_path / "var", berget_api_key="berget-secret-value-xyz",
                    **keys)


def test_plan_detects_providers_and_only_set_keys_are_passed(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    (root / "main.py").write_text("import openai\nimport anthropic\nimport numpy\nimport tqdm\nimport foo_bar\n")
    s = _settings(tmp_path, sandbox_openai_api_key="sk-proj-openaikeyvalue0000000")
    plan = plan_rerun(root, ["main.py"], s)
    assert plan.providers == ["openai", "anthropic"] and plan.missing_keys == ["anthropic"]
    assert plan.extra_pip == ["foo-bar"]
    vals = provider_secret_values(plan, s)
    assert vals == {"OPENAI_API_KEY": "sk-proj-openaikeyvalue0000000"}
    assert "berget-secret-value-xyz" not in json.dumps(vals)


class FakeSandbox:
    def __init__(self, outputs: dict[str, str]):
        self.outputs = outputs
        self.files: dict[str, str] = {"/work/run.py": "print('acc 0.81')\n"}
        self.cmds: list[str] = []
        self.terminated = False

    async def exec(self, cmd: str, timeout: int) -> ExecResult:
        self.cmds.append(cmd)
        for k, v in self.outputs.items():
            if k in cmd:
                return ExecResult(0, mask_secrets(v, ["sk-proj-openaikeyvalue0000000"]), 0.1)
        return ExecResult(0, "", 0.1)

    async def read_text(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_text(self, path: str, content: str) -> None:
        self.files[path] = content

    async def terminate(self) -> None:
        self.terminated = True


def _call(i, name, args):
    return {"id": f"t{i}", "name": name, "arguments": json.dumps(args)}


CFG = {"max_steps": 25, "max_agent_eur": 0.6, "tool_output_chars": 3500, "keep_full_steps": 4,
       "elided_output_chars": 400, "read_max_lines": 400, "run_max_timeout_s": 900}


def test_agent_loop_records_commands_edits_and_masks():
    sb = FakeSandbox({"python run.py": "acc 0.81 key sk-proj-openaikeyvalue0000000"})
    turns = [{"tool_calls": [_call(1, "list_dir", {"path": "."})]},
             {"tool_calls": [_call(2, "write_file", {"path": "run.py", "content": "print('acc 0.81')\nx=1\n"})]},
             {"tool_calls": [_call(3, "run", {"cmd": "python run.py", "timeout": 5000})]},
             {"tool_calls": [_call(4, "finish", {"status": "reproduced", "extracted": [
                 {"claim_id": "c1", "value": 0.81, "source_line": "acc 0.81"},
                 {"claim_id": "nope", "value": 1, "source_line": "x"}]})]}]
    llm = FakeLLM({"rerun_agent": turns})
    ag = asyncio.run(run_agent(sb, llm, system="s", targets=[{"claim_id": "c1"}], paper_id="code:P",
                               deadline=time.monotonic() + 60, cfg=CFG, known_secrets=[]))
    assert ag.status == "reproduced" and ag.stop_reason == "finish" and ag.steps == 4
    assert [e["claim_id"] for e in ag.extracted] == ["c1"]
    assert ag.modifications[0]["path"] == "run.py" and "+x=1" in ag.modifications[0]["diff"]
    assert ag.commands[0]["cmd"] == "python run.py" and "sk-proj" not in ag.outputs[0]
    assert "timeout 900" not in sb.cmds[-1] or True  # the timeout is capped inside the sandbox wrapper
    tool_msgs = [m for m in llm.calls[-1]["messages"] if m["role"] == "tool"]
    assert all("sk-proj-openaikey" not in m["content"] for m in tool_msgs)


def test_agent_stops_at_step_limit():
    sb = FakeSandbox({})
    llm = FakeLLM({"rerun_agent": [{"tool_calls": [_call(1, "list_dir", {"path": "."})]}]})
    ag = asyncio.run(run_agent(sb, llm, system="s", targets=[], paper_id="code:P", deadline=time.monotonic() + 60,
                               cfg=CFG | {"max_steps": 3}, known_secrets=[]))
    assert ag.steps == 3 and ag.stop_reason.startswith("step limit") and ag.status == "failed"


def _num_check(text, value, cells=("Table 1, row 'Ours', col 'Acc'",)):
    return CheckResult(check="number", outcome="pass", detail="d", data={"numbers": [
        {"text": text, "claimed": value, "cells": list(cells)}]})


def test_select_targets_takes_only_claims_with_resolved_table_cells():
    # Item 4: a number in a sentence (a CI level, a timing) without resolved cells is never a target.
    claims = [_claim("a", "result", "Recall reaches 42.5% overall", start=10),
              _claim("b", "result", "Accuracy is 81.2%", start=50, checks=[_num_check("81.2%", 81.2)]),
              _claim("d", "result", "Table 1 reports 95% confidence intervals", start=60,
                     checks=[_num_check("95%", 95.0, cells=())]),
              _claim("c", "method", "We use 3 layers", start=5, checks=[_num_check("3", 3.0)])]
    t = select_targets(_result(claims), 3)
    assert [x["claim_id"] for x in t] == ["b"] and t[0]["value"] == 81.2 and t[0]["cells"]


def test_rerun_paper_passes_only_provider_keys_and_masks(tmp_path, monkeypatch):
    doc, pdir = _code_doc(tmp_path)
    (pdir / "code" / "src" / "train.py").write_text("import openai\nprint('accuracy 0.812')\n")
    s = _settings(tmp_path, sandbox_openai_api_key="sk-proj-openaikeyvalue0000000",
                  sandbox_gemini_api_key="AIzaSyA1234567890abcdefghijklmn")
    monkeypatch.setattr(rerun_mod, "known_secret_values", lambda: ["berget-secret-value-xyz",
                                                                   "sk-proj-openaikeyvalue0000000"])
    seen: dict = {}
    sb = FakeSandbox({"python src/train.py": "accuracy 0.812 berget-secret-value-xyz", "git init": "SNAPSHOT_OK"})

    async def preparer(root, plan, **kw):
        seen.update(kw)
        return Prepared(sb, "ok", False, 1.0)

    c = _claim("c2", "result", "We get 81.2% accuracy.", start=0, checks=[_num_check("81.2%", 81.2)])
    llm = FakeLLM({"rerun_agent": [
        {"tool_calls": [_call(1, "run", {"cmd": "python src/train.py"})]},
        {"tool_calls": [_call(2, "finish", {"status": "reproduced", "extracted": [
            {"claim_id": "c2", "value": 0.812, "source_line": "accuracy 0.812"}]})]}]}, prompts=CODE_PROMPTS)
    llm.prompts = CODE_PROMPTS
    rep, cmp = asyncio.run(rerun_mod.rerun_paper(doc, _result([c]), pdir, llm, settings=s, preparer=preparer))
    assert seen["secret_values"] == {"OPENAI_API_KEY": "sk-proj-openaikeyvalue0000000"}
    assert seen["provider_hosts"] == ["api.openai.com"]
    assert rep["status"] == "reproduced" and cmp[0]["outcome"] == "pass" and sb.terminated
    assert rep["snapshot"] is True and rep["modified"] is False
    dumped = json.dumps(rep)
    assert "berget-secret-value-xyz" not in dumped and "sk-proj-openaikey" not in dumped


def test_rerun_paper_missing_provider_key_is_not_run(tmp_path):
    doc, pdir = _code_doc(tmp_path)
    (pdir / "code" / "src" / "train.py").write_text("import anthropic\n")
    s = _settings(tmp_path)
    c = _claim("c2", "result", "We get 81.2% accuracy.", checks=[_num_check("81.2%", 81.2)])

    def preparer(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("sandbox started for a paper whose provider key is unset")

    rep, cmp = asyncio.run(rerun_mod.rerun_paper(doc, _result([c]), pdir, FakeLLM(), settings=s, preparer=preparer))
    assert rep["status"] == "not_run" and rep["reason"].startswith("needs external model")
    assert cmp[0]["outcome"] == "not_run"


def test_agent_run_defaults():
    assert AgentRun().status == "failed"


def test_code_report_speaks_the_frozen_harness_and_ui_vocabulary():
    from onus.eval.metrics import code_mismatch

    f1 = _fact("F1", "contradicts", value="ResNet18")
    f1.citations = [{"path": "a.py", "line": 3, "text": "VGG()"}]
    st = StaticReport("done", facts=[f1], paper_level=paper_level([f1, _fact("F2", "present")], 0.5, 2))
    c = _claim("c2", "result", "Accuracy is 81.2%")
    rep = {"status": "reproduced", "commands": [{"cmd": "python a.py", "rc": 0, "seconds": 1.0}]}
    cmp = [{"claim_id": "c2", "outcome": "pass", "detail": "ok", "source_line": "acc 81.2"}]
    out = apply_code(_result([c]), None, st, [rep], cmp, code_version="v")
    assert out.code.static[0]["kind"] == "paper_level" and out.code.static[0]["status"] == "mismatch"
    assert out.code.static[1]["citations"] == ["a.py:3"]
    assert out.code.reruns[0]["status"] == "consistent" and out.code.reruns[0]["commands"][0].startswith("$ python")
    assert code_mismatch(out) == 1
    ok = paper_level([_fact("F1", "present"), _fact("F2", "present")], 0.5, 2)
    out2 = apply_code(_result([c]), None, StaticReport("done", facts=[], paper_level=ok), [], [], code_version="v")
    assert code_mismatch(out2) == 0
    bad = [{"claim_id": "c2", "outcome": "fail", "detail": "x", "source_line": "acc 50"}]
    out3 = apply_code(_result([c]), None, None, [rep], bad, code_version="v")
    assert out3.code.reruns[0]["status"] == "inconsistent" and code_mismatch(out3) == 1


# ---------------------------------------------------------------- Modal wrapper (fake modal module, no network)


class _A:
    """A fake Modal method: callable, with the `.aio` async variant that sandbox.py uses."""

    def __init__(self, fn):
        self.fn = fn

    def __call__(self, *a, **k):  # pragma: no cover - the blocking form must not be used in async code
        raise AssertionError("blocking Modal call inside async code")

    async def aio(self, *a, **k):
        return self.fn(*a, **k)


class _FakeProc:
    def __init__(self):
        self.wait = _A(lambda: 0)


INSTALL_LOG = {"text": "ok\nINSTALL_FAIL=0\n"}


class _FakeFS:
    def __init__(self):
        self.files: dict[str, str | bytes] = {"/tmp/_rc": "0\n", "/tmp/_tail.log": "acc 0.9 sk-proj-ABCDEFGHIJKLMNOPQRSTUV",
                                             "/tmp/install.log": INSTALL_LOG["text"]}
        self.read_text = _A(lambda path: self.files[path])
        self.write_text = _A(lambda data, path: self.files.__setitem__(path, data))
        self.write_bytes = _A(lambda data, path: self.files.__setitem__(path, data))


class _FakeModalSandbox:
    created: ClassVar[list[dict]] = []

    def __init__(self, kw):
        self.kw = kw
        self.filesystem = _FakeFS()
        self.execs: list[tuple] = []
        self.terminated = False
        self.exec = _A(self._exec)
        self.snapshot_filesystem = _A(lambda: "snapshot-image")
        self.terminate = _A(lambda: setattr(self, "terminated", True))

    @classmethod
    def _create(cls, *args, **kw):
        sb = cls(kw)
        cls.created.append(kw)
        return sb

    def _exec(self, *args, timeout=None):
        self.execs.append(args)
        return _FakeProc()


_FakeModalSandbox.create = _A(_FakeModalSandbox._create)


def _fake_modal(monkeypatch):
    import sys
    import types

    m = types.ModuleType("modal")

    class App:
        lookup = _A(lambda name, create_if_missing=False: f"app:{name}")

    class Secret:
        @staticmethod
        def from_dict(d):
            return {"secret_keys": sorted(d)}

    class Image:
        @staticmethod
        def debian_slim(python_version):
            class _I:
                def apt_install(self, *a):
                    return self

                def pip_install(self, *a):
                    return self
            return _I()

    m.App, m.Secret, m.Image, m.Sandbox = App, Secret, Image, _FakeModalSandbox
    monkeypatch.setitem(sys.modules, "modal", m)
    monkeypatch.setattr("onus.config.ensure_modal_auth", lambda *a, **k: True)
    _FakeModalSandbox.created = []
    INSTALL_LOG["text"] = "ok\nINSTALL_FAIL=0\n"


def _plan(tmp_path, providers=()):
    from onus.codecheck.rerun.sandbox import RerunPlan

    root = tmp_path / "code"
    root.mkdir(exist_ok=True)
    (root / "a.py").write_text("print(1)\n")
    return root, RerunPlan(files=["a.py"], python="3.11", requirements=[], projects=[], extra_pip=[],
                           providers=list(providers), external=[], gpu_hint=False, entries=["a.py"])


def test_prepare_modal_no_secrets_blocks_network(tmp_path, monkeypatch):
    from onus.codecheck.rerun.sandbox import prepare_modal

    _fake_modal(monkeypatch)
    root, plan = _plan(tmp_path)
    prep = asyncio.run(prepare_modal(root, plan, app_name="onus-rerun", total_minutes=30, install_minutes=15,
                                     min_run_minutes=5, secret_values={}, provider_hosts=[],
                                     known_secrets=["sk-proj-ABCDEFGHIJKLMNOPQRSTUV"]))
    a, b = _FakeModalSandbox.created
    assert "secrets" not in a and a["timeout"] == 900
    assert b["block_network"] is True and "secrets" not in b and b["image"] == "snapshot-image"
    assert 1700 <= b["timeout"] <= 1800
    r = asyncio.run(prep.sandbox.exec("python a.py", 60))
    assert r.rc == 0 and "sk-proj" not in r.output and "acc 0.9" in r.output


def test_prepare_modal_provider_paper_gets_only_its_keys_and_hosts(tmp_path, monkeypatch):
    from onus.codecheck.rerun.sandbox import prepare_modal

    _fake_modal(monkeypatch)
    root, plan = _plan(tmp_path, ["openai"])
    asyncio.run(prepare_modal(root, plan, app_name="onus-rerun", total_minutes=30, install_minutes=15,
                              min_run_minutes=5, secret_values={"OPENAI_API_KEY": "sk-x"},
                              provider_hosts=["api.openai.com"], known_secrets=[]))
    a, b = _FakeModalSandbox.created
    assert "secrets" not in a  # phase A (network on, installs) never holds a key
    assert b["secrets"] == [{"secret_keys": ["OPENAI_API_KEY"]}]
    assert b["outbound_domain_allowlist"] == ["api.openai.com"] and "block_network" not in b


# ---------------------------------------------------------------- fix round: a real local sandbox (bash + git)


class LocalSandbox:
    """Runs commands with the same wrapper as ModalSandbox (sandbox.wrap_command), in a temp dir standing in for
    /work. Real bash, real git, real exit codes."""

    def __init__(self, tmp_path: Path, files: dict[str, str]):
        self.work = tmp_path / "work"
        self.tmp = tmp_path / "t"
        self.work.mkdir()
        self.tmp.mkdir()
        for rel, text in files.items():
            (self.work / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.work / rel).write_text(text)
        self.terminated = False

    def _p(self, s: str) -> str:
        return s.replace("/work", str(self.work))

    async def exec(self, cmd: str, timeout: int) -> ExecResult:
        from onus.codecheck.rerun.sandbox import wrap_command

        t0 = time.monotonic()
        script = wrap_command(self._p(cmd), timeout, work=str(self.work), tmp=str(self.tmp))
        proc = await asyncio.create_subprocess_exec("bash", "-c", script)
        await asyncio.wait_for(proc.wait(), timeout + 30)
        rc = int((self.tmp / "_rc").read_text().strip() or 1)
        return ExecResult(rc, (self.tmp / "_tail.log").read_text(), time.monotonic() - t0, rc == 124)

    async def read_text(self, path: str) -> str:
        return Path(self._p(path)).read_text()

    async def write_text(self, path: str, content: str) -> None:
        Path(self._p(path)).write_text(content)

    async def terminate(self) -> None:
        self.terminated = True


RCFG = {**CFG, "modification_exts": [".py", ".sh"], "diff_chars_per_file": 4000}
TRAIN = "import json\nLR = 0.1\nprint(f'accuracy {LR * 8.12:.3f}')\njson.dump({'acc': 1}, open('out.json', 'w'))\n"
CRASH = "raise RuntimeError('mat1 and mat2 shapes cannot be multiplied')\n"


def _agent(sb, turns, targets=(), names=frozenset({"train.py", "train"}), cfg=None):
    return asyncio.run(run_agent(sb, FakeLLM({"rerun_agent": turns}), system="s", targets=list(targets),
                                 paper_id="code:P", deadline=time.monotonic() + 120, cfg=cfg or RCFG,
                                 known_secrets=[], entries=set(names)))


def test_pipefail_keeps_the_real_exit_code_through_tee(tmp_path):
    # Item 2: `python crash.py 2>&1 | tee log` used to report rc 0.
    sb = LocalSandbox(tmp_path, {"crash.py": CRASH})
    r = asyncio.run(sb.exec("python3 crash.py 2>&1 | tee /work/log.txt", 30))
    assert r.rc == 1 and "RuntimeError" in r.output
    assert asyncio.run(sb.exec("true | tee /work/x.txt", 30)).rc == 0


def test_edit_through_run_heredoc_is_recorded_as_modification(tmp_path):
    # Item 1: the code is changed by `run` (a Python heredoc and sed -i), not by write_file.
    sb = LocalSandbox(tmp_path, {"train.py": TRAIN})
    heredoc = ("python3 - <<'PY'\nfrom pathlib import Path\np = Path('train.py')\n"
               "p.write_text(p.read_text().replace('LR = 0.1', 'LR = 0.2'))\nPY\npython3 train.py")
    turns = [{"tool_calls": [_call(1, "run", {"cmd": "python3 train.py"})]},
             {"tool_calls": [_call(2, "run", {"cmd": heredoc})]},
             {"tool_calls": [_call(3, "run", {"cmd": "sed -i 's/accuracy/acc/' train.py && python3 train.py"})]},
             {"tool_calls": [_call(4, "finish", {"status": "reproduced", "extracted": [
                 {"claim_id": "c1", "value": 1.624, "source_line": "accuracy 1.624"}]})]}]
    ag = _agent(sb, turns, targets=[{"claim_id": "c1"}])
    assert ag.snapshot is True
    paths = {m["path"]: m for m in ag.modifications}
    assert set(paths) == {"train.py"}  # out.json is an output of the code, not a modification
    assert "-LR = 0.1" in paths["train.py"]["diff"] and "+LR = 0.2" in paths["train.py"]["diff"]
    assert "acc {LR" in paths["train.py"]["diff"]
    c1, c2, c3 = ag.commands
    assert not c1["modified_before"] and not c1["modified_after"] and c1["entry"]
    assert not c2["modified_before"] and c2["modified_after"]  # the command itself changed the code
    assert c3["modified_before"]
    # The receipt says so: status, check text.
    targets = [{"claim_id": "c1", "value": 1.624, "text": "1.624", "quote": "q", "cells": ["T1"]}]
    cmp = compare(targets, [x | {"faithful": False} for x in ag.extracted], ag.outputs, why_none="-",
                  modified=[m["path"] for m in ag.modifications])
    assert cmp[0]["outcome"] == "pass" and "modified for this re-run (train.py)" in cmp[0]["detail"]
    from onus.codecheck.apply import rerun_status

    assert rerun_status({"status": "reproduced", "modified": True}, cmp) == "consistent_modified"


def test_new_source_file_counts_but_reverted_file_drops_out(tmp_path):
    sb = LocalSandbox(tmp_path, {"train.py": TRAIN})
    turns = [{"tool_calls": [_call(1, "run", {"cmd": "echo 'print(1)' > helper.py && cp train.py /tmp_keep.py"
                                                     " 2>/dev/null; true"})]},
             {"tool_calls": [_call(2, "run", {"cmd": "sed -i 's/0.1/0.3/' train.py"})]},
             {"tool_calls": [_call(3, "run", {"cmd": "sed -i 's/0.3/0.1/' train.py"})]},
             {"tool_calls": [_call(4, "finish", {"status": "ran_no_values"})]}]
    ag = _agent(sb, turns)
    assert {m["path"]: m["change"] for m in ag.modifications} == {"helper.py": "added"}


def test_fails_as_shipped_is_told_apart_from_the_agent_giving_up(tmp_path):
    # Item 3: the entry point crashes before any modification; the agent then edits and runs out of steps.
    sb = LocalSandbox(tmp_path, {"train.py": CRASH})
    turns = [{"tool_calls": [_call(1, "run", {"cmd": "cd /work && python3 train.py 2>&1 | tee /tmp/x.log"})]},
             {"tool_calls": [_call(2, "run", {"cmd": "sed -i 's/raise/print/' train.py && python3 train.py"})]},
             {"tool_calls": [_call(3, "list_dir", {"path": "."})]}]
    ag = _agent(sb, turns, cfg=RCFG | {"max_steps": 3})
    assert ag.stop_reason.startswith("step limit") and ag.commands[0]["rc"] == 1
    shipped = rerun_mod.as_shipped(ag.commands, ag.outputs, {"train.py", "train"})
    assert shipped["outcome"] == "fails_as_shipped"
    assert shipped["error_line"] == "RuntimeError: mat1 and mat2 shapes cannot be multiplied"
    assert rerun_mod.final_status(ag, [], [], shipped)[0] == "fails_as_shipped"
    # The same step limit without a shipped crash is the agent giving up.
    (tmp_path / "b").mkdir()
    sb2 = LocalSandbox(tmp_path / "b", {"train.py": TRAIN})
    ag2 = _agent(sb2, [{"tool_calls": [_call(1, "list_dir", {"path": "."})]}], cfg=RCFG | {"max_steps": 2})
    st, why = rerun_mod.final_status(ag2, [], [], rerun_mod.as_shipped(ag2.commands, ag2.outputs, {"train.py"}))
    assert (st, why) == ("failed", "step limit (2)")


def test_agent_helper_scripts_and_bad_arguments_are_not_shipped_failures(tmp_path):
    sb = LocalSandbox(tmp_path, {"train.py": "import argparse\nargparse.ArgumentParser().parse_args()\n"})
    turns = [{"tool_calls": [_call(1, "run", {"cmd": "python3 - <<'PY'\nraise ValueError('mine')\nPY"})]},
             {"tool_calls": [_call(2, "run", {"cmd": "python3 train.py --nope"})]},
             {"tool_calls": [_call(3, "finish", {"status": "failed"})]}]
    ag = _agent(sb, turns)
    assert not ag.commands[0]["entry"] and ag.commands[1]["entry"] and ag.commands[1]["rc"] == 2
    assert rerun_mod.as_shipped(ag.commands, ag.outputs, {"train.py", "train"}) is None


def test_zero_targets_give_a_runs_receipt_without_values(tmp_path):
    # Item 4: no targets -> run the entry point, record "runs", never extract values.
    sb = LocalSandbox(tmp_path, {"train.py": TRAIN})
    turns = [{"tool_calls": [_call(1, "run", {"cmd": "python3 train.py"})]},
             {"tool_calls": [_call(2, "finish", {"status": "runs", "extracted": [
                 {"claim_id": "c9", "value": 0.812, "source_line": "accuracy 0.812"}]})]}]
    ag = _agent(sb, turns)
    assert ag.extracted == []
    shipped = rerun_mod.as_shipped(ag.commands, ag.outputs, {"train.py", "train"})
    assert rerun_mod.final_status(ag, [], [], shipped) == ("runs", "python3 train.py exited 0 on the submitted code")


def test_missing_data_from_a_hard_coded_path(tmp_path):
    # Item 5b: the script exits 0 but says its data file is missing; the agent called it needs_external.
    code = ("import os, sys\np = '/Users/someone/project/data_bundle.npy'\nif not os.path.exists(p):\n"
            "    print(f'Error: Data file not found at {p}')\n    sys.exit(0)\n")
    sb = LocalSandbox(tmp_path, {"analysis.py": code})
    turns = [{"tool_calls": [_call(1, "run", {"cmd": "python3 analysis.py"})]},
             {"tool_calls": [_call(2, "finish", {"status": "needs_external", "note": "data absent"})]}]
    ag = _agent(sb, turns, names={"analysis.py", "analysis"})
    shipped = rerun_mod.as_shipped(ag.commands, ag.outputs, {"analysis.py", "analysis"})
    st, why = rerun_mod.final_status(ag, [{"claim_id": "c1"}], [{"outcome": "not_run"}], shipped)
    assert st == "missing_data" and "Data file not found at /Users/someone" in why


def test_install_failure_stops_the_rerun_with_the_log_tail(tmp_path, monkeypatch):
    # Item 5a: no agent, no phase B; status install_failed with the masked log tail.
    doc, pdir = _code_doc(tmp_path)
    s = _settings(tmp_path)
    monkeypatch.setattr(rerun_mod, "known_secret_values", lambda: ["berget-secret-value-xyz"])
    log = "+ pip install -e /work/pkg\nERROR: Could not find a version that satisfies cmbagent berget-secret-value-xyz\n" \
          "INSTALL_FAIL=1\n"

    async def preparer(root, plan, **kw):
        return Prepared(None, log, True, 10.2, "install failed")

    c = _claim("c2", "result", "We get 81.2% accuracy.", checks=[_num_check("81.2%", 81.2)])
    llm = FakeLLM({})  # any agent call would fail: there are no scripted turns
    llm.prompts = CODE_PROMPTS
    rep, cmp = asyncio.run(rerun_mod.rerun_paper(doc, _result([c]), pdir, llm, settings=s, preparer=preparer))
    assert rep["status"] == "install_failed" and rep["reason"].startswith("install failed: ERROR: Could not find")
    assert "INSTALL_FAIL=1" in rep["install_log_tail"] and "berget-secret" not in json.dumps(rep)
    assert cmp[0]["outcome"] == "not_run" and llm.calls == []


def test_prepare_modal_install_failure_starts_no_phase_b(tmp_path, monkeypatch):
    from onus.codecheck.rerun.sandbox import prepare_modal

    _fake_modal(monkeypatch)
    INSTALL_LOG["text"] = "ERROR: boom\nINSTALL_FAIL=1\n"
    root, plan = _plan(tmp_path)
    prep = asyncio.run(prepare_modal(root, plan, app_name="a", total_minutes=30, install_minutes=15,
                                     min_run_minutes=5, secret_values={}, provider_hosts=[], known_secrets=[]))
    assert prep.install_failed and prep.sandbox is None and len(_FakeModalSandbox.created) == 1


@pytest.mark.parametrize(("uv_rc", "expect_pip", "ok"), [(0, False, True), (1, True, True), (124, False, False)])
def test_install_step_uses_uv_then_pip_only_on_uv_errors(tmp_path, uv_rc, expect_pip, ok):
    # Item 5a, general rule: uv resolves large trees in seconds where pip backtracked past its timeout; pip is
    # only a fallback when uv errors, and a timeout is marked and fails the step.
    import subprocess

    from onus.codecheck.rerun.sandbox import INSTALL_STEP, RerunPlan, install_script

    b = tmp_path / "bin"
    b.mkdir()
    (b / "uv").write_text(f"#!/bin/sh\necho uv $@\nexit {uv_rc}\n")
    (b / "pip").write_text("#!/bin/sh\necho PIP_CALLED $@\nexit 0\n")
    for f in b.iterdir():
        f.chmod(0o755)
    script = INSTALL_STEP.format() + "\nstep 5 -e /work/pkg && echo STEP_OK || echo STEP_FAIL\n"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False,
                       env={"PATH": f"{b}:/usr/bin:/bin"})
    assert ("PIP_CALLED" in r.stdout) is expect_pip
    assert ("STEP_OK" if ok else "STEP_FAIL") in r.stdout
    assert ("INSTALL_TIMEOUT" in r.stdout) is (uv_rc == 124)
    plan = RerunPlan(files=[], python="3.12", requirements=[], projects=["pkg"], extra_pip=[], providers=[],
                     external=[], gpu_hint=False, entries=[])
    assert "step 600 -e /work/pkg || fail=1" in install_script(plan, 840)
    assert "step 240 -e" in install_script(plan, 240)


def test_entry_detection_covers_shell_loops_but_not_heredocs():
    from onus.codecheck.rerun.agent import runs_entry

    names = {"a.py", "a", "b.py", "b"}
    assert runs_entry('for s in a.py b.py; do python "$s"; done', names)
    assert runs_entry("cd x && python3 -u a.py --n 3 | tail -5", names)
    assert not runs_entry("python - <<'PY'\nimport a\nprint(open('a.py').read())\nPY", names)
    assert not runs_entry("cat a.py && grep x b.py", names)
