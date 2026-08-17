"""SQLite-backed graph store for RepoWeaver."""

from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path


_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class GraphStore:
    """
    Thin wrapper around an SQLite database that holds the call-graph.

    This is a stub implementation; full CRUD and query methods are
    added in milestone T0.1.
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> "GraphStore":
        """Open (or create) the database and apply the schema."""
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._apply_schema()
        return self

    def close(self) -> None:
        """Flush and close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "GraphStore":
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _apply_schema(self) -> None:
        assert self._conn is not None
        ddl = _SCHEMA_PATH.read_text(encoding="utf-8")
        self._conn.executescript(ddl)

    # ------------------------------------------------------------------
    # Stub read/write helpers (implemented in T0.1)
    # ------------------------------------------------------------------

    def node_count(self) -> int:
        """Return the number of indexed nodes."""
        assert self._conn is not None
        (count,) = self._conn.execute("SELECT COUNT(*) FROM node").fetchone()
        return int(count)

    def edge_count(self) -> int:
        """Return the number of indexed edges."""
        assert self._conn is not None
        (count,) = self._conn.execute("SELECT COUNT(*) FROM edge").fetchone()
        return int(count)

    def is_fresh(self) -> bool:
        """
        Return True if the index is up-to-date.

        Freshness check is a stub — always returns False until T0.1.
        """
        return False
