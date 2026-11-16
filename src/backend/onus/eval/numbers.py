"""Submission numbers: `onus export-numbers` (BUILD_PLAN Phase 12).

Reads the files the harness and the corpus step already wrote and lays every headline number out as small
Markdown tables, each with its 95% interval, sample size, source file and the command that produced it:

- metrics.json from `onus eval merge-metrics` (`{"splits": {name: metrics}, "pooled": {...}, "labels": ...}`);
  a single split's metrics.json from `onus eval` is accepted too;
- findings.json from `onus findings` (RQ1-RQ4);
- labels_scores.json from `onus labels score` (optional; precision is "not measured" without it).

No new arithmetic: values and intervals are copied from the inputs. Where an input gives a proportion without
an interval, a Wilson 95% interval is computed with `metrics.wilson` and marked †. Where n is not stored as
one number, it is the sum of stored counts and the table says which. This module is outside the harness
fingerprint (eval/{__init__,mutate,baseline,metrics,truth,harness}.py) and the pipeline fingerprint.
"""

from __future__ import annotations

import json
from pathlib import Path

from .metrics import wilson
from .mutate import TYPES

DAGGER = "†"
NA = "not applicable"


class NumbersError(ValueError):
    pass


# ---------------------------------------------------------------- inputs


def load_json(path: Path, what: str) -> dict:
    if not path.is_file():
        raise NumbersError(f"{what}: no file at {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise NumbersError(f"{what}: {path} is not JSON ({e})") from e
    if not isinstance(data, dict):
        raise NumbersError(f"{what}: {path} does not hold a JSON object")
    return data


def normalise_metrics(m: dict, name: str) -> dict:
    """A merge-metrics file as is; a single split's metrics.json wrapped in the same shape (no new numbers)."""
    if "splits" in m:
        if not isinstance(m["splits"], dict) or not m["splits"]:
            raise NumbersError("metrics: 'splits' is empty")
        return m
    if "distributions" not in m:
        raise NumbersError("metrics: neither a merge-metrics file ('splits') nor a split metrics.json "
                           "('distributions')")
    mu = m.get("mutations") or {}
    pooled = {"recall": {t: {s: (x or {}).get(s) for s in ("onus", "baseline")}
                         for t, x in (mu.get("recall") or {}).items()},
              "collateral": (mu.get("collateral") or {}).get("onus")} if mu else None
    return {"splits": {name: m}, "pooled": pooled, "labels": None, "_single": True}


def check_findings(f: dict) -> None:
    missing = [k for k in ("rq1", "rq2", "rq3", "rq4") if k not in f]
    if missing:
        raise NumbersError(f"findings: missing {', '.join(missing)} (expected the output of `onus findings`)")


def check_labels(lab: dict) -> None:
    if not isinstance(lab.get("systems"), dict):
        raise NumbersError("labels: no 'systems' (expected the output of `onus labels score`)")


# ---------------------------------------------------------------- formatting (no arithmetic beyond Wilson)


class Notes:
    def __init__(self) -> None:
        self.wilson_used = False


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.1%}"


def _num(x: float | None, nd: int = 2) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def _ci(ci, fmt=_pct) -> str:
    if not ci:
        return "—"
    return f"{fmt(ci[0])} – {fmt(ci[1])}"


def rate_cells(r: dict | None, notes: Notes, empty: str = NA) -> tuple[str, str, str]:
    """(value, 95% CI, n) for a stored {"k", "n", "rate", "ci95"} dict."""
    if not r or not r.get("n"):
        return empty, "—", "0" if r is not None else "—"
    k, n = r["k"], r["n"]
    ci = r.get("ci95")
    mark = ""
    if not ci:
        ci, mark = wilson(k, n), DAGGER
        notes.wilson_used = True
    rv = r.get("rate")
    val = f"{k}/{n} = {_pct(rv)}" if rv is not None else f"{k}/{n}"
    return val, _ci(ci) + mark, str(n)


def table(head: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _src(file: str, cmd: str) -> str:
    return f"Source: `{file}` · command: `{cmd}`"


# ---------------------------------------------------------------- commands that produced the inputs


def eval_cmd(name: str, m: dict) -> str:
    plan = m.get("mutation_plan")
    base = m.get("distributions", {}).get("baseline") is not None
    return (f"onus eval --split-root <{name} split root> --results <{name} results> --out <{name}/metrics>"
            + (f" --mutations {plan}" if plan else "") + (" --baseline" if base else ""))


def merge_cmd(split_names: list[str], has_labels: bool, out: str) -> str:
    return ("onus eval merge-metrics " + " ".join(f"<{s}/metrics>" for s in split_names)
            + (" --labels labels_scores.json" if has_labels else "") + f" --out {out}")


def findings_cmd(f: dict, out: str) -> str:
    names = list((f.get("splits") or {}).keys())
    return (f"onus findings --runs {' '.join(f'<{s} results>' for s in names)} "
            f"--split-roots {' '.join(f'<{s} split root>' for s in names)} --out {out} --seed {f.get('seed', 7)}")


# ---------------------------------------------------------------- sections


def how_to_read() -> str:
    return (
        "**How to read this.** Every number below is copied from the input files named under each table; nothing "
        "is re-estimated here. Proportions are shown as k/n = rate with a 95% interval: Wilson intervals for "
        "recall, flag rates, agreement and precision (as stored by the harness), bootstrap intervals for the corpus "
        "correlations and differences (as stored by `onus findings`). An interval marked † was not in the input and "
        "was computed here as a Wilson interval from the stored k and n. *n* is the number of units the proportion "
        "is over (mutations, claims, labelled flags or papers, as the column says); *papers* is how many distinct "
        "papers those units come from, which matters because units from one paper are not independent. Recall "
        "is read together with each system's flag rate on unmutated papers and its collateral flag rate: a system "
        "that flags everything has perfect recall. \"not applicable\" means no unit qualified (n = 0); "
        "\"not measured\" means the input that would carry the number was not given."
    )


def inputs_section(mm: dict, src: dict[str, str | None]) -> str:
    rows = []
    for name, m in mm["splits"].items():
        rows.append([name, f"`{m.get('fingerprint', '—')}`", f"`{m.get('harness_fingerprint') or '—'}`",
                     str(m.get("distributions", {}).get("papers", "—")),
                     ", ".join(str(x) for x in m.get("runs") or []) or "—", m.get("mutation_plan") or "none",
                     "yes" if m.get("distributions", {}).get("baseline") else "no", str(m.get("created_at", "—"))])
    fps = {m.get("fingerprint") for m in mm["splits"].values()}
    hfps = {m.get("harness_fingerprint") for m in mm["splits"].values()}
    warn = []
    if len(fps) > 1:
        warn.append("**The splits were analysed with different pipeline fingerprints.**")
    if len(hfps) > 1:
        warn.append("**The splits were evaluated with different harness fingerprints.**")
    lines = ["## Inputs", "",
             table(["Split", "Pipeline fingerprint", "Harness fingerprint", "Papers", "Runs per paper",
                    "Mutation plan", "Baseline", "Evaluated at"], rows), ""]
    lines += warn + ([""] if warn else [])
    lines += [f"- metrics: `{src['metrics']}`", f"- findings: `{src['findings']}`",
              f"- labels: `{src['labels']}`" if src.get("labels") else "- labels: not given"]
    return "\n".join(lines)


def recall_section(mm: dict, src: dict, notes: Notes) -> str:
    pooled = (mm.get("pooled") or {}).get("recall") or {}
    has_base = any(m.get("distributions", {}).get("baseline") is not None for m in mm["splits"].values())
    rows = []
    for t in (*TYPES, "all"):
        x = pooled.get(t) or {}
        o = rate_cells(x.get("onus"), notes)
        b = rate_cells(x.get("baseline"), notes) if has_base else ("baseline not run", "—", "—")
        papers = (x.get("onus") or {}).get("papers")
        rows.append(["**all types**" if t == "all" else t, *o, *b, str(papers) if papers else "—"])
    single = mm.get("_single")
    cmd = eval_cmd(*next(iter(mm["splits"].items()))) if single else \
        merge_cmd(list(mm["splits"]), mm.get("labels") is not None, src["metrics"])
    where = "mutations.recall" if single else "pooled.recall (summed over splits)"
    per = []
    for name, m in mm["splits"].items():
        rec = (m.get("mutations") or {}).get("recall") or {}
        for t in (*TYPES, "all"):
            if t in rec:
                x = rec[t]
                per.append([name, "**all types**" if t == "all" else t, str(x.get("attempted", "—")),
                            str(x.get("applied", "—")), str(x.get("target_missing", "—")), str(x.get("papers", "—"))])
    out = ["## 1. Recall on planted errors", "",
           table(["Mutation type", "Onus recall", "95% CI", "n", "Baseline recall", "95% CI", "n", "papers"], rows),
           "", _src(f"{src['metrics']} → {where}", cmd)]
    if per:
        out += ["", ("Mutations per split (attempted = planned, applied = preconditions held, target not extracted = "
                "the claim at the mutation was not extracted from the mutated paper):"), "",
                table(["Split", "Mutation type", "Attempted", "Applied", "Target not extracted", "papers"], per),
                "", _src(f"{src['metrics']} → splits.<split>.mutations.recall", "onus eval … --mutations NxK")]
    return "\n".join(out)


def flag_section(mm: dict, src: dict, notes: Notes) -> str:
    rows, col = [], []
    for name, m in mm["splits"].items():
        d = m.get("distributions") or {}
        papers = str(d.get("papers", "—"))
        rows.append([name, "Onus", "claims flagged (contradicted / overstated / untraced)",
                     *rate_cells(d.get("flagged"), notes), papers])
        rows.append([name, "Onus", "main claims flagged", *rate_cells(d.get("main_flagged"), notes), papers])
        b = d.get("baseline")
        if b:
            rows.append([name, "Baseline", "claims judged not_supported", *rate_cells(b.get("not_supported"), notes),
                         str(b.get("papers", "—"))])
        mc = (m.get("mutations") or {}).get("collateral")
        if mc:
            for system in ("onus", "baseline"):
                x = mc.get(system)
                if x is not None:
                    col.append([name, "Onus" if system == "onus" else "Baseline", *rate_cells(x, notes),
                                str(x.get("papers", "—"))])
    pc = (mm.get("pooled") or {}).get("collateral")
    if pc and not mm.get("_single"):
        col.append(["all splits (pooled)", "Onus", *rate_cells(pc, notes), str(pc.get("papers", "—"))])
    out = ["## 2. Flag rate on unmutated papers and collateral flags", "",
           table(["Split", "System", "What is counted", "Flag rate", "95% CI", "n (claims)", "papers"], rows), "",
           _src(f"{src['metrics']} → splits.<split>.distributions", "onus eval --split-root … --results …")]
    out += ["", ("Collateral flag rate: flags in a mutated run that are not at the mutation and were absent from the "
            "original run, over all non-target claims of the mutated runs."), ""]
    out += [table(["Split", "System", "Collateral rate", "95% CI", "n (claims)", "papers"], col) if col
            else "not measured (no mutation runs in the metrics)"]
    out += ["", _src(f"{src['metrics']} → splits.<split>.mutations.collateral, pooled.collateral",
                     "onus eval … --mutations NxK [--baseline]; pooled by onus eval merge-metrics")]
    return "\n".join(out)


def precision_section(lab: dict | None, lab_src: str | None, notes: Notes) -> str:
    head = ["## 3. Precision (blind hand labels)", ""]
    if lab is None:
        return "\n".join(head + [("not measured (no labels_scores.json given: pass `--labels`, or merge it into the "
                                 "metrics with `onus eval merge-metrics … --labels`)")])
    rows = []
    for system, by in sorted(lab["systems"].items()):
        for verdict in sorted(by, key=lambda v: (v == "all", v)):
            x = by[verdict]
            c, w, u = x.get("correct", 0), x.get("incorrect", 0), x.get("unsure", 0)
            n = c + w
            ci = x.get("ci95")
            mark = ""
            if not ci and n:
                ci, mark = wilson(c, n), DAGGER
                notes.wilson_used = True
            p = x.get("precision")
            rows.append([system, "**all**" if verdict == "all" else verdict,
                         f"{c}/{n} = {_pct(p)}" if p is not None else NA, _ci(ci) + mark, str(n), str(c), str(w),
                         str(u)])
    return "\n".join(head + [
        table(["System", "Verdict", "Precision", "95% CI", "n (correct + incorrect)", "correct", "incorrect",
               "unsure (left out)"], rows), "",
        _src(f"{lab_src} → systems", "onus labels score --in labels_done.csv --key labels_todo.key.json "
             "--out labels_scores.json")])


def stability_section(mm: dict, src: dict, notes: Notes) -> str:
    rows = []
    for name, m in mm["splits"].items():
        st = m.get("stability") or {}
        if st.get("na"):
            rows.append([name, str(st.get("runs", 1)), st["na"], "—", "—", "—", "—"])
            continue
        v, ci, n = rate_cells(st.get("full_agreement"), notes)
        rows.append([name, str(st.get("runs")), v, ci, n, str(st.get("papers", "—")), _num(st.get("mean_agreement"))])
    return "\n".join(["## 4. Run-to-run stability", "",
                      ("Share of claims whose verdict was the same in every run, and the mean share of runs agreeing "
                      "with the final verdict."), "",
                      table(["Split", "Runs per paper", "Claims with full agreement", "95% CI", "n (claims)", "papers",
                             "Mean agreement"], rows), "",
                      _src(f"{src['metrics']} → splits.<split>.stability", "onus analyze --runs 3, then onus eval")])


CODE_COUNT_KEYS = ("code_checks", "codecheck", "code")


def code_section(mm: dict, f: dict, src: dict) -> str:
    rows, counts = [], []
    for name, m in mm["splits"].items():
        ca = m.get("code_agreement") or {}
        if ca.get("na") or not ca.get("n"):
            rows.append([name, ca.get("na") or NA, "—", "—", "—", str(ca.get("labelled", "—")),
                         str(ca.get("excluded", "—"))])
        else:
            c = ca.get("confusion") or {}
            conf = "/".join(str(c.get(k, "—")) for k in ("pred_mismatch_audit_mismatch", "pred_mismatch_audit_passed",
                                                         "pred_ok_audit_mismatch", "pred_ok_audit_passed"))
            rows.append([name, _pct(ca.get("accuracy")), _num(ca.get("kappa")), str(ca["n"]), conf,
                         str(ca.get("labelled", "—")), str(ca.get("excluded", "—"))])
        for key in CODE_COUNT_KEYS:
            block = m.get(key)
            if isinstance(block, dict):
                for kind in ("static", "reruns", "rerun"):
                    by = block.get(kind)
                    if isinstance(by, dict):
                        counts += [[name, kind, str(s), str(v)] for s, v in sorted(by.items())]
    out = ["## 5. Code checks", "",
           ("Agreement of Onus's paper-level code verdict (mismatch / consistent) with the audit label "
           "(CRITICAL vs PASSED). Confusion: predicted mismatch & audit mismatch / predicted mismatch & audit passed /"
           " predicted ok & audit mismatch / predicted ok & audit passed."), "",
           table(["Split", "Accuracy", "Cohen's κ", "n (papers)", "Confusion", "Labelled", "Excluded"], rows), "",
           _src(f"{src['metrics']} → splits.<split>.code_agreement", "onus codecheck …, then onus eval"), ""]
    if counts:
        out += [table(["Split", "Check", "Status", "Count"], counts), "",
                _src(f"{src['metrics']} → splits.<split>.code_checks", "onus codecheck …"), ""]
    else:
        out += [("Static-check and re-run outcome counts: not measured (not in the metrics; the harness stores only "
                "code agreement).")]
    fn = (f.get("rq4") or {}).get("funnel") or {}
    out += [("" if counts else "\n") + f"Re-runs: attempted on {fn.get('rerun_attempted', '—')} papers, consistent "
            f"with the paper on {fn.get('consistent', '—')} (the full funnel is RQ4 in section 7; "
            f"source `{src['findings']}` → rq4.funnel)."]
    if fn.get("note"):
        out += ["", f"Note from the findings: {fn['note']}"]
    return "\n".join(out)


def cost_section(mm: dict, src: dict) -> str:
    rows = []
    for name, m in mm["splits"].items():
        c = m.get("cost") or {}
        rows.append([name, str(m.get("distributions", {}).get("papers", "—")),
                     f"€{c['eur']:.2f}" if c.get("eur") is not None else "—",
                     f"{c['minutes']:.1f}" if c.get("minutes") is not None else "—",
                     f"€{c['estimate_eur']:.2f}" if c.get("estimate_eur") is not None else "—"])
    return "\n".join(["## 6. Cost and time", "",
                      ("Per split, the evaluation run's LLM spend and wall time as stored (mutation re-analyses and "
                      "baseline calls; the original analyses are paid by `onus analyze` and are not in these files). "
                      "A per-paper figure is not stored in the inputs, so it is not given here."), "",
                      table(["Split", "Papers", "Eval spend", "Wall time (min)", "Pre-run estimate"], rows), "",
                      _src(f"{src['metrics']} → splits.<split>.cost", "onus eval … --mutations NxK --baseline")])


def findings_section(f: dict, src: dict, notes: Notes) -> str:
    cmd = findings_cmd(f, src["findings"])
    r1 = f.get("rq1") or {}
    rows1 = [[label, _num(x.get("rho")), _ci(x.get("ci95"), _num), str(x.get("n", "—"))]
             for key, label in (("ai_score", "mean AI reviewer score"), ("human_score", "human reviewer score"))
             if (x := r1.get(key)) is not None]
    r2 = f.get("rq2") or {}
    p = r2.get("mann_whitney_p")
    rows2 = [[_pct(r2.get("mean_accepted")), _pct(r2.get("mean_rejected")), _pct(r2.get("difference")),
              _ci(r2.get("ci95")), "—" if p is None else f"{p:.3g}", str(r2.get("n_accepted", "—")),
              str(r2.get("n_rejected", "—"))]]
    rows3 = []
    for it in (f.get("rq3") or {}).get("items") or []:
        yes, ub, ct = it.get("yes", 0), it.get("unbacked", 0), it.get("contradicted", 0)
        share = it.get("unbacked_share")
        ci = it.get("ci95")
        mark = ""
        if not ci and yes:
            ci, mark = wilson(ub + ct, yes), DAGGER
            notes.wilson_used = True
        rows3.append([it.get("key", "—"), f"{ub + ct}/{yes} = {_pct(share)}" if yes else NA, _ci(ci) + mark,
                      str(yes), str(ub), str(ct)])
    fn = (f.get("rq4") or {}).get("funnel") or {}
    rows4 = [[str(fn.get(k, "—")) for k in ("papers", "with_code", "executable", "rerun_attempted", "consistent")]]
    splits = ", ".join(f"{k} ({v})" for k, v in (f.get("splits") or {}).items())
    return "\n".join([
        "## 7. Corpus findings (RQ1–RQ4)", "",
        (f"{f.get('papers', '—')} papers from {splits or '—'}; bootstrap intervals use {f.get('bootstrap', '—')} "
        f"resamples, seed {f.get('seed', '—')}."), "",
        "**RQ1.** Spearman ρ between a paper's flagged share and its review scores.", "",
        table(["Score", "Spearman ρ", "95% CI (bootstrap)", "n (papers)"], rows1), "", _src(f"{src['findings']} → rq1", cmd),
        "", "**RQ2.** Flagged share of accepted vs rejected papers (difference = rejected − accepted).", "",
        table(["Mean, accepted", "Mean, rejected", "Difference", "95% CI (bootstrap)", "Mann–Whitney p",
               "n accepted", "n rejected"], rows2), "", _src(f"{src['findings']} → rq2", cmd),
        "", ("**RQ3.** Share of \"Yes\" checklist answers the paper does not back (unbacked + contradicted), per item. "
        "k is the sum of the stored unbacked and contradicted counts."), "",
        table(["Checklist item", "Not backed", "95% CI", "n (Yes answers)", "unbacked", "contradicted"], rows3)
        if rows3 else NA, "", _src(f"{src['findings']} → rq3.items", cmd),
        "", "**RQ4.** Re-runnability funnel (papers at each step).", "",
        table(["Papers", "With code", "Executable (our test)", "Re-run attempted", "Re-run consistent"], rows4), "",
        _src(f"{src['findings']} → rq4.funnel", cmd)])


# ---------------------------------------------------------------- the document


def render(metrics: dict, findings: dict, labels: dict | None, src: dict[str, str | None], out: str,
           metrics_name: str = "metrics") -> str:
    mm = normalise_metrics(metrics, metrics_name)
    check_findings(findings)
    lab, lab_src = None, None
    if labels is not None:
        check_labels(labels)
        lab, lab_src = labels, src.get("labels")
    elif isinstance(mm.get("labels"), dict) and mm["labels"].get("systems"):
        lab, lab_src = mm["labels"], f"{src['metrics']} → labels"
    notes = Notes()
    cmd = (f"onus export-numbers --metrics {src['metrics']} --findings {src['findings']}"
           + (f" --labels {src['labels']}" if src.get("labels") else "") + f" --out {out}")
    body = [recall_section(mm, src, notes), flag_section(mm, src, notes), precision_section(lab, lab_src, notes),
            stability_section(mm, src, notes), code_section(mm, findings, src), cost_section(mm, src),
            findings_section(findings, src, notes)]
    parts = ["# Submission numbers", "", f"Generated by `{cmd}`.", "", how_to_read(), "",
             inputs_section(mm, {**src, "labels": lab_src}), "", *[s + "\n" for s in body]]
    if notes.wilson_used:
        parts += ["---", "", (f"{DAGGER} Interval not stored in the input; computed here as a Wilson 95% interval "
                  "(onus.eval.metrics.wilson) from the stored k and n."), ""]
    return "\n".join(parts).rstrip() + "\n"


def export_numbers(metrics_path: Path, findings_path: Path, labels_path: Path | None, out: Path) -> str:
    metrics = load_json(metrics_path, "metrics")
    findings = load_json(findings_path, "findings")
    labels = load_json(labels_path, "labels") if labels_path else None
    src = {"metrics": str(metrics_path), "findings": str(findings_path),
           "labels": str(labels_path) if labels_path else None}
    name = metrics_path.parent.name if metrics_path.parent.name != "metrics" else metrics_path.parent.parent.name
    text = render(metrics, findings, labels, src, str(out), metrics_name=name or "metrics")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    return text
