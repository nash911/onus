"""`onus anchors-eval` (Phase 13a): how well the report page's PDF highlights land, on a split we may read (dev).

For every anchor key the API serves (`anchor_jobs`: each claim, each text, figure or table evidence item, and each
checklist audit's evidence, kind `checklist`), places it with `onus.api.anchors` (the code the API serves) and
scores it:

- placed: it got at least one rect;
- page match: the first highlighted page is the item's `location.page`;
- method: offsets / table / search / paragraph;
- F1: token F1 between the PDF words inside the rects (PyMuPDF words whose centre lies in a rect) and the claim
  quote or evidence snippet (a clipped snippet without its cut last word). Tokens are lower-cased \\w+ runs after
  NFKC; a word broken by a line-end hyphen counts once. For table evidence this is the old measure (the box's words
  against the whole caption-plus-grid snippet);
- cell F1 (`table_cell` placements only): the same token F1 against the referenced cells' raw text instead.

Reads `<split_root>/papers/<id>/paper.pdf` and results only; no labels, no LLM calls.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from onus.api.anchors import (
    TABLE_KINDS,
    Pdf,
    anchor_jobs,
    find_table,
    load_doc,
    locate_cells,
    place,
    words_of,
)

GOOD_F1 = 0.8
TARGETS = {"claim": 0.95, "text": 0.90}


def token_f1(got: list[str], want: list[str]) -> float:
    if not got or not want:
        return 0.0
    common = sum((Counter(got) & Counter(want)).values())
    if not common:
        return 0.0
    p, r = common / len(got), common / len(want)
    return 2 * p * r / (p + r)


def words_in(pdf: Pdf, items: list[dict]) -> list[str]:
    out: list[str] = []
    for it in items:
        rects = it["rects"]
        ws = [w for w in pdf.words(it["page"])
              if any(r[0] - 1 <= (w[0] + w[2]) / 2 <= r[2] + 1 and r[1] - 1 <= (w[1] + w[3]) / 2 <= r[3] + 1
                     for r in rects)]
        text, carry = [], ""
        for w in ws:
            t = carry + w[4]
            if t.endswith("-") and len(t) > 1 and t[-2].isalpha():
                carry = t[:-1]
                continue
            carry = ""
            text.append(t)
        if carry:
            text.append(carry)
        out += words_of(" ".join(text))
    return out


def eval_paper(result: dict, pdf_path: Path, var_dir: Path) -> list[dict]:
    """One row per anchor key the API serves (`anchor_jobs`: claims, claim evidence, checklist evidence)."""
    paper = result["paper"]
    sha = paper.get("pdf_sha256") or hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    doc = load_doc(var_dir, paper["id"], paper.get("source") or "", pdf_path, sha)
    pdf = Pdf.open(pdf_path)
    rows = []
    try:
        for key, args in anchor_jobs(result):
            ev_kind = args.get("kind") or "claim"
            kind = "checklist" if key.startswith("checklist:") else ev_kind
            ref = args["text"] if ev_kind not in TABLE_KINDS else _table_snippet(result, key)
            loc = args.get("loc")
            got = place(doc, pdf, **args)
            got_words = words_in(pdf, got) if got else []
            f1 = token_f1(got_words, words_of(ref)) if got else 0.0
            row = {"paper_id": paper["id"], "key": key, "kind": kind, "ev_kind": ev_kind, "placed": bool(got),
                   "method": got[0]["method"] if got else "missing",
                   "page": got[0]["page"] if got else None, "location_page": (loc or {}).get("page"),
                   "page_match": bool(got) and loc is not None and got[0]["page"] == loc.get("page"),
                   "f1": round(f1, 4), "quote_head": " ".join(ref.split())[:70]}
            if ev_kind in TABLE_KINDS:
                wheres = [args["label"]] if ev_kind == "table_cell" else \
                    [c for c in args.get("cells") or [] if find_table(doc, c) is find_table(doc, args["label"])]
                row["cells_referenced"] = len(wheres)
                if got and got[0]["method"] == "table_cell":
                    t = find_table(doc, args["label"])
                    found = locate_cells(pdf, t, wheres) or []
                    want = [w for raw, _ in found for w in words_of(raw)]
                    row["cell_f1"] = round(token_f1(got_words, want), 4)
            rows.append(row)
    finally:
        pdf.close()
    return rows


def _table_snippet(result: dict, key: str) -> str:
    """A table item's full evidence snippet (the old measure compares the box's words with all of it)."""
    head, _, ev_id = key.rpartition(":")
    if head.startswith("checklist:"):
        items = next((a.get("evidence") or [] for a in result.get("checklist") or []
                      if f"checklist:{a['key']}" == head), [])
    else:
        items = next((c.get("evidence") or [] for c in result.get("claims") or [] if c["id"] == head), [])
    return next((e.get("snippet") or "" for e in items if e["id"] == ev_id), "")


def table_cells_summary(rows: list[dict]) -> dict:
    """Table evidence (claims and checklist): how many got cell rects, and the cell-aware F1 of those."""
    t = [r for r in rows if r["ev_kind"] in TABLE_KINDS]
    cell = [r for r in t if r["method"] == "table_cell"]
    ref = [r for r in t if r.get("cells_referenced")]
    f1s = [r["cell_f1"] for r in cell if "cell_f1" in r]
    return {"n": len(t), "with_cells_referenced": len(ref), "table_cell": len(cell),
            "fell_back_with_cells": sum(1 for r in ref if r["method"] != "table_cell"),
            "no_cells_referenced": len(t) - len(ref),
            "cell_f1_good": sum(1 for x in f1s if x >= GOOD_F1),
            "cell_f1_mean": (sum(f1s) / len(f1s)) if f1s else None}


def summarise(rows: list[dict]) -> dict:
    by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    kinds: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[(r["kind"], r["method"])].append(r)
        kinds[r["kind"]].append(r)

    def stats(rs: list[dict]) -> dict:
        n = len(rs)
        placed = [r for r in rs if r["placed"]]
        return {"n": n, "placed": len(placed), "page_match": sum(r["page_match"] for r in rs),
                "good": sum(1 for r in rs if r["placed"] and r["f1"] >= GOOD_F1),
                "good_share": (sum(1 for r in rs if r["placed"] and r["f1"] >= GOOD_F1) / n) if n else None,
                "mean_f1": (sum(r["f1"] for r in placed) / len(placed)) if placed else None}

    out = {"by_kind_method": {f"{k}/{m}": stats(rs) for (k, m), rs in sorted(by.items())},
           "by_kind": {k: stats(rs) for k, rs in sorted(kinds.items())}, "targets": {},
           "table_cells": table_cells_summary(rows)}
    for k, t in TARGETS.items():
        s = out["by_kind"].get(k)
        share = s["good_share"] if s else None
        out["targets"][k] = {"target": t, "share": share, "met": share is not None and share >= t}
    return out


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.1%}"


def render(summary: dict, rows: list[dict], worst: int = 15) -> str:
    lines = ["| Kind | Method | n | Page match | F1 ≥ 0.8 | Mean F1 (placed) |", "| --- | --- | ---: | ---: | ---: | ---: |"]
    for key, s in summary["by_kind_method"].items():
        k, m = key.split("/")
        lines.append(f"| {k} | {m} | {s['n']} | {s['page_match']} | {s['good']} ({_pct(s['good_share'])}) | "
                     f"{'—' if s['mean_f1'] is None else format(s['mean_f1'], '.3f')} |")
    for k, s in summary["by_kind"].items():
        lines.append(f"| **{k}** | all | {s['n']} | {s['page_match']} | {s['good']} ({_pct(s['good_share'])}) | "
                     f"{'—' if s['mean_f1'] is None else format(s['mean_f1'], '.3f')} |")
    lines += ["", "Targets (placed with F1 ≥ 0.8): " + "; ".join(
        f"{k} {_pct(t['share'])} (target {t['target']:.0%}, {'met' if t['met'] else 'NOT met'})"
        for k, t in summary["targets"].items())]
    tc = summary["table_cells"]
    lines += ["", ("Table evidence, cell-aware measure (for `table_cell` placements: token F1 between the words in "
              "the cell rects and the referenced cells' raw text; the table rows above use the old measure, the "
              "box's words against the whole table snippet):"), "",
              (f"- table items {tc['n']}: cells referenced by the claim's checks {tc['with_cells_referenced']}, "
              f"none referenced {tc['no_cells_referenced']}"),
              (f"- cell rects (`table_cell`) {tc['table_cell']}; referenced but fell back to the table box "
              f"{tc['fell_back_with_cells']}"),
              (f"- cell-aware F1 ≥ 0.8: {tc['cell_f1_good']} of {tc['table_cell']}; mean "
              f"{'—' if tc['cell_f1_mean'] is None else format(tc['cell_f1_mean'], '.3f')}")]
    bad = sorted(rows, key=lambda r: (r["f1"], r["paper_id"], r["key"]))[:worst]
    lines += ["", (f"Worst {len(bad)} items by the old measure (F1 against the snippet; Cell F1 is the cell-aware "
              "measure, `table_cell` only):"), "",
              "| Paper | Key | Kind | Method | Page (loc) | F1 | Cell F1 | Quote head |",
              "| --- | --- | --- | --- | --- | ---: | ---: | --- |"]
    lines += [f"| {r['paper_id']} | {r['key']} | {r['kind']} | {r['method']} | {r['page']} ({r['location_page']}) | "
              f"{r['f1']:.3f} | {format(r['cell_f1'], '.3f') if 'cell_f1' in r else '—'} | "
              f"{r['quote_head'].replace('|', '/')} |" for r in bad]
    return "\n".join(lines)


def run(results_dir: Path, split_root: Path, var_dir: Path) -> tuple[dict, list[dict], list[str]]:
    rows: list[dict] = []
    skipped: list[str] = []
    for f in sorted(results_dir.glob("*.json")):
        result = json.loads(f.read_text())
        if "claims" not in result or "paper" not in result:
            continue
        pdf = split_root / "papers" / result["paper"]["id"] / "paper.pdf"
        if not pdf.is_file():
            skipped.append(f"{result['paper']['id']}: no paper.pdf")
            continue
        rows += eval_paper(result, pdf, var_dir)
    return summarise(rows), rows, skipped
