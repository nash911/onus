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


if __name__ == "__main__":
    app()
