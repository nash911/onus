"""Phase 0: stage the Agents4Science corpus into split folders with an information barrier.

Writes the dev split INTO the repo (data/dev) and every other split OUTSIDE it (--heldout).
Inside each split root, model INPUTS and evaluation LABELS are kept in separate folders:

    <split_root>/
      papers/<forum_id>/paper.pdf          <- input
      papers/<forum_id>/code/...           <- input (submitted code: folder and/or submission.zip)
      labels/papers.csv                    <- label: this split's rows of splits_proposal.csv
      labels/reviews/<forum_id>.json       <- label: OpenReview notes (reviews, decision)
      labels/reviews/<forum_id>.md         <- label: review_report.md, if present
      labels/code_tests.csv                <- label: sandbox grades for this split, if given
      labels/conference/...                <- label: conference CSV rows + audit reports for this split
      manifest.json

Usage (run from the repo root):
    python scripts/stage_data.py \
        --splits /path/to/splits_proposal.csv \
        --papers-dir /path/to/papers \
        [--code-dir /path/to/code_root] \
        [--code-tests /path/to/code_test_results.csv] \
        [--conference-dir /path/to/conference_site_data] \
        --repo . --heldout ~/onus-heldout

Only the standard library is used. Nothing is deleted from the source folders.
"""

import argparse
import csv
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

DEV = "dev"
NUMBER_RE = re.compile(r"(\d+)_")
# conference files keyed by "<submission number>_<title>.pdf" in this column
NUMBER_KEYED = {"codebase_audits_results.csv": "Paper", "clf_results.csv": "filename",
                "ai_involvement_checklist_responses.csv": "paper_name"}
CONF_FILES = ("papers.csv", *NUMBER_KEYED)
SUBMISSION_RE = re.compile(r"/Submission(\d+)/")
FORUM_ID_RE = re.compile(r"[?&]id=([A-Za-z0-9_\-]+)")


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return rows, (list(rows[0].keys()) if rows else [])


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def submission_number(reviews_json_path, fid):
    """OpenReview submission number: the submission note's `number`, else from reviewer/author signatures."""
    try:
        data = json.loads(reviews_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for note in data.get("notes", []):
        if note.get("id") == fid and isinstance(note.get("number"), int):
            return note["number"]
    for note in data.get("notes", []):
        for sig in note.get("signatures", []):
            m = SUBMISSION_RE.search(sig + "/")
            if m:
                return int(m.group(1))
    return None


def rows_for_numbers(rows, col, numbers):
    """Rows whose `col` starts with a submission number in `numbers`, with a forum_id column added."""
    keep = []
    for r in rows:
        m = NUMBER_RE.match(r.get(col, ""))
        if m and int(m.group(1)) in numbers:
            keep.append({**r, "forum_id": numbers[int(m.group(1))]})
    return keep


_SKIP = shutil.ignore_patterns(".git", "__pycache__", ".venv", "node_modules")


def _ignore(folder, names):
    """Skip build/VCS folders and every symlink: submitted code is untrusted, and following a link
    could copy files from elsewhere on disk (e.g. held-out data) into a staged split."""
    return set(_SKIP(folder, names)) | {n for n in names if (Path(folder) / n).is_symlink()}


def copy_code(fid, papers_dir, code_dir, dest):
    """Copy submitted code for one paper. Returns a short description of what was copied."""
    copied = []
    candidates = []
    if code_dir:
        candidates += [code_dir / fid, code_dir / f"{fid}.zip"]
    candidates += [papers_dir / fid / "code"]
    for src in candidates:
        if src.is_symlink():
            continue
        if src.is_dir() and any(src.iterdir()):
            shutil.copytree(src, dest, dirs_exist_ok=True, ignore=_ignore)
            copied.append(f"dir:{src}")
        elif src.is_file() and src.suffix == ".zip":
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / "submission.zip")
            copied.append(f"zip:{src}")
        if copied:
            break
    return copied


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--splits", required=True, type=Path)
    ap.add_argument("--papers-dir", required=True, type=Path)
    ap.add_argument("--code-dir", type=Path)
    ap.add_argument("--code-tests", type=Path, help="code_test_results.csv with a forum_id column")
    ap.add_argument("--conference-dir", type=Path,
                    help="folder with papers.csv, codebase_audits_results.csv, audits/audit_<n>.html, ...")
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--heldout", required=True, type=Path)
    args = ap.parse_args()

    repo = args.repo.expanduser().resolve()
    heldout = args.heldout.expanduser().resolve()
    if heldout == repo or repo in heldout.parents:
        sys.exit("--heldout must be OUTSIDE the repo, or the coding agent can read it.")

    rows, fields = read_csv(args.splits)
    by_split = {}
    for r in rows:
        by_split.setdefault(r["split"], []).append(r)

    code_tests, ct_fields = (read_csv(args.code_tests) if args.code_tests else ([], []))
    conf = {}
    if args.conference_dir:
        for name in CONF_FILES:
            hits = sorted(args.conference_dir.rglob(name))
            if not hits:
                print(f"WARNING: {name} not found under {args.conference_dir}; skipped")
                continue
            if len(hits) > 1:
                print(f"WARNING: several {name} under {args.conference_dir}; using {hits[0]}")
            conf[name] = read_csv(hits[0])
            col = NUMBER_KEYED.get(name)
            if col and col not in conf[name][1]:
                print(f"WARNING: {name} has no '{col}' column; skipped")
                del conf[name]
        if not (args.conference_dir / "audits").is_dir():
            print(f"WARNING: no audits/ folder under {args.conference_dir}; no audit HTML reports staged")

    summary = {}
    for split, srows in sorted(by_split.items()):
        root = (repo / "data" / DEV) if split == DEV else (heldout / split)
        if root.exists():
            sys.exit(f"{root} already exists; remove it first so stale files cannot leak between splits.")
        ids = {r["forum_id"] for r in srows}
        n_pdf = n_code = n_rev = n_audit = 0
        n_conf = {}
        missing_pdf, missing_code = [], []
        numbers = {}

        for r in srows:
            fid = r["forum_id"]
            src = args.papers_dir / fid
            pdir = root / "papers" / fid
            if (src / "paper.pdf").exists():
                pdir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src / "paper.pdf", pdir / "paper.pdf")
                n_pdf += 1
            else:
                missing_pdf.append(fid)
            if copy_code(fid, args.papers_dir, args.code_dir, pdir / "code"):
                n_code += 1
            elif r.get("code_status") == "available":
                missing_code.append(fid)
            rev = src / "reviews.json"
            if rev.exists():
                (root / "labels" / "reviews").mkdir(parents=True, exist_ok=True)
                shutil.copy2(rev, root / "labels" / "reviews" / f"{fid}.json")
                n_rev += 1
                num = submission_number(rev, fid)
                if num is not None:
                    numbers[num] = fid
            if (src / "review_report.md").exists():
                shutil.copy2(src / "review_report.md", root / "labels" / "reviews" / f"{fid}.md")

        write_csv(root / "labels" / "papers.csv", srows, fields)
        if code_tests:
            write_csv(root / "labels" / "code_tests.csv",
                      [r for r in code_tests if r.get("forum_id") in ids], ct_fields)

        if conf:
            cdir = root / "labels" / "conference"
            if "papers.csv" in conf:
                prow, pf = conf["papers.csv"]
                keep = [r for r in prow if (m := FORUM_ID_RE.search(r.get("link", ""))) and m.group(1) in ids]
                write_csv(cdir / "papers.csv", keep, pf)
            for name, col in NUMBER_KEYED.items():
                if name in conf:
                    crow, cf = conf[name]
                    keep = rows_for_numbers(crow, col, numbers)
                    write_csv(cdir / name, keep, cf + ["forum_id"])
                    n_conf[name] = len(keep)
            audits_src = args.conference_dir / "audits"
            for num, fid in numbers.items():
                html = audits_src / f"audit_{num}.html"
                if html.exists():
                    (cdir / "audits").mkdir(parents=True, exist_ok=True)
                    shutil.copy2(html, cdir / "audits" / f"{fid}.html")
                    n_audit += 1

        no_number = sorted(ids - set(numbers.values()))
        if no_number:
            print(f"WARNING: {split}: no submission number for {len(no_number)} papers "
                  f"(conference rows and audits not linked): {', '.join(no_number)}")
        manifest = {
            "split": split, "papers": len(srows), "pdfs": n_pdf, "with_code": n_code, "reviews": n_rev,
            "audit_reports": n_audit, "conference_rows": n_conf, "no_submission_number": no_number, "missing_pdf": missing_pdf, "missing_code_expected": missing_code,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
        summary[split] = manifest
        print(f"{split:15s} -> {root}  papers={len(srows)} pdf={n_pdf} code={n_code} reviews={n_rev} "
              f"audits={n_audit} missing_pdf={len(missing_pdf)} missing_code={len(missing_code)}")

    if missing := [s for s, m in summary.items() if m["missing_pdf"] or m["missing_code_expected"]]:
        print(f"\nCheck manifest.json in: {', '.join(missing)}")
    print(f"\nDev split is in {repo / 'data' / DEV}. Held-out splits are in {heldout}.")
    print("Next: lock the held-out folder (see BUILD_PLAN.md, step 0.6) before starting the coding agent.")


if __name__ == "__main__":
    main()
