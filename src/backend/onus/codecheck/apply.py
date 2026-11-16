"""Attach code-check results to a stored RunResult and recompute verdicts with the frozen rules. (Phase 10)

- Static facts become `code_static` checks on related claims (code decides the relation: the fact's value
  appears in the claim, or they share `min_shared_terms` content words):
  - method claims: fail/contradicts if a related fact is contradicted by cited code, pass if every related fact
    is present, inconclusive otherwise; cited lines are added as `code` evidence;
  - result and comparison claims: only a contradiction is attached (the code implementing the setup says
    nothing about a reported number).
- Re-run comparisons (rerun/compare.py) become `code_rerun` checks on their target claims, with the verified
  output line as `rerun_output` evidence.
- Verdicts: the frozen `verdict.rules.rule_verdict` is re-applied to each claim's evidence and checks. When it
  returns the stored rule verdict the claim keeps its (possibly adjudicated) verdict and reason; when the code
  checks change it, the new rule verdict and its reason replace them, and the adjudication record is marked
  superseded. Nothing is sent to the adjudicator (no new P0 model calls). Checklist claims are unchanged.
- Ledger entries for code_static / code_rerun, the "Submitted code" not-checked row, the summary (plus
  summary["code"]) and RunResult.code are rewritten; code_version = "<CODE_VERSION>+<code fingerprint>".
"""

from __future__ import annotations

import re
from collections import Counter

from onus.codecheck.static import _NUM, _WORD, STOP, Fact, StaticReport
from onus.pipeline import summarise
from onus.schemas.document import Document
from onus.schemas.result import (
    RISK,
    CheckResult,
    Claim,
    CodeReport,
    Evidence,
    LedgerEntry,
    NotChecked,
    RunResult,
)
from onus.verdict.rules import rule_verdict

CODE_CHECKS = ("code_static", "code_rerun")


def _content_words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text) if len(w) >= 4 and w.lower() not in STOP
            and not re.fullmatch(r"\d+(?:\.\d+)?", w)}


def _numbers(text: str) -> set[float]:
    out = set()
    for m in _NUM.finditer(text.replace(",", "")):
        try:
            out.add(float(m.group(0)))
        except ValueError:
            pass
    return out


def related(f: Fact, claim: Claim, min_shared: int) -> bool:
    q = claim.quote.lower()
    if f.start < claim.location.end and f.end > claim.location.start:
        return True  # the fact's own sentence is the claim
    if f.value:
        vn = _numbers(f.value)
        if vn and vn <= _numbers(claim.quote) and any(abs(n) not in (0.0, 1.0, 2.0) for n in vn):
            return True
        v = f.value.strip().lower()
        if not vn and len(v) >= 4 and v in q:
            return True
    return len(_content_words(f.statement + " " + f.value) & _content_words(claim.quote)) >= min_shared


def own_dangling(doc: Document | None, c: Claim) -> list[str]:
    if doc is None:
        return []
    s, e = c.location.start, c.location.end
    return list(dict.fromkeys(m.label for m in doc.mentions if s <= m.start < e and m.resolved_id is None))


def _next_ev(claims: list[Claim]) -> int:
    n = 0
    for c in claims:
        for e in c.evidence:
            if m := re.fullmatch(r"ev(\d+)", e.id):
                n = max(n, int(m.group(1)))
    return n


def static_checks(claim: Claim, facts: list[Fact], min_shared: int, new_id,
                  claim_types: tuple[str, ...] = ("method", "result", "comparison")) -> tuple[CheckResult | None,
                                                                                             list[Evidence]]:
    if claim.type not in claim_types:
        return None, []
    rel = [f for f in facts if f.status in ("present", "absent", "contradicts", "unverified")
           and related(f, claim, min_shared)]
    if not rel:
        return None, []
    contra = [f for f in rel if f.status == "contradicts"]
    is_method = claim.type == "method"
    if not is_method and not contra:
        return None, []
    ev: list[Evidence] = []
    for f in (rel if is_method else contra):
        for c in f.citations:
            ev.append(Evidence(id=new_id(), kind="code", label=f"{c['path']}:{c['line']}", snippet=c["text"],
                               link_method="code_search",
                               relation="contradicts" if f.status == "contradicts" else "supports"))
    data = {"facts": [{"fact_id": f.id, "kind": f.kind, "status": f.status, "value": f.value} for f in rel]}
    ids = [e.id for e in ev]
    if contra:
        f = contra[0]
        where = ", ".join(f"{c['path']}:{c['line']}" for c in f.citations[:2])
        detail = f"The submitted code differs from the paper: {f.statement} ({f.note or 'see ' + where})"
        return CheckResult(check="code_static", outcome="fail", severity="contradicts", detail=detail.strip(),
                           data=data, evidence_ids=ids), ev
    if all(f.status == "present" for f in rel):
        where = ", ".join(e.label for e in ev[:3])
        return CheckResult(check="code_static", outcome="pass", detail=f"The submitted code implements this "
                           f"({where}).", data=data, evidence_ids=ids), ev
    missing = [f for f in rel if f.status != "present"]
    return CheckResult(check="code_static", outcome="inconclusive", detail=f"{len(missing)} of {len(rel)} related "
                       "implementation fact(s) could not be found in the submitted code.", data=data,
                       evidence_ids=ids), ev


def rerun_check(cmp: dict, new_id) -> tuple[CheckResult, list[Evidence]]:
    ev = []
    if cmp.get("source_line"):
        rel = {"pass": "supports", "fail": "contradicts"}.get(cmp["outcome"], "unrelated")
        ev.append(Evidence(id=new_id(), kind="rerun_output", label="re-run output", snippet=cmp["source_line"][:300],
                           link_method="sandbox", relation=rel))  # type: ignore[arg-type]
    sev = "contradicts" if cmp["outcome"] == "fail" else "none"
    return CheckResult(check="code_rerun", outcome=cmp["outcome"], severity=sev, detail=cmp["detail"],  # type: ignore[arg-type]
                       data={k: v for k, v in cmp.items() if k not in ("outcome", "detail")},
                       evidence_ids=[e.id for e in ev]), ev


PAPER_LEVEL_STATUS = {True: "mismatch", False: "consistent", None: "not_checkable"}


def paper_level_entry(static: StaticReport) -> dict:
    """First item of RunResult.code.static: the paper-level verdict, in the vocabulary the frozen harness reads
    (eval/metrics.py: status "mismatch" makes a paper-level code mismatch)."""
    pl = static.paper_level
    return {"kind": "paper_level", "fact": f"Paper level: {pl.get('reason', '')}",
            "status": PAPER_LEVEL_STATUS[pl.get("mismatch")], "citations": [],
            **{k: pl.get(k) for k in ("core_facts", "present", "absent", "contradicts", "unverified", "facts")}}


def rerun_status(rep: dict, comparisons: list[dict]) -> str:
    """Top-level re-run status in the harness vocabulary (eval/metrics.py, eval/findings.py):
    - "inconsistent": a verified value from the unmodified code contradicts the paper;
    - "consistent": a verified value from the unmodified code matches and none contradicts;
    - "consistent_modified": a value matched only after the submitted code was modified (not counted as
      consistent by the harness);
    - otherwise the re-run's own status (fails_as_shipped, missing_data, install_failed, runs, failed, ...);
      "reproduced" without a verified value becomes "unverified"."""
    outs = [c["outcome"] for c in comparisons]
    if "fail" in outs:
        return "inconsistent"
    if "pass" in outs:
        return "consistent_modified" if rep.get("modified") else "consistent"
    st = rep.get("status") or "not_run"
    return "unverified" if st == "reproduced" else st


def display_rerun(rep: dict, comparisons: list[dict]) -> dict:
    cmds = [f"$ {c['cmd']}   # exit {c['rc']}, {c['seconds']} s{' (timed out)' if c.get('timed_out') else ''}"
            for c in rep.get("commands", [])]
    return rep | {"status": rerun_status(rep, comparisons), "run_status": rep.get("status"), "commands": cmds,
                  "command_log": rep.get("commands", []), "comparisons": comparisons}


def code_ledger(claims: list[Claim], static_reason: str | None, rerun_reason: str | None) -> list[LedgerEntry]:
    out = []
    for name, why in (("code_static", static_reason), ("code_rerun", rerun_reason)):
        ran = sum(1 for c in claims if any(r.check == name and r.outcome != "not_run" for r in c.checks))
        reasons: Counter = Counter()
        for c in claims:
            r = next((r for r in c.checks if r.check == name), None)
            if r is not None and r.outcome == "not_run":
                reasons[r.data.get("reason", r.detail) or "not run"] += 1
            elif r is None:
                reasons[why or ("checklist claim" if c.type == "checklist" else "no related code fact or target")] += 1
        out.append(LedgerEntry(check=name, ran_on=ran, not_run_on=sum(reasons.values()), reasons=dict(reasons)))
    return out


def apply_code(result: RunResult, doc: Document | None, static: StaticReport | None, reruns: list[dict],
               comparisons: list[dict], *, code_version: str, min_shared: int = 2,
               claim_types: tuple[str, ...] = ("method", "result", "comparison")) -> RunResult:
    """Return a new RunResult with code checks attached and verdicts recomputed. `result` must be the P0 result
    (no code checks yet)."""
    claims = [c.model_copy(deep=True) for c in result.claims]
    n = _next_ev(claims)

    def new_id() -> str:
        nonlocal n
        n += 1
        return f"ev{n}"

    facts = static.facts if static and static.status == "done" else []
    by_target: dict[str, dict] = {c["claim_id"]: c for c in comparisons}
    changed = 0
    for i, c in enumerate(claims):
        if c.type == "checklist":
            continue
        c.checks = [r for r in c.checks if r.check not in CODE_CHECKS]
        dangling = own_dangling(doc, c)
        before = rule_verdict(c.evidence, c.checks, dangling).verdict
        added = False
        if facts:
            chk, ev = static_checks(c, facts, min_shared, new_id, claim_types)
            if chk is not None:
                c.checks.append(chk)
                c.evidence += ev
                added = True
        if c.id in by_target:
            chk, ev = rerun_check(by_target[c.id], new_id)
            c.checks.append(chk)
            c.evidence += ev
            added = True
        if not added:
            continue
        rv = rule_verdict(c.evidence, c.checks, dangling)
        if rv.verdict == before:  # the code checks did not move the rule verdict: keep the stored verdict
            continue
        adj = (c.adjudication or {}) | {"superseded_by_code": True} if c.adjudication else None
        claims[i] = c.model_copy(update={"verdict": rv.verdict, "reason": rv.reason, "caveat": rv.caveat,
                                         "risk": RISK[rv.verdict], "rule_verdict": rv.verdict, "adjudication": adj})
        changed += 1
    claims.sort(key=lambda c: (-c.risk, c.location.start))

    static_reason = None
    if static is None:
        static_reason = "static code check not run"
    elif static.status != "done":
        static_reason = f"static code check not run: {static.reason}"
    rerun_reason = None if reruns else "no re-run for this paper"
    ledger = [e for e in result.ledger if e.check not in CODE_CHECKS] + code_ledger(claims, static_reason,
                                                                                    rerun_reason)
    nc = [x for x in result.not_checked if x.what != "Submitted code"]
    if result.code.available and (static is None or static.status != "done"):
        nc.append(NotChecked(what="Submitted code", reason=static_reason or "not checked", count=result.code.files))
    statics = ([paper_level_entry(static)] + [f.as_dict() for f in static.facts]
               if static and static.status == "done" else [])
    shown = [display_rerun(r, comparisons) for r in reruns]
    code = CodeReport(available=result.code.available, files=result.code.files, static=statics, reruns=shown,
                      code_version=code_version)
    summary = summarise(claims, result.checklist)
    summary["code"] = {
        "static": ({"status": static.status, "reason": static.reason, **static.paper_level,
                    "dropped": static.dropped} if static else {"status": "not_run"}),
        "reruns": [{"status": r["status"], "run_status": r.get("run_status"), "agent_status": r.get("agent_status"),
                    "minutes": r.get("minutes"),
                    "extracted": len(r.get("extracted", []))} for r in shown],
        "verdicts_changed": changed,
    }
    warnings = [w for w in result.warnings if not w.startswith("code:")]
    warnings += [f"code: {w}" for w in (static.warnings if static else [])]
    return result.model_copy(update={"claims": claims, "ledger": ledger, "not_checked": nc, "code": code,
                                     "summary": summary, "warnings": warnings})
