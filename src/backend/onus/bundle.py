"""Bundle RunResults with display metadata from labels. May read labels. (Phase 7)

One JSON line per paper: the RunResult plus `display` = {title, decision, scores: {ai_mean, human_mean},
tier: our code execution tier}. The serving app shows `display`; the pipeline never sees it.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from onus.eval.harness import load_results
from onus.eval.truth import read_labels


def write_bundle(results_dir: Path, split_root: Path, out: Path) -> int:
    labels = read_labels(split_root)
    results = load_results(results_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        for r in results:
            pid = r.paper["id"]
            row = labels.papers.get(pid, {})
            display = {"title": labels.title(pid) or r.paper.get("title"), "decision": labels.status(pid),
                       "scores": {"ai_mean": labels.score(pid, "ai_overall_mean"),
                                  "human_mean": labels.score(pid, "human_overall_mean")},
                       "tier": row.get("code_exec_tier") or None, "topic": row.get("primary_topic") or None}
            d = json.loads(r.model_dump_json())
            d["display"] = display
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return len(results)
