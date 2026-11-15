"""`onus` command-line interface (Typer).

Commands for later phases are registered now and exit with a clear message until they are built.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from onus.config import get_settings, repo_root

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Onus: claim-to-evidence maps for papers.")
console = Console()

llm_app = typer.Typer(no_args_is_help=True, help="Berget AI: capability probe and spend.")
schema_app = typer.Typer(no_args_is_help=True, help="The RunResult contract.")
db_app = typer.Typer(no_args_is_help=True, help="Serving database.")
eval_app = typer.Typer(help="Evaluation harness (Phase 7).", invoke_without_command=True)
labels_app = typer.Typer(no_args_is_help=True, help="Blind hand labels (Phase 7).")
app.add_typer(llm_app, name="llm")
app.add_typer(schema_app, name="schema")
app.add_typer(db_app, name="db")
app.add_typer(eval_app, name="eval")
app.add_typer(labels_app, name="labels")


def _todo(phase: int) -> NoReturn:
    console.print(f"[yellow]Not implemented yet: built in Phase {phase}.[/yellow]")
    raise typer.Exit(code=2)


# ---------------------------------------------------------------- version and freeze


@app.command()
def version() -> None:
    """Print the Onus version, fingerprint and models."""
    from onus import version as v

    s = get_settings()
    console.print(f"onus {v.ONUS_VERSION}  fingerprint {v.fingerprint()}")
    for role, model in s.models_config.items():
        console.print(f"  {role:16} {model}")
    frozen = v.load_frozen()
    if frozen:
        state = "matches" if frozen["fingerprint"] == v.fingerprint() else "[red]DRIFTED[/red]"
        console.print(f"frozen {frozen['version']} ({frozen['fingerprint']}): {state}")


@app.command()
def freeze(
    version: Annotated[str | None, typer.Option("--version", help="P0 version to freeze, X.Y.Z")] = None,
    code_version: Annotated[str | None, typer.Option("--code-version", help="code-v1 version (Phase 10)")] = None,
) -> None:
    """Pin the fingerprint in frozen.json and set ONUS_VERSION."""
    from onus import version as v

    if code_version:
        _todo(10)
    if not version:
        raise typer.BadParameter("give --version X.Y.Z")
    rec = v.freeze(version)
    console.print(f"Frozen onus {rec['version']} fingerprint {rec['fingerprint']} -> {v.frozen_path()}")


# ---------------------------------------------------------------- schema and db


@schema_app.command("export")
def schema_export(
    out: Annotated[Path | None, typer.Option(help="Output path")] = None,
) -> None:
    """Write schema/result.schema.json from onus.schemas.result."""
    from onus.schemas.result import export_schema

    p = export_schema(out or repo_root() / "schema" / "result.schema.json")
    console.print(f"wrote {p}")


@db_app.command("init")
def db_init(url: Annotated[str | None, typer.Option(help="Database URL (default DATABASE_URL)")] = None) -> None:
    """Create all tables (SQLite: also WAL mode)."""
    from onus.db.session import init_db, make_engine

    engine = make_engine(url)
    init_db(engine)
    console.print(f"initialised {engine.url.render_as_string(hide_password=True)}")


@db_app.command("export-pg")
def db_export_pg() -> None:
    """Write db/migrations/001_init.sql and db/schema.sql (Postgres DDL)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("export_pg_schema", repo_root() / "scripts" / "export_pg_schema.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for p in mod.export():
        console.print(f"wrote {p}")


# ---------------------------------------------------------------- llm


@llm_app.command("probe")
def llm_probe() -> None:
    """Probe each role's model on Berget (live; costs under €0.05)."""
    from onus.llm.capabilities import probe, write_probe_outputs
    from onus.llm.client import get_client

    s = get_settings()
    if not s.berget_api_key.get_secret_value():
        console.print("[red]BERGET_API_KEY is not set[/red]")
        raise typer.Exit(code=1)
    client = get_client()
    before = client.ledger.total_eur
    caps, rows = asyncio.run(probe(s, client))
    spent = client.ledger.total_eur - before
    write_probe_outputs(caps, rows, s.var_dir, repo_root() / "docs", spent)

    t = Table(title="Berget capabilities")
    for col in ("role", "model", "chat", "json_schema", "json_object", "tools", "cached tokens", "reasoning"):
        t.add_column(col)
    yn = {True: "yes", False: "no", None: "—"}
    for role, rc in caps.roles.items():
        if role == "embed":
            t.add_row(role, rc.model, "—", "—", "—", "—", "—", f"embedding dim {rc.embedding_dim or '—'}")
        else:
            t.add_row(role, rc.model, yn[rc.chat], yn[rc.json_schema], yn[rc.json_object], yn[rc.tools],
                      yn[rc.cached_tokens_reported], rc.reasoning_style or "—")
    console.print(t)
    failed = [r for r in rows if r["ok"] is False]
    for r in failed:
        console.print(f"  [yellow]{r['role']} {r['test']}[/yellow]: {r['note']}")
    console.print(f"Probe spend: €{spent:.4f}. Wrote {s.var_dir / 'berget_capabilities.json'} and "
                  f"docs/berget_capabilities.md")


@llm_app.command("spend")
def llm_spend(
    by: Annotated[str, typer.Option(help="Group by: role, model, prompt_id or paper_id")] = "role",
) -> None:
    """Summarise the cost ledger."""
    from onus.llm.costs import CostLedger

    s = get_settings()
    ledger = CostLedger(s.var_dir / "cost_ledger.jsonl")
    summary = ledger.summary()
    if by not in summary:
        raise typer.BadParameter("--by must be one of role, model, prompt_id, paper_id")
    t = Table(title=f"Spend by {by} ({ledger.path})")
    for col in (by, "eur", "calls", "cache hits", "tokens in", "tokens out"):
        t.add_column(col, justify="right" if col != by else "left")
    for key, acc in sorted(summary[by].items(), key=lambda kv: -kv[1]["eur"]):
        t.add_row(key, f"{acc['eur']:.4f}", str(acc["calls"]), str(acc["cache_hits"]), str(acc["tokens_in"]),
                  str(acc["tokens_out"]))
    tot = summary["total"]
    t.add_row("[b]total[/b]", f"[b]{tot['eur']:.4f}[/b]", str(tot["calls"]), str(tot["cache_hits"]),
              str(tot["tokens_in"]), str(tot["tokens_out"]))
    console.print(t)
    console.print(f"Dev budget: €{tot['eur']:.4f} of €{s.onus_dev_budget_eur:.2f}")


# ---------------------------------------------------------------- later phases


def _guard(data_root: Path, *outputs: Path) -> None:
    """Hard rule 4: a data root outside the repo needs ONUS_VAR_DIR (and every output) outside it too."""
    from onus.config import UnsafePathsError, assert_safe_paths

    try:
        assert_safe_paths(data_root, get_settings().var_dir, extra_outputs=tuple(outputs))
    except UnsafePathsError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e


def _data_root_of(path: Path) -> Path:
    path = path.resolve()
    paper_dir = path if path.is_dir() else path.parent
    return paper_dir.parent.parent if paper_dir.parent.name == "papers" else paper_dir


@app.command()
def ingest(
    path: Annotated[Path, typer.Argument(help="A paper directory (paper.pdf + code/) or a PDF file")],
    source: Annotated[str | None, typer.Option(help="Paper source (default: agents4science for a paper "
                                                       "directory, upload for a bare PDF)")] = None,
    paper_id: Annotated[str | None, typer.Option("--paper-id", help="Override the paper id")] = None,
) -> None:
    """Ingest one paper directory or PDF into $ONUS_VAR_DIR/docs/<id>.json."""
    from onus.ingest.document_builder import ingest_paper_dir, ingest_pdf, save_document

    s = get_settings()
    if not path.exists():
        raise typer.BadParameter(f"{path} does not exist")
    _guard(_data_root_of(path))
    if path.is_dir():
        doc = ingest_paper_dir(path, s.var_dir, source=source or "agents4science", paper_id=paper_id)
    else:
        doc = ingest_pdf(path, s.var_dir, source=source or "upload", paper_id=paper_id)
    out = save_document(doc, s.var_dir)
    console.print(f"{doc.paper_id}: {doc.pages} pages, {len(doc.sections)} sections, {len(doc.tables)} tables, "
                  f"{len(doc.checklist)} checklist items -> {out}")
    for w in doc.warnings:
        console.print(f"  [yellow]warning[/yellow] {w}")


@app.command("ingest-all")
def ingest_all(
    data_root: Annotated[Path | None, typer.Option("--data-root", help="Split root (default ONUS_DATA_ROOT)")] = None,
    source: Annotated[str, typer.Option(help="Paper source")] = "agents4science",
) -> None:
    """Ingest every paper under <data_root>/papers/."""
    from onus.ingest.document_builder import ingest_paper_dir, paper_dirs, save_document

    s = get_settings()
    root = (data_root or s.data_root).resolve()
    _guard(root)
    dirs = paper_dirs(root)
    if not dirs:
        console.print(f"[red]no papers under {root / 'papers'}[/red]")
        raise typer.Exit(code=1)
    failed = 0
    t = Table(title=f"Ingest {root}")
    for col in ("paper", "pages", "sections", "tables", "checklist", "code files", "warnings"):
        t.add_column(col)
    for d in dirs:
        try:
            doc = ingest_paper_dir(d, s.var_dir, source=source)
        except Exception as e:  # noqa: BLE001 - report and continue with the next paper
            failed += 1
            t.add_row(d.name, "[red]failed[/red]", "", "", "", "", f"{type(e).__name__}: {e}"[:80])
            continue
        save_document(doc, s.var_dir)
        t.add_row(doc.paper_id, str(doc.pages), str(len(doc.sections)), str(len(doc.tables)),
                  str(len(doc.checklist)), str(len(doc.code.files)), str(len(doc.warnings)))
    console.print(t)
    console.print(f"{len(dirs) - failed}/{len(dirs)} ingested -> {s.var_dir / 'docs'}")
    if failed:
        raise typer.Exit(code=1)


@app.command("ingest-report")
def ingest_report(
    data_root: Annotated[Path | None, typer.Option("--data-root", help="Split root (default ONUS_DATA_ROOT)")] = None,
    out: Annotated[Path | None, typer.Option(help="Report path (default docs/ingest_report.md)")] = None,
    refresh: Annotated[bool, typer.Option(help="Re-ingest even if a stored Document exists")] = False,
) -> None:
    """Write the per-paper ingest diagnostics to docs/ingest_report.md."""
    from onus.ingest.document_builder import (
        doc_path,
        ingest_paper_dir,
        ingest_stats,
        load_document,
        paper_dirs,
        render_report,
        save_document,
    )

    s = get_settings()
    root = (data_root or s.data_root).resolve()
    target = out or repo_root() / "docs" / "ingest_report.md"
    _guard(root, target)
    stats: list[dict] = []
    failures: dict[str, str] = {}
    for d in paper_dirs(root):
        p = doc_path(s.var_dir, d.name)
        try:
            if p.is_file() and not refresh and p.stat().st_mtime >= (d / "paper.pdf").stat().st_mtime:
                doc = load_document(p)
            else:
                doc = ingest_paper_dir(d, s.var_dir)
                save_document(doc, s.var_dir)
        except Exception as e:  # noqa: BLE001
            failures[d.name] = f"{type(e).__name__}: {e}"
            continue
        stats.append(ingest_stats(doc))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_report(stats, failures))
    console.print(f"wrote {target} ({len(stats)} papers, {len(failures)} failed)")


def _load_or_ingest(paper_dir: Path, var_dir: Path, refresh: bool):
    from onus.ingest.document_builder import doc_path, ingest_paper_dir, load_document, save_document

    p = doc_path(var_dir, paper_dir.name)
    if p.is_file() and not refresh and p.stat().st_mtime >= (paper_dir / "paper.pdf").stat().st_mtime:
        return load_document(p)
    doc = ingest_paper_dir(paper_dir, var_dir)
    save_document(doc, var_dir)
    return doc


@app.command()
def claims(
    paths: Annotated[list[Path], typer.Argument(help="Paper directories, or a split root (all its papers)")],
    run_index: Annotated[int, typer.Option("--run-index", help="Run index (cache key; 0-2 in batch)")] = 0,
    refresh: Annotated[bool, typer.Option(help="Re-ingest even if a stored Document exists")] = False,
    report: Annotated[Path | None, typer.Option(help="Also write a markdown sample (e.g. docs/claims_dev.md)")]
    = None,
    sample: Annotated[int, typer.Option(help="Papers listed claim by claim in --report")] = 3,
    concurrency: Annotated[int, typer.Option(help="Papers processed at once")] = 4,
) -> None:
    """Extract, verify and type the claims of one or more papers -> $ONUS_VAR_DIR/claims/<id>.r<N>.json."""
    from onus.claims.extract import extract_claims, save_claims
    from onus.claims.report import render_claims_report
    from onus.ingest.document_builder import paper_dirs

    s = get_settings()
    dirs: list[Path] = []
    for p in paths:
        p = p.resolve()
        if not p.is_dir():
            raise typer.BadParameter(f"{p} is not a directory")
        _guard(_data_root_of(p) if (p / "paper.pdf").is_file() else p, *([report] if report else []))
        dirs += [p] if (p / "paper.pdf").is_file() else paper_dirs(p)
    if not dirs:
        console.print("[red]no papers found[/red]")
        raise typer.Exit(code=1)

    async def run_all():
        docs = [_load_or_ingest(d, s.var_dir, refresh) for d in dirs]
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(doc):
            async with sem:
                return doc, await extract_claims(doc, run_index=run_index)

        return await asyncio.gather(*[one(doc) for doc in docs])

    results = asyncio.run(run_all())
    t = Table(title=f"Claims (run {run_index})")
    for col in ("paper", "model items", "kept", "main", "unverified", "dup", "other drops", "checklist",
                "numbers", "total", "€"):
        t.add_column(col, justify="right" if col != "paper" else "left")
    for doc, cs in results:
        save_claims(cs, s.var_dir)
        st, dr = cs.stats, cs.stats["dropped"]
        other = dr["too_short"] + dr["too_long"] + dr["unlocatable"] + dr["over_cap"]
        t.add_row(doc.paper_id, str(st["llm_items"]), str(st["llm_claims"]), str(st["main_claims"]),
                  str(dr["unverified"]), str(dr["duplicate"]), str(other), str(st["checklist_claims"]),
                  str(st["checklist_number_claims"]), str(len(cs.claims)), f"{cs.cost['eur']:.4f}")
        for w in cs.warnings:
            console.print(f"  [yellow]{doc.paper_id}[/yellow] {w}")
    console.print(t)
    console.print(f"Spend this command: €{sum(cs.cost['eur'] for _, cs in results):.4f} -> "
                  f"{s.var_dir / 'claims'}")
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_claims_report(results, sample=sample))
        console.print(f"wrote {report}")


def _paper_dirs_of(paths: list[Path], *outputs: Path) -> list[Path]:
    from onus.ingest.document_builder import paper_dirs

    dirs: list[Path] = []
    for p in paths:
        p = p.resolve()
        if not p.is_dir():
            raise typer.BadParameter(f"{p} is not a directory")
        _guard(_data_root_of(p) if (p / "paper.pdf").is_file() else p, *outputs)
        dirs += [p] if (p / "paper.pdf").is_file() else paper_dirs(p)
    if not dirs:
        console.print("[red]no papers found[/red]")
        raise typer.Exit(code=1)
    return dirs


@app.command()
def evidence(
    paths: Annotated[list[Path], typer.Argument(help="Paper directories, or a split root (all its papers)")],
    run_index: Annotated[int, typer.Option("--run-index", help="Run index (cache key; 0-2 in batch)")] = 0,
    report: Annotated[Path | None, typer.Option(help="Also write a markdown sample (e.g. docs/evidence_dev.md)")]
    = None,
    sample: Annotated[int, typer.Option(help="Claims sampled into --report")] = 10,
    concurrency: Annotated[int, typer.Option(help="Papers processed at once")] = 4,
) -> None:
    """Link every non-checklist claim to evidence -> $ONUS_VAR_DIR/evidence/<id>.r<N>.json.

    Uses the stored claims of the same run and fingerprint, or extracts them first."""
    from onus.claims.extract import claims_path, extract_claims, load_claims, save_claims
    from onus.evidence.report import render_evidence_report
    from onus.evidence.select import link_evidence, save_evidence
    from onus.version import fingerprint

    s = get_settings()
    dirs = _paper_dirs_of(paths, *([report] if report else []))
    fp = fingerprint()

    async def run_all():
        docs = [_load_or_ingest(d, s.var_dir, False) for d in dirs]
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(doc):
            async with sem:
                cp = claims_path(s.var_dir, doc.paper_id, run_index)
                cs = load_claims(cp) if cp.is_file() else None
                if cs is None or cs.fingerprint != fp:
                    cs = await extract_claims(doc, run_index=run_index)
                    save_claims(cs, s.var_dir)
                return doc, cs, await link_evidence(doc, cs, run_index=run_index)

        return await asyncio.gather(*[one(doc) for doc in docs])

    results = asyncio.run(run_all())
    t = Table(title=f"Evidence (run {run_index})")
    for col in ("paper", "claims", "pool", "cited", "dangling", "linked", "not linked", "select failed", "€"):
        t.add_column(col, justify="right" if col != "paper" else "left")
    for doc, _, es in results:
        save_evidence(es, s.var_dir)
        st = es.stats
        t.add_row(doc.paper_id, str(st["claims"]), str(st["pool"]), str(st["with_explicit"]),
                  str(st["dangling_claims"]), str(st["with_support"]), str(st["no_support"]),
                  str(st["select_failed"]), f"{es.cost['eur']:.4f}")
        for w in es.warnings:
            console.print(f"  [yellow]{doc.paper_id}[/yellow] {w}")
    console.print(t)
    console.print(f"Spend (chat calls): €{sum(es.cost['eur'] for _, _, es in results):.4f} -> "
                  f"{s.var_dir / 'evidence'}")
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_evidence_report(results, sample=sample))
        console.print(f"wrote {report}")


async def _stored_or_fresh(doc, var_dir: Path, run_index: int, fp: str):
    """Claims and evidence of this run: stored ones when their fingerprint matches, else recomputed (cached calls
    make recomputation free when only later-stage prompts or rubrics changed)."""
    from onus.claims.extract import claims_path, extract_claims, load_claims, save_claims
    from onus.evidence.select import evidence_path, link_evidence, load_evidence, save_evidence

    cp = claims_path(var_dir, doc.paper_id, run_index)
    cs = load_claims(cp) if cp.is_file() else None
    fresh = cs is None or cs.fingerprint != fp
    if fresh:
        cs = await extract_claims(doc, run_index=run_index)
        save_claims(cs, var_dir)
    ep = evidence_path(var_dir, doc.paper_id, run_index)
    es = load_evidence(ep) if ep.is_file() and not fresh else None
    if es is None or es.fingerprint != fp:
        es = await link_evidence(doc, cs, run_index=run_index)
        save_evidence(es, var_dir)
    return cs, es


@app.command()
def checks(
    paths: Annotated[list[Path], typer.Argument(help="Paper directories, or a split root (all its papers)")],
    run_index: Annotated[int, typer.Option("--run-index", help="Run index (cache key; 0-2 in batch)")] = 0,
    report: Annotated[Path | None, typer.Option(help="Also write a markdown report (e.g. docs/checks_dev.md)")]
    = None,
    concurrency: Annotated[int, typer.Option(help="Papers processed at once")] = 4,
) -> None:
    """Run the number, dominance, strength, scope and checklist checks -> $ONUS_VAR_DIR/checks/<id>.r<N>.json.

    Uses the stored claims and evidence of the same run and fingerprint, or recomputes them first."""
    from onus.checks.report import render_checks_report
    from onus.checks.run import run_checks, save_checks
    from onus.version import fingerprint

    s = get_settings()
    dirs = _paper_dirs_of(paths, *([report] if report else []))
    fp = fingerprint()

    async def run_all():
        docs = [_load_or_ingest(d, s.var_dir, False) for d in dirs]
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(doc):
            async with sem:
                cs, es = await _stored_or_fresh(doc, s.var_dir, run_index, fp)
                return doc, cs, es, await run_checks(doc, cs, es, run_index=run_index)

        return await asyncio.gather(*[one(doc) for doc in docs])

    results = asyncio.run(run_all())
    t = Table(title=f"Checks (run {run_index}): fail / inconclusive / pass")
    for col in ("paper", "claims", "number", "dominance", "strength", "scope", "limitations", "checklist B/U/C", "€"):
        t.add_column(col, justify="right" if col != "paper" else "left")
    for doc, _, _, ks in results:
        save_checks(ks, s.var_dir)
        st = ks.stats

        def f(name: str, st=st) -> str:
            d = st.get(name, {})
            return f"{d.get('fail', 0)} / {d.get('inconclusive', 0)} / {d.get('pass', 0)}"

        cl = st.get("checklist", {})
        t.add_row(doc.paper_id, str(st["claims"]), f("number"), f("dominance"), f("strength"), f("scope"),
                  str(st["limitations"]), f"{cl.get('backed', 0)}/{cl.get('unbacked', 0)}/{cl.get('contradicted', 0)}",
                  f"{ks.cost['eur']:.4f}")
        for w in ks.warnings:
            console.print(f"  [yellow]{doc.paper_id}[/yellow] {w}")
    console.print(t)
    console.print(f"Spend (check calls): €{sum(ks.cost['eur'] for *_, ks in results):.4f} -> {s.var_dir / 'checks'}")
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_checks_report(results))
        console.print(f"wrote {report}")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def analyze() -> None:
    """Run the full pipeline over a data root (Phase 6)."""
    _todo(6)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def report() -> None:
    """Markdown summary of a results directory (Phase 6)."""
    _todo(6)


@eval_app.callback()
def eval_main(ctx: typer.Context) -> None:
    """Evaluate results against a split root; prints aggregates only (Phase 7)."""
    if ctx.invoked_subcommand is None:
        _todo(7)


@eval_app.command("merge-metrics", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def eval_merge_metrics() -> None:
    """Combine metrics directories and hand-label scores into metrics.json (Phase 7)."""
    _todo(7)


@labels_app.command("export", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def labels_export() -> None:
    """Blind, stratified hand-label sheet (Phase 7)."""
    _todo(7)


@labels_app.command("score", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def labels_score() -> None:
    """Precision from a completed label sheet (Phase 7)."""
    _todo(7)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def findings() -> None:
    """Corpus research questions RQ1-RQ4 (Phase 7)."""
    _todo(7)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def bundle() -> None:
    """Bundle results with display metadata (Phase 7)."""
    _todo(7)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def codecheck() -> None:
    """Static paper-code checks and sandboxed re-runs (Phase 10)."""
    _todo(10)


if __name__ == "__main__":
    app()
