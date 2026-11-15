"""Corpus research questions RQ1-RQ4, as chart-ready series. (Phase 7)

- RQ1: Spearman rho between per-paper flagged share and (a) mean AI reviewer score, (b) human score, with
  bootstrap 95% CIs (2,000 resamples, fixed seed).
- RQ2: accepted vs rejected flagged share: difference in means with a bootstrap CI and the Mann-Whitney p.
- RQ3: share of "Yes" checklist answers that are unbacked (and contradicted), per item.
- RQ4: re-runnability funnel: with code -> executable (our execution test) -> re-run attempted -> consistent
  with the claim (the last two from code-check results; zero until Phase 10).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import stats

from onus.schemas.result import RunResult

from .harness import load_results
from .truth import SplitLabels, read_labels

BOOT = 2000
SEED = 7
CONSISTENT = {"consistent"}


def _boot_ci(f, xs: list, rng: np.random.Generator, n: int = BOOT) -> tuple[float, float] | None:
    vals = []
    idx = np.arange(len(xs))
    for _ in range(n):
        s = rng.choice(idx, size=len(idx), replace=True)
        v = f([xs[i] for i in s])
        if v is not None and not np.isnan(v):
            vals.append(v)
    if len(vals) < n // 2:
        return None
    lo, hi = np.percentile(vals, [2.5, 97.5])
    return float(lo), float(hi)


def _spearman(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3 or len({p[0] for p in pairs}) < 2 or len({p[1] for p in pairs}) < 2:
        return None
    rho = stats.spearmanr([p[0] for p in pairs], [p[1] for p in pairs]).statistic
    return None if np.isnan(rho) else float(rho)


def _papers(runs: list[tuple[list[RunResult], SplitLabels, str]]) -> list[dict]:
    out = []
    for results, labels, split in runs:
        for r in results:
            pid = r.paper["id"]
            out.append({"id": pid, "split": split, "flagged_share": r.summary.get("flagged_share", 0.0),
                        "ai": labels.score(pid, "ai_overall_mean"), "human": labels.score(pid, "human_overall_mean"),
                        "status": labels.status(pid), "result": r, "labels": labels})
    return out


def rq1(papers: list[dict], rng: np.random.Generator) -> dict:
    out = {}
    for key, col in (("ai_score", "ai"), ("human_score", "human")):
        pairs = [(p["flagged_share"], p[col]) for p in papers if p[col] is not None]
        out[key] = {"n": len(pairs), "rho": _spearman(pairs), "ci95": _boot_ci(_spearman, pairs, rng),
                    "points": [{"x": x, "y": y} for x, y in pairs]}
    return out


def rq2(papers: list[dict], rng: np.random.Generator) -> dict:
    acc = [p["flagged_share"] for p in papers if p["status"] == "accepted"]
    rej = [p["flagged_share"] for p in papers if p["status"] == "rejected"]
    out: dict = {"n_accepted": len(acc), "n_rejected": len(rej),
                 "mean_accepted": float(np.mean(acc)) if acc else None,
                 "mean_rejected": float(np.mean(rej)) if rej else None}
    if acc and rej:
        out["difference"] = out["mean_rejected"] - out["mean_accepted"]
        diffs = []
        for _ in range(BOOT):
            a = rng.choice(acc, size=len(acc), replace=True)
            b = rng.choice(rej, size=len(rej), replace=True)
            diffs.append(float(np.mean(b) - np.mean(a)))
        lo, hi = np.percentile(diffs, [2.5, 97.5])
        out["ci95"] = (float(lo), float(hi))
        out["mann_whitney_p"] = float(stats.mannwhitneyu(rej, acc, alternative="two-sided").pvalue) \
            if len(set(acc + rej)) > 1 else None
    out["series"] = {"accepted": acc, "rejected": rej}
    return out


def rq3(papers: list[dict]) -> dict:
    t: dict[str, dict[str, int]] = defaultdict(lambda: {"yes": 0, "unbacked": 0, "contradicted": 0})
    for p in papers:
        for a in p["result"].checklist:
            if a.answer.strip().lower() != "yes":
                continue
            t[a.key]["yes"] += 1
            if a.status in ("unbacked", "contradicted"):
                t[a.key][a.status] += 1
    items = [{"key": k, **v, "unbacked_share": (v["unbacked"] + v["contradicted"]) / v["yes"] if v["yes"] else None}
             for k, v in sorted(t.items())]
    return {"items": items}


def rq4(papers: list[dict]) -> dict:
    steps = {"papers": len(papers), "with_code": 0, "executable": 0, "rerun_attempted": 0, "consistent": 0}
    for p in papers:
        row = p["labels"].papers.get(p["id"], {})
        has_code = p["result"].code.available or row.get("code_status", "") == "available"
        if not has_code:
            continue
        steps["with_code"] += 1
        if row.get("code_exec_tier") == "executable":
            steps["executable"] += 1
        reruns = p["result"].code.reruns
        if reruns:
            steps["rerun_attempted"] += 1
            if any(str(x.get("status", "")).lower() in CONSISTENT for x in reruns):
                steps["consistent"] += 1
    steps["note"] = "re-run steps are 0 until code checks run (Phase 10)" if not steps["rerun_attempted"] else ""
    return {"funnel": steps}


def compute_findings(run_dirs: list[Path], split_roots: list[Path], seed: int = SEED) -> dict:
    if len(run_dirs) != len(split_roots):
        raise ValueError("--runs and --split-roots must pair up (same count, same order)")
    runs = [(load_results(d), read_labels(r), r.name) for d, r in zip(run_dirs, split_roots, strict=True)]
    papers = _papers(runs)
    rng = np.random.default_rng(seed)
    return {"seed": seed, "bootstrap": BOOT, "papers": len(papers),
            "splits": {name: len(res) for res, _, name in runs},
            "rq1": rq1(papers, rng), "rq2": rq2(papers, rng), "rq3": rq3(papers), "rq4": rq4(papers)}
