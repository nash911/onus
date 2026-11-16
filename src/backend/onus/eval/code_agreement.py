"""Agreement of the code checks (code-v1) with the split's audit labels. (Phase 10)

Not a harness module (not in the harness fingerprint): it reads code-check results and labels side by side, so
codecheck/ never touches labels (hard rule 2).

- Static: Onus's paper-level verdict (code.static[0], kind "paper_level": mismatch / consistent / not_checkable)
  against the conference code audit (PARTIAL, CRITICAL, FAILED = mismatch; PASSED = consistent; ERROR,
  NO CODEBASE, missing = excluded). Unlike the frozen harness's `code_agreement` (eval/metrics.py), which counts
  any static result that is not a mismatch as consistent, `not_checkable` is reported separately here.
- Re-runs: the re-run status against our own execution test (code_tests.csv grade, papers.csv code_exec_tier):
  did the code run in the sandbox when the earlier test says it runs?

    python -m onus.eval.code_agreement --split-root data/dev --results var/results/dev_code [--out file.md]
Per-paper rows are printed: use it on dev only. On held-out splits pass --aggregate (counts only).
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from onus.schemas.result import RunResult

from .truth import read_labels

# The submitted (unmodified) code ran: a verified value, or an entry-point run that exited 0 (as_shipped "runs").
RAN = {"consistent", "inconsistent", "unverified", "ran_no_values", "reproduced", "runs"}


def ran_as_shipped(rr: dict) -> bool:
    return (rr.get("as_shipped") or {}).get("outcome") == "runs" or rr["status"] in RAN


def paper_level(r: RunResult) -> str | None:
    pl = next((s for s in r.code.static if s.get("kind") == "paper_level"), None)
    return pl.get("status") if pl else None


def agreement(results: list[RunResult], split_root: Path) -> dict:
    labels = read_labels(split_root)
    rows, pairs, rr_pairs = [], [], []
    for r in sorted(results, key=lambda x: x.paper["id"]):
        pid = r.paper["id"]
        if not r.code.available:
            continue
        y = labels.audit_class(pid)
        p = paper_level(r)
        if y is not None and p in ("mismatch", "consistent"):
            pairs.append((int(p == "mismatch"), y))
        rr = r.code.reruns[0] if r.code.reruns else None
        tier = labels.papers.get(pid, {}).get("code_exec_tier", "")
        grade = labels.code_tests.get(pid, {}).get("grade", "") or labels.papers.get(pid, {}).get("code_test_grade", "")
        if rr is not None:
            rr_pairs.append((ran_as_shipped(rr), tier == "executable"))
        rows.append({"paper": pid, "audit": labels.audits.get(pid, "") or "-", "static": p or "not run",
                     "core p/a/c": "/".join(str(r.code.static[0].get(k, 0)) for k in ("present", "absent",
                                                                                      "contradicts"))
                     if p else "-",
                     "tier": tier or "-", "grade": grade or "-", "rerun": rr["status"] if rr else "-",
                     "as_shipped": ((rr.get("as_shipped") or {}).get("outcome") or "-") if rr else "-",
                     "rerun_minutes": rr.get("minutes") if rr else None,
                     "code_rerun": ",".join(c["outcome"] for c in rr.get("comparisons", [])) if rr else "-",
                     "verdicts_changed": r.summary.get("code", {}).get("verdicts_changed", 0)})
    cm = Counter(pairs)
    out = {"rows": rows, "static_n": len(pairs),
           "static_accuracy": (sum(1 for a, b in pairs if a == b) / len(pairs)) if pairs else None,
           "static_confusion": {"pred_mismatch_audit_mismatch": cm[(1, 1)], "pred_mismatch_audit_passed": cm[(1, 0)],
                                "pred_ok_audit_mismatch": cm[(0, 1)], "pred_ok_audit_passed": cm[(0, 0)]},
           "static_not_checkable_labelled": sum(1 for r in rows if r["static"] == "not_checkable"
                                                and r["audit"] not in ("-", "ERROR", "NO CODEBASE")),
           "rerun_n": len(rr_pairs), "rerun_ran": sum(a for a, _ in rr_pairs),
           "rerun_ran_of_executable": sum(1 for a, b in rr_pairs if a and b),
           "rerun_executable": sum(1 for _, b in rr_pairs if b)}
    return out


def render(a: dict, aggregate: bool = False) -> str:
    lines = []
    if not aggregate:
        cols = ["paper", "audit", "static", "core p/a/c", "tier", "grade", "rerun", "as_shipped", "code_rerun",
                "rerun_minutes", "verdicts_changed"]
        lines += ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
        lines += ["| " + " | ".join("-" if r[c] is None else str(r[c]) for c in cols) + " |" for r in a["rows"]]
        lines.append("")
    acc = a["static_accuracy"]
    c = a["static_confusion"]
    lines.append(f"Static vs audit: {a['static_n']} labelled papers with a paper-level verdict"
                 + (f", agreement {acc:.0%}" if acc is not None else "") + f" (mismatch/mismatch {c['pred_mismatch_audit_mismatch']}, "
                 f"mismatch/passed {c['pred_mismatch_audit_passed']}, consistent/mismatch {c['pred_ok_audit_mismatch']}, "
                 f"consistent/passed {c['pred_ok_audit_passed']}); {a['static_not_checkable_labelled']} labelled "
                 "paper(s) not checkable.")
    lines.append(f"Re-runs: the submitted code ran in {a['rerun_ran']} of {a['rerun_n']}; "
                 f"{a['rerun_ran_of_executable']} of {a['rerun_executable']} executable-tier papers ran.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--split-root", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--aggregate", action="store_true", help="counts only, no per-paper rows")
    args = ap.parse_args(argv)
    results = [RunResult.model_validate_json(p.read_text()) for p in sorted(args.results.glob("*.json"))]
    text = render(agreement(results, args.split_root), args.aggregate)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")


if __name__ == "__main__":
    main()
