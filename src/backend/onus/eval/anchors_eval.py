"""`onus anchors-eval` (Phase 13a): how well the report page's PDF highlights land, on a split we may read (dev).

For every claim and every text, figure or table evidence item of each RunResult, places it with
`onus.api.anchors` (the code the API serves) and scores it:

- placed: it got at least one rect;
- page match: the first highlighted page is the item's `location.page`;
- method: offsets / table / search / paragraph;
- F1: token F1 between the PDF words inside the rects (PyMuPDF words whose centre lies in a rect) and the claim
  quote or evidence snippet (a clipped snippet without its cut last word). Tokens are lower-cased \\w+ runs after
  NFKC; a word broken by a line-end hyphen counts once.

Reads `<split_root>/papers/<id>/paper.pdf` and results only; no labels, no LLM calls.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from onus.api.anchors import NO_PDF_PLACE, TABLE_KINDS, Pdf, load_doc, place, snippet_core, words_of

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
    paper = result["paper"]
    sha = paper.get("pdf_sha256") or hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    doc = load_doc(var_dir, paper["id"], paper.get("source") or "", pdf_path, sha)
    pdf = Pdf.open(pdf_path)
    rows = []
    try:
        for c in result.get("claims") or []:
            todo = [("claim", c["id"], c.get("quote") or "", c.get("location"), "")]
            for e in c.get("evidence") or []:
                if e.get("kind") in NO_PDF_PLACE or (e.get("location") is None and e.get("kind") not in TABLE_KINDS):
                    continue
                core = snippet_core(e.get("snippet") or "")[0] if e["kind"] not in TABLE_KINDS else e.get("snippet", "")
                todo.append((e["kind"], f"{c['id']}:{e['id']}", core, e.get("location"), e.get("label") or ""))
            for kind, key, ref, loc, label in todo:
                got = place(doc, pdf, text=ref, loc=loc, kind="claim" if kind == "claim" else kind, label=label)
                f1 = token_f1(words_in(pdf, got), words_of(ref)) if got else 0.0
                rows.append({"paper_id": paper["id"], "key": key, "kind": kind, "placed": bool(got),
                             "method": got[0]["method"] if got else "missing",
                             "page": got[0]["page"] if got else None, "location_page": (loc or {}).get("page"),
                             "page_match": bool(got) and loc is not None and got[0]["page"] == loc.get("page"),
                             "f1": round(f1, 4), "quote_head": " ".join(ref.split())[:70]})
    finally:
        pdf.close()
    return rows


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
           "by_kind": {k: stats(rs) for k, rs in sorted(kinds.items())}, "targets": {}}
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
    bad = sorted(rows, key=lambda r: (r["f1"], r["paper_id"], r["key"]))[:worst]
    lines += ["", f"Worst {len(bad)} items:", "", "| Paper | Key | Kind | Method | Page (loc) | F1 | Quote head |",
              "| --- | --- | --- | --- | --- | ---: | --- |"]
    lines += [f"| {r['paper_id']} | {r['key']} | {r['kind']} | {r['method']} | {r['page']} ({r['location_page']}) | "
              f"{r['f1']:.3f} | {r['quote_head'].replace('|', '/')} |" for r in bad]
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
