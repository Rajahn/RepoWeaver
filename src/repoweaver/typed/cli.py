"""`fabric overlay` sub-commands: scip."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import typer

from repoweaver.cli_errors import exit_on_locked_db
from repoweaver.typed.overlay import run_overlay

app = typer.Typer(
    name="overlay",
    help="Layer compiler-derived typed edges onto the tree-sitter fabric.",
    no_args_is_help=True,
)


@app.command()
def scip(
    repo: str = typer.Option(".", "--repo", help="Path to the repository root."),
    index: str = typer.Option(..., "--index", help="Path to a SCIP index.scip file."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report stats without writing to the graph."
    ),
) -> None:
    """Merge a SCIP index into the repo's call-graph as *_TYPED edges."""
    repo_root = Path(repo).resolve()
    index_path = Path(index).resolve()
    db_path = repo_root / ".repoweaver" / "graph.db"
    try:
        stats = run_overlay(repo_root, index_path, dry_run=dry_run)
    except ValueError as exc:
        print(f"error: could not read SCIP index {index_path} — {exc}")
        raise typer.Exit(code=1) from None
    except sqlite3.OperationalError as exc:
        exit_on_locked_db(db_path, exc)
    print(json.dumps(stats.as_dict(), indent=2, sort_keys=True))
