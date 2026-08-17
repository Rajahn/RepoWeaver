"""Java parser — tree-sitter-based call-graph extractor for Java source."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class NodeRecord:
    """Represents a symbol node extracted from Java source."""

    kind: str  # e.g. "method", "class", "interface"
    language: str = "java"
    repo: str = ""
    file: str = ""
    span_start: int = 0
    span_end: int = 0
    qualified_name: str = ""
    simple_name: str = ""
    signature: str = ""
    commit_hash: str = ""


@dataclass
class EdgeRecord:
    """Represents a directed call/reference edge between two nodes."""

    from_id: str
    to_id: str
    type: str  # e.g. "calls", "implements", "extends"
    provenance: str = "static"
    confidence: float = 1.0
    source_hash: str = ""
    ambiguous_candidates: list[str] = field(default_factory=list)


class JavaParser:
    """
    Parses Java source files and emits NodeRecord / EdgeRecord streams.

    This is a stub implementation. Full tree-sitter integration is
    implemented in milestone T0.1.
    """

    def __init__(self, repo_root: str | Path = ".") -> None:
        self.repo_root = Path(repo_root)

    def parse_file(self, path: str | Path) -> tuple[list[NodeRecord], list[EdgeRecord]]:
        """
        Parse a single Java file and return (nodes, edges).

        Returns empty lists until the tree-sitter backend is wired up.
        """
        return [], []

    def walk_repo(self) -> Iterator[tuple[list[NodeRecord], list[EdgeRecord]]]:
        """
        Walk all ``*.java`` files under ``repo_root`` and yield (nodes, edges)
        for each file.
        """
        for java_file in self.repo_root.rglob("*.java"):
            yield self.parse_file(java_file)
