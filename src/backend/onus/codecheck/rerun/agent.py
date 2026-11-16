"""Tool-loop agent that produces values for target claims (F15, code-v1). (Phase 10)

The judge model drives four tools inside the phase-B sandbox (list_dir, read_file ≤ read_max_lines lines,
run with timeout ≤ run_max_timeout_s, write_file recorded as a modification with a diff) plus `finish`. At most
max_steps tool calls, a wall-clock deadline (ONUS_RERUN_MAX_MINUTES for the whole paper) and an own spend cap
(max_agent_eur). Every tool output is masked (keys) before the model sees it; the full output of every `run` is
kept so compare.py can verify the cited lines. Older tool outputs are shortened so the context stays bounded.

Targets: only result/comparison claims whose number check resolved table cells (the claimed value is that
number); main claims first. A paper with none still gets a short run: does the entry point run as submitted?

Receipt integrity (code, not model):
- /work is committed to a git snapshot before the agent starts. After every `run` and `write_file`, the diff
  against that snapshot is recorded as `modifications`, so edits made through `run` (heredocs, sed -i) count too.
  Files the code writes (outputs) are not modifications; new files count when they are source files or were
  written with write_file.
- Every command records whether the code was already modified when it started, whether it ran one of the
  submission's own entry points, and its first error line, so run.py can tell "fails as shipped" from "the agent
  gave up".
"""

from __future__ import annotations

import difflib
import json
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from onus.codecheck.common import mask_secrets
from onus.codecheck.rerun.sandbox import Sandbox
from onus.llm.client import LLMError
from onus.schemas.result import RunResult

TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "List a directory under /work.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read lines start..end (1-based) of a file.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer"},
                                                     "end": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "run", "description": "Run a shell command in /work.",
     "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}, "timeout": {"type": "integer"}},
                    "required": ["cmd"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or replace a file (recorded).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "finish", "description": "Stop and report the values obtained.",
     "parameters": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["reproduced", "runs", "ran_no_values", "failed", "missing_data",
                                               "needs_external"]},
         "extracted": {"type": "array", "items": {"type": "object", "properties": {
             "claim_id": {"type": "string"}, "value": {"type": "number"}, "source_line": {"type": "string"},
             "faithful": {"type": "boolean"}}, "required": ["claim_id", "value", "source_line"]}},
         "note": {"type": "string"}}, "required": ["status"]}}},
]


def select_targets(result: RunResult, k: int) -> list[dict]:
    """Up to k result/comparison claims whose number check resolved table cells: main claims first, then paper
    order. Claims without resolved cells are never targets (a number in a methods sentence, a CI level or a
    timing is not a reported result that a re-run can reproduce)."""
    cands = []
    for c in result.claims:
        if c.type not in ("result", "comparison"):
            continue
        num = next((r for r in c.checks if r.check == "number"), None)
        resolved = [n for n in (num.data.get("numbers", []) if num else []) if isinstance(n, dict) and n.get("cells")
                    and n.get("claimed") is not None]
        if resolved:
            n0 = resolved[0]
            cands.append((not c.is_main, c.location.start, {"claim_id": c.id, "quote": c.quote,
                                                            "value": n0["claimed"], "text": n0["text"],
                                                            "cells": n0.get("cells", [])}))
    cands.sort(key=lambda x: x[:2])
    return [c[2] for c in cands[:k]]


# ---------------------------------------------------------------- snapshot and command classification

BASE_REF = "onus-submitted"
SNAPSHOT_CMD = (f"git init -q . && git add -A -f . && git -c user.name=onus -c user.email=onus@localhost commit -q "
                f"--no-verify --allow-empty -m submitted && git tag -f {BASE_REF} >/dev/null && echo SNAPSHOT_OK")
ERROR_LINE = re.compile(r"^\s*(?:[\w.]+(?:Error|Exception|Exit)|Error|ERROR|Traceback|Killed|Segmentation fault)\b.*",
                        re.MULTILINE)
EXC_LINE = re.compile(r"^\s*[\w.]+(?:Error|Exception)\b:.*", re.MULTILINE)
_SEP = r"(?:^|[\s;&|(])"
_ARG = r"([^\s;&|<>()'\"]+)"
ENTRY = [re.compile(_SEP + r"(?:python[\d.]*|pypy3?)\s+(?:-[a-zA-Z]+\s+)*" + _ARG + r"\.py\b"),
         re.compile(_SEP + r"(?:bash|sh)\s+" + _ARG + r"\.sh\b"),
         re.compile(_SEP + r"\./" + _ARG),
         re.compile(_SEP + r"Rscript\s+" + _ARG + r"\.[Rr]\b"),
         re.compile(r"--execute\s+(?:--\S+\s+)*" + _ARG + r"\.ipynb\b"),
         re.compile(_SEP + r"(?:make)\b()")]
INTERPRETER = re.compile(_SEP + r"(?:python[\d.]*|pypy3?|bash|sh|Rscript|jupyter)\b")
SCRIPT_TOKEN = re.compile(r"[\w./-]+\.(?:py|sh|R|ipynb)\b")
PY_MODULE = re.compile(_SEP + r"python[\d.]*\s+(?:-[a-zA-Z]+\s+)*-m\s+([\w.]+)")


def entry_names(files: list[str]) -> set[str]:
    """Basenames (and module names) of the submission's own runnable files."""
    out: set[str] = set()
    for f in files:
        p = PurePosixPath(f)
        if p.suffix in (".py", ".sh", ".R", ".r", ".ipynb") or p.name == "Makefile":
            out |= {p.name, p.stem}
        out |= set(p.parent.parts)
    return out


def runs_entry(cmd: str, names: set[str]) -> bool:
    """True when the command runs one of the submission's own scripts (not a heredoc or helper of the agent's)."""
    if "<<" in cmd and not any(rx.search(cmd.split("<<")[0]) for rx in ENTRY[:5]):
        cmd = cmd.split("<<")[0]
    for rx in ENTRY:
        for m in rx.finditer(cmd):
            arg = m.group(1)
            if rx is ENTRY[-1]:
                if "Makefile" in names:
                    return True
                continue
            if PurePosixPath(arg).name in names or PurePosixPath(arg).stem in names:
                return True
    if any(m.group(1).split(".")[0] in names or m.group(1).split(".")[-1] in names for m in PY_MODULE.finditer(cmd)):
        return True
    # Indirect form (a shell loop, `python "$f"`): an interpreter is called and a submission script is named.
    return bool(INTERPRETER.search(cmd)) and any(PurePosixPath(t).name in names for t in SCRIPT_TOKEN.findall(cmd))


def error_line(output: str) -> str:
    """The line that names the error: the last `XxxError: ...` line, else the last error-looking line."""
    m = EXC_LINE.findall(output) or ERROR_LINE.findall(output)
    if m:
        return m[-1].strip()[:300]
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


@dataclass
class AgentRun:
    status: str = "failed"
    note: str = ""
    # {cmd, rc, seconds, timed_out, entry, modified_before, error_line}
    commands: list[dict] = field(default_factory=list)
    modifications: list[dict] = field(default_factory=list)  # {path, change, diff}, against the snapshot
    outputs: list[str] = field(default_factory=list)  # full (masked) output of every run
    extracted: list[dict] = field(default_factory=list)
    steps: int = 0
    eur: float = 0.0
    calls: int = 0
    stop_reason: str = ""
    snapshot: bool = False  # the git snapshot exists, so `modifications` is complete
    written: set[str] = field(default_factory=set)  # paths written with write_file


def _clip(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    head = n // 4
    return s[:head] + f"\n...[{len(s) - n} chars cut]...\n" + s[-(n - head):]


async def snapshot(sb: Sandbox) -> bool:
    r = await sb.exec(SNAPSHOT_CMD, 300)
    return r.rc == 0 and "SNAPSHOT_OK" in r.output


async def record_changes(sb: Sandbox, out: AgentRun, cfg: dict, known: list[str]) -> None:
    """Diff /work against the snapshot: changed or deleted files, plus new source files and files written with
    write_file. Replaces out.modifications (a file changed back to its original drops out, but a command that ran
    while it was changed keeps modified_before=True)."""
    if not out.snapshot:
        return
    exts = set(cfg.get("modification_exts") or ())
    r = await sb.exec("git add -u . >/dev/null 2>&1; git -c core.quotepath=off ls-files --others .", 120)
    new = [f for f in r.output.splitlines() if f and (PurePosixPath(f).suffix in exts
                                                     or "/work/" + f in out.written)]
    add = ("git add -f -- " + " ".join(shlex.quote(f) for f in new[:200]) + " >/dev/null 2>&1; ") if new else ""
    r = await sb.exec(add + f"git -c core.quotepath=off diff --cached --no-renames --name-status {BASE_REF}", 120)
    if r.rc != 0:
        out.snapshot = False  # the agent removed or rewrote the snapshot: modifications can no longer be proven
        return
    changed = []
    for ln in r.output.splitlines():
        parts = ln.split("\t", 1)
        if len(parts) == 2 and parts[0][:1] in "MADT":
            changed.append(({"M": "modified", "A": "added", "D": "deleted", "T": "modified"}[parts[0][0]], parts[1]))
    mods = []
    per_file = int(cfg.get("diff_chars_per_file", 4000))
    for change, path in changed[:50]:
        d = await sb.exec(f"git -c core.quotepath=off diff --cached --no-renames -U1 {BASE_REF} -- "
                          f"{shlex.quote(path)} | head -c {per_file}", 120)
        mods.append({"path": path, "change": change, "diff": mask_secrets(d.output, known)[:per_file]})
    if len(changed) > 50:
        mods.append({"path": f"... and {len(changed) - 50} more files", "change": "modified", "diff": ""})
    out.modifications = mods


async def run_agent(sb: Sandbox, llm, *, system: str, targets: list[dict], paper_id: str, deadline: float,
                    cfg: dict, known_secrets: list[str], entries: set[str] | None = None) -> AgentRun:
    out = AgentRun()
    out.snapshot = await snapshot(sb)
    max_steps, cap = int(cfg["max_steps"]), float(cfg["max_agent_eur"])
    out_chars, keep = int(cfg["tool_output_chars"]), int(cfg["keep_full_steps"])
    elided = int(cfg["elided_output_chars"])
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": "Start."}]
    tool_msgs: list[int] = []
    target_ids = {t["claim_id"] for t in targets}
    while True:
        if out.steps >= max_steps:
            out.stop_reason = f"step limit ({max_steps})"
            break
        if time.monotonic() > deadline:
            out.stop_reason = "time limit"
            break
        if out.eur >= cap:
            out.stop_reason = f"agent spend cap (€{cap:.2f})"
            break
        for i in tool_msgs[:-keep] if keep else tool_msgs:  # shorten older tool outputs
            c = messages[i]["content"]
            if len(c) > elided:
                messages[i] = messages[i] | {"content": c[:elided] + " ...[older output shortened]"}
        try:
            resp, meta = await llm.complete_tools("judge", messages, TOOLS, prompt_id="rerun_agent",
                                                  paper_id=paper_id, max_tokens=4000)
        except LLMError as e:
            out.stop_reason = f"model error: {str(e)[:200]}"
            break
        out.eur += meta.eur
        out.calls += 1
        calls = resp.get("tool_calls") or []
        if not calls:
            out.stop_reason = "the agent stopped without calling finish"
            out.note = (resp.get("content") or "")[:500]
            break
        messages.append({"role": "assistant", "content": resp.get("content") or "",
                         "tool_calls": [{"id": c["id"], "type": "function",
                                         "function": {"name": c["name"], "arguments": _args_text(c["arguments"])}}
                                        for c in calls]})
        finished = False
        for c in calls:
            out.steps += 1
            args = _args(c["arguments"])
            name = c["name"]
            if name == "finish":
                out.status = str(args.get("status") or "failed")
                out.note = mask_secrets(str(args.get("note") or ""), known_secrets)[:500]
                for x in args.get("extracted") or []:
                    if isinstance(x, dict) and x.get("claim_id") in target_ids:
                        out.extracted.append({"claim_id": x["claim_id"], "value": x.get("value"),
                                              "source_line": mask_secrets(str(x.get("source_line") or ""),
                                                                          known_secrets)[:400],
                                              "faithful": bool(x.get("faithful", True))})
                result = "ok"
                finished = True
            else:
                remaining = max(30, int(deadline - time.monotonic()) - 30)
                result = await _tool(sb, name, args, out, cfg, remaining, known_secrets, entries or set())
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": _clip(result, out_chars)})
            tool_msgs.append(len(messages) - 1)
            if finished or out.steps >= max_steps:
                break
        if finished:
            out.stop_reason = "finish"
            break
    await record_changes(sb, out, cfg, known_secrets)
    return out


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return out if isinstance(out, dict) else {}


def _args_text(raw) -> str:
    return raw if isinstance(raw, str) and raw else json.dumps(raw or {})


def _path(p: str) -> str:
    p = (p or ".").strip()
    if p in (".", "./", ""):
        return "/work"
    return p if p.startswith("/") else "/work/" + p.removeprefix("./")


async def _tool(sb: Sandbox, name: str, args: dict, out: AgentRun, cfg: dict, remaining_s: int, known: list[str],
                entries: set[str]) -> str:
    if name == "list_dir":
        r = await sb.exec(f"ls -la {shlex.quote(_path(args.get('path', '.')))} | head -200", 60)
        return r.output
    if name == "read_file":
        start = max(1, int(args.get("start") or 1))
        end = int(args.get("end") or start + int(cfg["read_max_lines"]) - 1)
        end = min(end, start + int(cfg["read_max_lines"]) - 1)
        r = await sb.exec(f"sed -n '{start},{end}p' {shlex.quote(_path(args.get('path', '')))} | cut -c1-400", 60)
        return r.output or "(empty)"
    if name == "run":
        cmd = str(args.get("cmd") or "").strip()
        if not cmd:
            return "error: empty command"
        t = min(int(args.get("timeout") or cfg["run_max_timeout_s"]), int(cfg["run_max_timeout_s"]), remaining_s)
        modified_before = bool(out.modifications) or not out.snapshot or any(c.get("modified_after")
                                                                             for c in out.commands)
        r = await sb.exec("export MPLBACKEND=Agg PYTHONUNBUFFERED=1 PYTHONPATH=/work:$PYTHONPATH; " + cmd, t)
        await record_changes(sb, out, cfg, known)
        out.commands.append({"cmd": mask_secrets(cmd, known)[:500], "rc": r.rc, "seconds": round(r.seconds, 1),
                             "timed_out": r.timed_out, "entry": runs_entry(cmd, entries),
                             "modified_before": modified_before, "modified_after": bool(out.modifications),
                             "error_line": mask_secrets(error_line(r.output), known) if r.rc != 0 else ""})
        out.outputs.append(r.output)
        return f"exit code {r.rc}{' (timed out)' if r.timed_out else ''}\n{r.output}"
    if name == "write_file":
        path = _path(args.get("path", ""))
        content = str(args.get("content") or "")
        try:
            old = await sb.read_text(path)
        except Exception:  # noqa: BLE001 - a new file
            old = ""
        await sb.write_text(path, content)
        out.written.add(path)
        if out.snapshot:
            await record_changes(sb, out, cfg, known)
        else:  # no snapshot: fall back to recording the write itself
            diff = "".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True), fromfile=path,
                                                tofile=path, n=1))
            out.modifications.append({"path": path.removeprefix("/work/"), "change": "modified" if old else "added",
                                      "diff": mask_secrets(diff, known)[:4000]})
        return f"wrote {path} ({len(content)} chars)"
    return f"error: unknown tool {name}"
