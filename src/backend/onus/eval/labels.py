"""Blind hand-label export and scoring. (Phase 7)

`export`: a shuffled sheet of flags from Onus (contradicted / overstated / untraced, stratified by verdict) and
from the baseline (`not_supported`), with no column saying which system raised the flag. Columns: item_id,
paper title, claim quote, location, flag text, evidence snippets, then empty `label` (correct / incorrect /
unsure) and `notes`. The key (item_id -> system, paper, claim, verdict) is written next to the CSV as
`<name>.key.json`.

`score`: precision per system and per verdict (correct / (correct + incorrect); `unsure` and blank rows are
left out), with Wilson 95% CIs.
"""

from __future__ import annotations

import csv
import json
import random
from collections import defaultdict
from pathlib import Path

from onus.schemas.result import RunResult

from .baseline import load_baselines
from .harness import load_results
from .metrics import FLAGGED, wilson

COLUMNS = ["item_id", "paper_title", "claim_quote", "location", "flag", "evidence", "label", "notes"]
LABELS = {"correct", "incorrect", "unsure"}


def key_path(csv_path: Path) -> Path:
    return csv_path.with_name(csv_path.stem + ".key.json")


def _stratified(items: list[dict], n: int, rng: random.Random, by: str) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        groups[it[by]].append(it)
    for g in groups.values():
        rng.shuffle(g)
    out: list[dict] = []
    keys = sorted(groups)
    while len(out) < n and any(groups.values()):  # round-robin over verdicts: equal shares where possible
        for k in keys:
            if groups[k] and len(out) < n:
                out.append(groups[k].pop())
    return out


def _evidence(c) -> str:
    return " | ".join(f"{e.label}: {' '.join(e.snippet.split())[:240]}" for e in c.evidence
                      if e.relation in ("supports", "partial", "contradicts"))[:900]


def export_sheet(results_dir: Path, baseline_dir: Path | None, n_onus: int, n_baseline: int, seed: int,
                 out: Path) -> dict:
    rng = random.Random(seed)
    results = load_results(results_dir)
    titles = {r.paper["id"]: r.paper.get("title") or "" for r in results}
    claims = {(r.paper["id"], c.id): c for r in results for c in r.claims}
    onus = [{"system": "onus", "paper_id": r.paper["id"], "claim_id": c.id, "verdict": c.verdict,
             "flag": c.reason + (f" Caveat: {c.caveat}" if c.caveat else "")}
            for r in results for c in r.claims if c.verdict in FLAGGED]
    base: list[dict] = []
    if baseline_dir is not None:
        for pid, b in load_baselines(baseline_dir).items():
            for bc in b.claims:
                if bc.label == "not_supported" and (pid, bc.claim_id) in claims:
                    base.append({"system": "baseline", "paper_id": pid, "claim_id": bc.claim_id,
                                 "verdict": "not_supported", "flag": bc.reason})
    picked = _stratified(onus, n_onus, rng, "verdict") + _stratified(base, n_baseline, rng, "verdict")
    rng.shuffle(picked)
    key = {}
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for i, it in enumerate(picked, 1):
            iid = f"L{i:03d}"
            c = claims[(it["paper_id"], it["claim_id"])]
            w.writerow({"item_id": iid, "paper_title": titles.get(it["paper_id"], ""), "claim_quote": c.quote,
                        "location": c.location.cite() + (f" ({c.location.section})" if c.location.section else ""),
                        "flag": " ".join(it["flag"].split()), "evidence": _evidence(c), "label": "", "notes": ""})
            key[iid] = {k: it[k] for k in ("system", "paper_id", "claim_id", "verdict")}
    key_path(out).write_text(json.dumps({"seed": seed, "items": key}, indent=1))
    return {"items": len(picked), "onus": sum(1 for x in picked if x["system"] == "onus"),
            "baseline": sum(1 for x in picked if x["system"] == "baseline"), "key": str(key_path(out))}


def score_sheet(done_csv: Path, key_file: Path) -> dict:
    key = json.loads(key_file.read_text())["items"]
    tally: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])  # correct, incorrect, unsure
    with done_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            k = key.get((row.get("item_id") or "").strip())
            lab = (row.get("label") or "").strip().lower()
            if k is None or lab not in LABELS:
                continue
            i = {"correct": 0, "incorrect": 1, "unsure": 2}[lab]
            tally[(k["system"], k["verdict"])][i] += 1
            tally[(k["system"], "all")][i] += 1

    def prec(c: int, w: int, u: int) -> dict:
        n = c + w
        return {"correct": c, "incorrect": w, "unsure": u, "precision": c / n if n else None, "ci95": wilson(c, n)}

    out: dict = {"systems": {}}
    for (system, verdict), (c, w, u) in sorted(tally.items()):
        out["systems"].setdefault(system, {})[verdict] = prec(c, w, u)
    return out


def results_titles(results: list[RunResult]) -> dict[str, str]:
    return {r.paper["id"]: r.paper.get("title") or "" for r in results}
