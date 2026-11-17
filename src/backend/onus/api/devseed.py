"""`onus dev-seed` (Phase 13a): local "done" analyses of dev papers, with their PDFs, for building and reviewing the
report page.

Each picked RunResult is imported with the bundle importer's `import_row` (no bundle file is read or written) and
its `paper.pdf` is stored with `store_upload`, so the paper row's `pdf_sha256` points at a stored file and
/api/analyses/{id}/pdf and /anchors work. `--no-pdf` seeds one more analysis without storing its PDF (the viewer's
no-preview path; corpus analyses look like this): the next paper by rank whose PDF is not already stored. Idempotent: an unchanged result is skipped and a stored PDF is
not written again.

Writes only into the local ONUS_VAR_DIR: the database must be SQLite inside it (anything else is refused).
Papers are picked by rule, not by id: those with table evidence first, then the most distinct verdicts, then the
most flagged claims, then id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from onus.config import Settings, is_within

FLAGGED = {"contradicted", "overstated", "untraced"}


class SeedError(RuntimeError):
    pass


@dataclass
class Seeded:
    analysis_id: str
    paper_id: str
    title: str
    pdf: bool
    verdicts: dict[str, int]
    status: str  # inserted | updated | unchanged

    @property
    def url(self) -> str:
        return f"/analyses/{self.analysis_id}"


def local_sqlite(settings: Settings) -> Path:
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        raise SeedError("dev-seed writes only to a local SQLite database inside ONUS_VAR_DIR; DATABASE_URL is not "
                        "SQLite")
    p = Path(url[len("sqlite:///"):]).expanduser().resolve()
    if not is_within(p, settings.var_dir):
        raise SeedError("dev-seed writes only inside ONUS_VAR_DIR; the SQLite database is outside it")
    return p


def _rank(r: dict) -> tuple:
    claims = r.get("claims") or []
    tables = sum(1 for c in claims for e in c.get("evidence") or [] if e.get("kind") in ("table", "table_cell"))
    verdicts = {c.get("verdict") for c in claims}
    flagged = sum(1 for c in claims if c.get("verdict") in FLAGGED)
    return (tables == 0, -len(verdicts), -flagged, str(r["paper"]["id"]))


def pick(results: list[dict], n: int) -> list[dict]:
    return sorted(results, key=_rank)[:n]


def load_results(results_dir: Path, split_root: Path) -> list[dict]:
    out = []
    for f in sorted(results_dir.glob("*.json")):
        try:
            r = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and "claims" in r and "paper" in r and \
                (split_root / "papers" / str(r["paper"]["id"]) / "paper.pdf").is_file():
            out.append(r)
    return out


def seed(settings: Settings, results_dir: Path, split_root: Path, n: int = 3, no_pdf: bool = False) -> list[Seeded]:
    from onus.db import repo
    from onus.db.importer import ImportReport, import_row, split_paper_ref
    from onus.db.session import init_db, make_engine, make_sessionmaker, session_scope
    from onus.ingest.fetch import store_upload

    local_sqlite(settings)
    results = load_results(results_dir, split_root)
    if not results:
        raise SeedError(f"no results with a paper.pdf under {split_root}/papers in {results_dir}")
    from onus.api.anchors import stored_pdf

    ranked = pick(results, len(results))
    with_pdf = ranked[:n]
    without: list[dict] = []
    if no_pdf:  # the next paper by rank whose PDF is not already stored (by an upload or an earlier seed)
        rest = [r for r in ranked[n:] if stored_pdf(settings.var_dir, r["paper"].get("pdf_sha256")) is None]
        if not rest:
            raise SeedError("--no-pdf: every remaining paper's PDF is already stored in ONUS_VAR_DIR/uploads")
        without = rest[:1]
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_sessionmaker(engine)
    out: list[Seeded] = []
    try:
        for r, store in [(r, True) for r in with_pdf] + [(r, False) for r in without]:
            pid = str(r["paper"]["id"])
            if store:
                data = (split_root / "papers" / pid / "paper.pdf").read_bytes()
                _, sha = store_upload(data, settings.var_dir)
                if r["paper"].get("pdf_sha256") and r["paper"]["pdf_sha256"] != sha:
                    raise SeedError(f"{pid}: paper.pdf does not match the result's pdf_sha256")
            rep = ImportReport()
            with session_scope(factory) as s:
                import_row(s, json.loads(json.dumps(r)), rep)
                source, ext = split_paper_ref(r["paper"])
                p = repo.find_paper(s, source=source, external_id=ext)
                assert p is not None
                a = repo.find_analysis(s, paper_id=p.id, fingerprint=r["fingerprint"], mode=r["mode"])
                assert a is not None
                status = "inserted" if rep.inserted else "updated" if rep.updated else "unchanged"
                counts: dict[str, int] = {}
                for c in r.get("claims") or []:
                    counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
                out.append(Seeded(analysis_id=a.id, paper_id=pid, title=p.title or "", status=status,
                                  pdf=stored_pdf(settings.var_dir, p.pdf_sha256) is not None, verdicts=counts))
    finally:
        engine.dispose()
    return out
