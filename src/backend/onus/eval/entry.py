"""The `onus` console script: onus/cli.py's app plus the commands added after the P0 freeze.

cli.py is part of the frozen pipeline fingerprint, so post-freeze, result-neutral commands register here instead
(pyproject `[project.scripts] onus = "onus.eval.entry:app"`). eval/ is outside the pipeline fingerprint, and this
file is not one of the harness modules, so neither fingerprint moves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from onus.cli import app, console

__all__ = ["app"]


@app.command("export-numbers")
def export_numbers_cmd(
    metrics: Annotated[Path, typer.Option(help="metrics.json from `onus eval merge-metrics`")],
    findings: Annotated[Path, typer.Option(help="findings.json from `onus findings`")],
    out: Annotated[Path, typer.Option(help="Markdown file to write")],
    labels: Annotated[Path | None, typer.Option(help="labels_scores.json from `onus labels score` (optional)")] = None,
) -> None:
    """Every headline number with its 95% CI, n, source file and command -> Markdown (no LLM calls)."""
    from onus.eval.numbers import NumbersError, export_numbers

    try:
        text = export_numbers(metrics, findings, labels, out)
    except NumbersError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e
    sections = [ln[3:] for ln in text.splitlines() if ln.startswith("## ")]
    console.print(f"wrote {out}: {len(sections)} sections ({'; '.join(sections)})", markup=False, highlight=False)


@app.command("anchors-eval")
def anchors_eval_cmd(
    results: Annotated[Path, typer.Option(help="Results directory (RunResult JSON files)")],
    split_root: Annotated[Path, typer.Option("--split-root", help="Split root with papers/<id>/paper.pdf (dev)")],
    out: Annotated[Path, typer.Option(help="JSON file for the per-item scores and the summary")],
) -> None:
    """How well the report page's PDF highlights land: placement, page, method and token F1 per item."""
    import json

    from onus.config import get_settings
    from onus.eval.anchors_eval import render, run

    summary, rows, skipped = run(results, split_root, get_settings().var_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "skipped": skipped, "items": rows}, indent=1))
    console.print(render(summary, rows), markup=False, highlight=False, soft_wrap=True)
    for s in skipped:
        console.print(f"skipped {s}", markup=False)
    console.print(f"wrote {out} ({len(rows)} items)", markup=False)


@app.command("dev-seed")
def dev_seed_cmd(
    results: Annotated[Path, typer.Option(help="Results directory (RunResult JSON files)")],
    split_root: Annotated[Path, typer.Option("--split-root", help="Split root with papers/<id>/paper.pdf (dev)")],
    papers: Annotated[int, typer.Option(help="Analyses to seed with their PDF")] = 3,
    no_pdf: Annotated[bool, typer.Option("--no-pdf", help="Also seed one analysis without a stored PDF")] = False,
) -> None:
    """Import dev RunResults as local "done" analyses with stored PDFs (local SQLite in ONUS_VAR_DIR only)."""
    from onus.api.devseed import SeedError, seed
    from onus.config import get_settings

    try:
        done = seed(get_settings(), results, split_root, papers, no_pdf)
    except SeedError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e
    for x in done:
        v = ", ".join(f"{k} {n}" for k, n in sorted(x.verdicts.items()))
        console.print(f"{x.url}  {x.paper_id}  pdf={'yes' if x.pdf else 'no'}  {x.status}  ({v})", markup=False,
                      soft_wrap=True)


if __name__ == "__main__":
    app()
