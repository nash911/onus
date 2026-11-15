"""Idempotent import of result bundles at startup (BUILD_PLAN §8, Phase 8).

A bundle is `bundle_<name>.jsonl.gz`: one `RunResult` per line plus an optional `display` object (title,
decision, scores, tier) that `onus bundle` added from labels after the run. The importer:

- upserts the paper by (source, external_id), where external_id is the RunResult's paper id (the OpenReview
  forum id for corpus papers; "arxiv:<id>" style ids are split into source and id);
- stores `display` on the paper;
- upserts the analysis by (paper, fingerprint, mode); an unchanged result (same `created_at`) is skipped, so
  re-importing the same bundle on every startup does nothing.

Directories are scanned for `*.jsonl.gz`; a missing or unreadable directory is skipped with a warning.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from onus.db import repo
from onus.db.models import SOURCE_VALUES
from onus.db.session import session_scope

log = logging.getLogger("onus.importer")


@dataclass
class ImportReport:
    files: list[str] = field(default_factory=list)
    papers: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"files": self.files, "papers": self.papers, "inserted": self.inserted, "updated": self.updated,
                "unchanged": self.unchanged, "errors": self.errors}


def split_paper_ref(paper: dict[str, Any]) -> tuple[str, str]:
    """(source, external_id) for a RunResult `paper` dict."""
    pid = str(paper.get("id") or "")
    source = str(paper.get("source") or "agents4science")
    if ":" in pid:
        prefix, rest = pid.split(":", 1)
        if prefix in SOURCE_VALUES:
            return prefix, rest
    if source not in SOURCE_VALUES:
        source = "agents4science"
    return source, pid


def iter_bundle(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path.name}:{n}: invalid JSON ({e.msg})") from e


def bundle_files(dirs: Iterable[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            files = sorted(d.glob("*.jsonl.gz"))
        except OSError as e:
            log.warning("skipping %s: %s", d, e)
            continue
        for f in files:
            r = f.resolve()
            if r not in seen:
                seen.add(r)
                out.append(f)
    return out


def import_row(s: Session, row: dict[str, Any], report: ImportReport) -> None:
    from onus.schemas.result import RunResult

    display = row.pop("display", None)
    result = json.loads(RunResult.model_validate(row).model_dump_json())  # validate against the contract
    paper = result["paper"]
    source, external_id = split_paper_ref(paper)
    if not external_id:
        raise ValueError("result has no paper id")
    title = (display or {}).get("title") or paper.get("title")
    p = repo.upsert_paper(s, source=source, external_id=external_id, title=title,
                          pdf_sha256=paper.get("pdf_sha256"), pages=paper.get("pages"), display=display)
    report.papers += 1
    a = repo.find_analysis(s, paper_id=p.id, fingerprint=result["fingerprint"], mode=result["mode"])
    if a is not None and a.status == "done" and a.result and a.result.get("created_at") == result["created_at"]:
        report.unchanged += 1
        return
    if a is None:
        a = repo.create_analysis(s, paper_id=p.id, onus_version=result["onus_version"],
                                 fingerprint=result["fingerprint"], mode=result["mode"], runs=result["runs"],
                                 status="running")
        report.inserted += 1
    else:
        a.onus_version, a.runs = result["onus_version"], result["runs"]
        report.updated += 1
    a.progress = None
    a.error = None
    repo.store_result(s, a, result)


def import_bundles(factory: sessionmaker[Session], dirs: Iterable[Path]) -> ImportReport:
    """Import every bundle under `dirs`. Each file is one transaction; a bad file is reported, not fatal."""
    report = ImportReport()
    for f in bundle_files(dirs):
        report.files.append(f.name)
        try:
            with session_scope(factory) as s:
                for row in iter_bundle(f):
                    import_row(s, row, report)
        except Exception as e:  # noqa: BLE001 - a bad bundle must not stop the app
            msg = f"{f.name}: {type(e).__name__}: {e}"[:500]
            log.error("bundle import failed: %s", msg)
            report.errors.append(msg)
    return report
