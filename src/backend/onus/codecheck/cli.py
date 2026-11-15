"""`onus codecheck ...`: code-check commands (Phase 10).

A Typer sub-app registered from onus/cli.py, so Phase 10 can add commands here without touching cli.py, which is
part of the P0 pipeline fingerprint. codecheck/ is excluded from that fingerprint (code-v1 is versioned
separately).
"""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer(help="Static paper-code checks and sandboxed re-runs (Phase 10).", invoke_without_command=True,
                  context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
console = Console()


@app.callback()
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        console.print("[yellow]Not implemented yet: built in Phase 10.[/yellow]")
        raise typer.Exit(code=2)


def freeze_code(code_version: str) -> None:
    """`onus freeze --code-version X.Y.Z`: pin the code-v1 fingerprint (Phase 10)."""
    console.print(f"[yellow]Not implemented yet: built in Phase 10 (code-v1 {code_version}).[/yellow]")
    raise typer.Exit(code=2)
