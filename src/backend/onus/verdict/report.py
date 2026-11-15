"""Markdown summaries of a results directory (`onus report`) and of a cost test (`onus analyze --cost-report`).

Neither reads labels: they summarise RunResults and the cost ledger only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime

from onus.llm.costs import cost_eur
from onus.schemas.result import RISK, RunResult

VERDICTS = ("contradicted", "overstated", "untraced", "not_checkable", "supported")
STATUSES = ("backed", "unbacked", "contradicted", "not_checkable", "not_applicable")
ROLES = ("extract", "judge", "fast", "embed")


def _cell(s: str, n: int | None = None) -> str:
    s = " ".join(str(s).split()).replace("|", "\\|")
    return s if n is None or len(s) <= n else s[: n - 1] + "…"


def render_results_report(results: list[RunResult], title: str = "Results", top: int = 3) -> str:
    fps = sorted({r.fingerprint for r in results})
    out = [f"# {title}", "",
           (f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `onus report`. {len(results)} papers; "
           f"fingerprint {', '.join(f'`{f}`' for f in fps) or '-'}; runs per paper "
           f"{', '.join(str(x) for x in sorted({r.runs for r in results})) or '-'}."), "",
           ("Flagged = contradicted, overstated or untraced. Stability = mean agreeing runs / runs over reported "
           "claims."), "",
           "| Paper | Claims | " + " | ".join(VERDICTS) + " | Flagged | Main flagged | Stability | Downgraded | € | "
           "Minutes |",
           "| --- | ---: | " + " | ".join("---:" for _ in VERDICTS) + " | ---: | ---: | ---: | ---: | ---: | ---: |"]
    tot: Counter = Counter()
    cl_tot: Counter = Counter()
    eur = mins = 0.0
    stabs: list[float] = []
    n_claims = n_main = n_main_flag = 0
    for r in sorted(results, key=lambda r: r.paper["id"]):
        s = r.summary
        c = s["counts"]
        tot.update(c)
        cl_tot.update(s.get("checklist", {}))
        eur += r.cost.get("eur", 0.0)
        mins += r.timings.get("wall_s", 0.0) / 60
        n_claims += s["n_claims"]
        n_main += s.get("n_main", 0)
        n_main_flag += round(s.get("main_flagged_share", 0) * s.get("n_main", 0))
        if s.get("stability_mean") is not None:
            stabs.append(s["stability_mean"])
        st = f"{s['stability_mean']:.2f}" if s.get("stability_mean") is not None else "—"
        out.append(f"| {r.paper['id']} | {s['n_claims']} | " + " | ".join(str(c.get(v, 0)) for v in VERDICTS)
                   + f" | {s['flagged_share']:.0%} | {s['main_flagged_share']:.0%} | {st} | {s.get('downgraded', 0)} "
                   f"| {r.cost.get('eur', 0):.3f} | {r.timings.get('wall_s', 0) / 60:.1f} |")
    flagged = sum(tot[v] for v in ("contradicted", "overstated", "untraced"))
    line = f"**Totals:** {n_claims} claims; " + ", ".join(f"{v} {tot[v]}" for v in VERDICTS)
    if n_claims:
        line += f". Flagged {flagged / n_claims:.0%}"
    if n_main:
        line += f"; main claims flagged {n_main_flag}/{n_main}"
    if stabs:
        line += f"; mean stability {sum(stabs) / len(stabs):.2f}"
    line += f". Chat spend €{eur:.3f}; {mins:.1f} paper-minutes (wall time per paper, summed)."
    out += ["", line, "", "## Checklist audit", "", "| " + " | ".join(STATUSES) + " |", "| " + " | ".join("---:" for _ in STATUSES) + " |",
            "| " + " | ".join(str(cl_tot.get(s, 0)) for s in STATUSES) + " |", ""]

    led: dict[str, Counter] = defaultdict(Counter)
    ran: Counter = Counter()
    for r in results:
        for e in r.ledger:
            ran[e.check] += e.ran_on
            led[e.check].update(e.reasons)
    out += ["## Ledger (all papers)", "", "| Check | Ran on | Not run: top reasons |", "| --- | ---: | --- |"]
    for check, reasons in led.items():
        rs = "; ".join(f"{k} ({v})" for k, v in reasons.most_common(4))
        out.append(f"| {check} | {ran[check]} | {_cell(rs)} |")
    nc: Counter = Counter()
    for r in results:
        for x in r.not_checked:
            nc[x.what if not x.what.endswith("not parsed") else "Tables not parsed"] += x.count
    out += ["", "Not checked: " + ("; ".join(f"{k} ({v})" for k, v in nc.most_common()) or "—") + ".", ""]

    out += [f"## Highest-risk claims (top {top} per paper)", ""]
    for r in sorted(results, key=lambda r: r.paper["id"]):
        flagged_claims = [c for c in r.claims if c.risk >= RISK["untraced"]][:top]
        if not flagged_claims:
            continue
        out += [f"### {r.paper['id']}", "", "| Verdict | Stability | Claim | Location | Reason |",
                "| --- | --- | --- | --- | --- |"]
        for c in flagged_claims:
            st = f"{c.stability.agree}/{c.stability.runs}" if c.stability else "—"
            dg = f" (rule: {c.rule_verdict})" if c.rule_verdict and c.rule_verdict != c.verdict else ""
            out.append(f"| {c.verdict}{dg} | {st} | {_cell(c.quote, 200)} | {c.location.cite()} | "
                       f"{_cell(c.reason, 360)}{' Caveat: ' + _cell(c.caveat, 200) if c.caveat else ''} |")
        out.append("")
    warns = [(r.paper["id"], w) for r in results for w in r.warnings]
    if warns:
        out += ["## Warnings", ""] + [f"- {pid}: {_cell(w, 300)}" for pid, w in warns]
    return "\n".join(out) + "\n"


def render_cost_report(results: list[RunResult], rows: list[dict], *, corpus_papers: int = 247,
                       corpus_runs: int = 3) -> str:
    """`rows`: cost-ledger rows written during the test, for these papers."""
    ids = [r.paper["id"] for r in results]
    by: dict[str, dict[str, dict[str, float]]] = {pid: defaultdict(lambda: defaultdict(float)) for pid in ids}
    for row in rows:
        pid = row.get("paper_id")
        if pid not in by:
            continue
        acc = by[pid][row.get("role", "?")]
        eur = float(row.get("eur", 0.0))
        if row.get("cache_hit"):
            try:  # what the call would have cost uncached
                eur_eq = cost_eur(row["model"], int(row.get("tokens_in", 0)), int(row.get("tokens_out", 0)),
                                  int(row.get("cached_in", 0)))
            except Exception:  # noqa: BLE001
                eur_eq = 0.0
            acc["cache_hits"] += 1
        else:
            eur_eq = eur
        acc["eur"] += eur
        acc["eur_uncached"] += eur_eq
        acc["calls"] += 1
        acc["tokens_in"] += int(row.get("tokens_in", 0))
        acc["tokens_out"] += int(row.get("tokens_out", 0))
    runs = results[0].runs if results else 1
    out = ["# Cost test (Phase 6)", "",
           f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC. {len(results)} dev papers × {runs} run(s), fingerprint "
           f"`{results[0].fingerprint if results else '-'}`. Models: "
           + (", ".join(f"{k} `{v}`" for k, v in results[0].models.items()) if results else "-") + ".", "",
           "€ is what the test spent; *€ uncached* also prices cache hits at list price (what a fresh paper costs).",
           "", "| Paper | " + " | ".join(f"{r} €" for r in ROLES) + " | € | € uncached | Calls | Tokens in | "
           "Tokens out | Minutes |", "| --- | " + " | ".join("---:" for _ in ROLES) + " | ---: | ---: | ---: | ---: | "
           "---: | ---: |"]
    tot_unc = tot_min = 0.0
    role_tot: Counter = Counter()
    for r in results:
        pid = r.paper["id"]
        roles = by[pid]
        unc = sum(v["eur_uncached"] for v in roles.values())
        spent = sum(v["eur"] for v in roles.values())
        mins = r.timings.get("wall_s", 0.0) / 60
        tot_unc += unc
        tot_min += mins
        for k, v in roles.items():
            role_tot[k] += v["eur_uncached"]
        out.append(f"| {pid} | " + " | ".join(f"{roles[k]['eur_uncached']:.4f}" if k in roles else "0" for k in ROLES)
                   + f" | {spent:.4f} | {unc:.4f} | {int(sum(v['calls'] for v in roles.values()))} | "
                   f"{int(sum(v['tokens_in'] for v in roles.values()))} | "
                   f"{int(sum(v['tokens_out'] for v in roles.values()))} | {mins:.1f} |")
    n = max(len(results), 1)
    per_run = tot_unc / n / max(runs, 1)
    mins_run = tot_min / n
    by_stage: Counter = Counter()
    for r in results:
        by_stage.update({k: v for k, v in r.cost.get("by_stage", {}).items()})
    out += ["", "Role columns are uncached-equivalent €.", "",
            "## Per paper-run", "",
            f"- Mean **€{per_run:.3f} per paper per run** (uncached), "
            + ", ".join(f"{k} €{v / n / max(runs, 1):.3f}" for k, v in role_tot.most_common()) + ".",
            "- By stage (spent, this test): " + ", ".join(f"{k} €{v / n:.3f}" for k, v in by_stage.items()) + ".",
            f"- Mean wall time {mins_run:.1f} min per paper ({runs} run(s) in parallel).", "",
            "## Extrapolation", "",
            (f"- Corpus: {corpus_papers} papers × {corpus_runs} runs = {corpus_papers * corpus_runs} paper-runs → "
            f"**≈ €{per_run * corpus_papers * corpus_runs:.0f}** (uncached)."),
            (f"- One live run (1 run, no code checks): **≈ €{per_run:.3f}** and ≈ {mins_run:.1f} min wall time"
             + (" (measured with runs in parallel)." if runs > 1 else ".")),
            f"- Threshold: €0.40 per paper-run → **{'over: propose cheaper role assignments' if per_run > 0.40 else 'within'}**.",
            ""]
    return "\n".join(out) + "\n"
