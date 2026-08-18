"""Shared CLI error reporting — used by both `fabric.cli` and `fabric overlay`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import typer


def exit_on_locked_db(db_path: Path, exc: sqlite3.OperationalError) -> None:
    print(
        f"error: could not access {db_path} ({exc}). "
        "Another `fabric build`/`fabric watch` process is likely holding a "
        "lock on it — wait for it to finish, or stop it, then retry."
    )
    raise typer.Exit(code=1)
