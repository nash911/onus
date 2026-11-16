"""One paper's re-run: plan, phase A/B sandbox, agent, comparison -> RerunReport. (Phase 10)

RerunReport (stored in RunResult.code.reruns):
  {status, reason, commands[{cmd, rc, seconds, timed_out, entry, modified_before, modified_after, error_line}],
   modifications[{path, change, diff}], modified, snapshot, as_shipped{outcome, cmd, rc, error_line, ...},
   stdout_tail, install_log_tail, extracted[{claim_id, value, source_line, faithful}], targets[...], minutes,
   minutes_install, eur, steps, stop_reason, network, providers}
Statuses (decided by code from the command log, not by the agent's word):
- reproduced: at least one value was verified in the output (apply.py shows consistent / inconsistent /
  consistent_modified);
- runs: no target claims, and a run of the submission's own entry point exited 0 on the unmodified code;
- fails_as_shipped: an entry point of the unmodified code crashed (error line kept) and none ran cleanly;
- missing_data: the unmodified code stops because data the submission does not include is missing (hard-coded
  absolute paths, missing input files), or the agent found the data absent;
- install_failed: phase A failed; the run is not attempted (masked install-log tail kept);
- ran_no_values, needs_external (hardware, services or models that are not available), failed (the agent gave
  up: step limit, time limit, spend cap, model error, or its own "failed"), not_run.
Every string is masked for keys.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from onus.codecheck.common import code_root, known_secret_values, mask_secrets, rubric
from onus.codecheck.rerun.agent import AgentRun, entry_names, error_line, run_agent, select_targets
from onus.codecheck.rerun.compare import compare
from onus.codecheck.rerun.sandbox import Prepared, plan_rerun, prepare_modal, provider_secret_values
from onus.llm.prompts import render_template
from onus.schemas.document import Document
from onus.schemas.result import RunResult

Preparer = Callable[..., Awaitable[Prepared]]

MISSING_DATA = re.compile(r"FileNotFoundError|No such file or directory|\bnot found (?:at|in)\b|[Ff]ile not found|"
                          r"does not exist|\bNo '[^']+' file found|\[Errno 2\]|[Mm]issing (?:data|input|file)")
NOT_THE_CODE = re.compile(r"can't open file|^usage:|unrecognized arguments|the following arguments are required|"
                          r"command not found", re.MULTILINE)
NO_MODULE = re.compile(r"No module named '([\w.]+)'")


def _missing_line(output: str) -> str:
    for ln in output.splitlines():
        if MISSING_DATA.search(ln) and re.search(r"(?i)error|not found|no such|does not exist|missing", ln):
            return ln.strip()[:300]
    return ""


def as_shipped(commands: list[dict], outputs: list[str], names: set[str]) -> dict | None:
    """What the submission's own entry points did on the unmodified code (code, from the command log)."""
    runs_ok, crashed, missing = [], [], []
    for c, o in zip(commands, outputs, strict=False):
        if not c.get("entry") or c.get("modified_before") or c.get("modified_after") or c.get("timed_out"):
            continue
        miss = _missing_line(o) if (c["rc"] != 0 or "rror" in o) else ""
        if miss:
            missing.append((c, miss))
        elif c["rc"] == 0:
            runs_ok.append(c)
        elif c["rc"] > 0 and not NOT_THE_CODE.search(o):
            m = NO_MODULE.search(o)
            if m and m.group(1).split(".")[0] in names:  # a local module: the agent ran it from the wrong place
                continue
            crashed.append((c, error_line(o)))
    if not (runs_ok or crashed or missing):
        return None
    counts = {"runs_ok": len(runs_ok), "crashed": len(crashed), "missing_data": len(missing)}
    if runs_ok:
        return {"outcome": "runs", "cmd": runs_ok[0]["cmd"], "rc": 0, "error_line": ""} | counts
    c, line = (missing or crashed)[0]
    return {"outcome": "missing_data" if missing else "fails_as_shipped", "cmd": c["cmd"], "rc": c["rc"],
            "error_line": line} | counts


def final_status(ag: AgentRun, targets: list[dict], cmp: list[dict], shipped: dict | None) -> tuple[str, str]:
    """(status, reason) from what the commands did; the agent's own status is used only where code cannot tell."""
    faithful = [c for c in cmp if c["outcome"] in ("pass", "fail") and c.get("faithful")]
    verified = [c for c in cmp if c["outcome"] in ("pass", "fail", "inconclusive")]
    note = ag.note or ag.stop_reason or ag.status
    if faithful:
        return "reproduced", note
    if shipped and shipped["outcome"] in ("fails_as_shipped", "missing_data"):
        return shipped["outcome"], f"{shipped['cmd'][:150]} -> {shipped['error_line']}"
    if verified:
        return "reproduced", note
    if shipped and shipped["outcome"] == "runs" and not targets:
        return "runs", f"{shipped['cmd'][:150]} exited 0 on the submitted code"
    if ag.status == "missing_data":
        return "missing_data", note
    if ag.stop_reason != "finish":
        return "failed", ag.stop_reason
    if ag.status == "runs":
        return "ran_no_values", (note if targets else "no run of the submission's own entry point was recorded: "
                                 + note)
    return ag.status, note




def _not_run(reason: str, targets: list[dict], **kw) -> tuple[dict, list[dict]]:
    rep = {"status": "not_run", "reason": reason, "commands": [], "modifications": [], "stdout_tail": "",
           "extracted": [], "targets": targets, "minutes": 0.0, **kw}
    return rep, compare(targets, [], [], why_none=reason)


async def rerun_paper(doc: Document, result: RunResult, paper_dir: Path | None, llm, *, settings,
                      preparer: Preparer = prepare_modal, max_minutes: float | None = None) -> tuple[dict, list[dict]]:
    cfg = rubric("rerun.yaml")
    targets = select_targets(result, int(cfg["max_targets"]))
    if not doc.code.present:
        return _not_run("no source code submitted", targets)
    root = code_root(doc, paper_dir)
    if root is None:
        return _not_run("code directory not found", targets)
    plan = plan_rerun(root, doc.code.entry_candidates, settings, str(cfg["python_default"]))
    if plan.missing_keys:
        return _not_run(f"needs external model ({', '.join(plan.missing_keys)} key not set)", targets,
                        providers=plan.providers)
    secrets = provider_secret_values(plan, settings)
    hosts = [cfg["provider_hosts"][p] for p in plan.providers]
    known = known_secret_values()
    total = float(max_minutes or settings.onus_rerun_max_minutes)
    t0 = time.monotonic()
    install_min = min(float(cfg["install_max_minutes"]), total / 2)
    prep = await preparer(root, plan, app_name=settings.onus_modal_app, total_minutes=total,
                          install_minutes=install_min, min_run_minutes=float(cfg["min_run_minutes"]),
                          secret_values=secrets, provider_hosts=hosts, known_secrets=known)
    secrets = {}  # drop the values as soon as the sandbox holds them
    install_tail = mask_secrets(prep.install_log[-int(cfg["install_log_chars"]):], known)
    common = {"providers": plan.providers, "extra_pip": plan.extra_pip, "install_log_tail": install_tail,
              "minutes_install": round(prep.minutes_a, 2)}
    if prep.install_failed:
        if prep.sandbox is not None:
            await prep.sandbox.terminate()
        why = install_error(prep.install_log) or "install failed"
        rep, cmp = _not_run(f"install failed: {mask_secrets(why, known)}", targets, **common)
        rep["status"] = "install_failed"
        rep["minutes"] = round((time.monotonic() - t0) / 60, 2)
        return rep, cmp
    if prep.sandbox is None:
        rep, cmp = _not_run(f"sandbox not available: {prep.error}", targets, **common)
        rep["minutes"] = round((time.monotonic() - t0) / 60, 2)
        return rep, cmp
    deadline = max(t0 + total * 60, time.monotonic() + float(cfg["min_run_minutes"]) * 60) - 30
    files = "\n".join(f"{f['path']} ({f['lines']})" for f in doc.code.files if f.get("is_source"))[:6000]
    if not targets:  # still a receipt: does the entry point run as submitted?
        cfg = cfg | {"max_steps": int(cfg["max_steps_no_targets"])}
    tgt = "\n".join(f"- claim_id {t['claim_id']}: \"{t['quote']}\" -> reproduce {t['text']}"
                    + (f" (paper table: {t['cells'][0]})" if t["cells"] else "") for t in targets) or (
        "(none: no number in the paper could be traced to a results table. Do not look for values. Run the main "
        "experiment as the README or the entry points describe, unmodified, and finish with status \"runs\" if it "
        "completes, \"failed\" if it crashes, or \"missing_data\" if input data is absent.)")
    prompt = llm.prompts.get("rerun_agent")
    minutes_left = max(1, int((deadline - time.monotonic()) / 60))
    system = render_template(prompt.system or "", {
        "network": "network access only to " + ", ".join(hosts) if hosts else "no network access",
        "minutes": minutes_left, "run_timeout": cfg["run_max_timeout_s"], "targets": tgt, "files": files,
        "entries": ", ".join(doc.code.entry_candidates[:10]) or "none found", "read_lines": cfg["read_max_lines"],
        "max_steps": cfg["max_steps"]})
    names = entry_names(plan.files)
    try:
        ag = await run_agent(prep.sandbox, llm, system=system, targets=targets, paper_id=f"code:{doc.paper_id}",
                             deadline=deadline, cfg=cfg, known_secrets=known, entries=names)
    finally:
        await prep.sandbox.terminate()
    minutes = round((time.monotonic() - t0) / 60, 2)
    modified = [m["path"] for m in ag.modifications]
    if not ag.snapshot and not modified:
        modified = ["(snapshot unavailable: changes could not be recorded)"]
    if modified:
        for x in ag.extracted:  # a value from modified code is shown, but never counts as a faithful re-run
            x["faithful"] = False
    why = ag.note or ag.stop_reason or ag.status
    cmp = compare(targets, ag.extracted, ag.outputs, why_none=f"{ag.status}: {why}"[:300], modified=modified)
    shipped = as_shipped(ag.commands, ag.outputs, names)
    status, reason = final_status(ag, targets, cmp, shipped)
    if status in ("fails_as_shipped", "missing_data") and not ag.extracted:  # say why the targets have no value
        cmp = compare(targets, [], ag.outputs, why_none=f"{status}: {reason}"[:300], modified=modified)
    tail = mask_secrets(ag.outputs[-1][-3000:] if ag.outputs else "", known)
    rep = {"status": status, "reason": reason[:400], "agent_status": ag.status, "agent_note": ag.note,
           "commands": ag.commands, "modifications": ag.modifications, "modified": bool(modified),
           "snapshot": ag.snapshot, "as_shipped": shipped, "stdout_tail": tail, "extracted": ag.extracted,
           "targets": targets, "minutes": minutes, "install_failed": False, "eur": round(ag.eur, 4),
           "steps": ag.steps, "calls": ag.calls, "stop_reason": ag.stop_reason,
           "network": "providers" if hosts else "off", **common}
    return rep, cmp


def install_error(log: str) -> str:
    """The line that says why phase A failed (pip's ERROR line, a timeout, or the failing step)."""
    lines = [ln.strip() for ln in log.splitlines() if ln.strip()]
    for pat in (r"^ERROR: (?!pip's dependency resolver)", r"INSTALL_TIMEOUT", r"(?i)error[:\s]", r"^\+ .*pip install"):
        hits = [ln for ln in lines if re.search(pat, ln)]
        if hits:
            return hits[-1][:300]
    return ""
