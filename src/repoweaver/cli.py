"""RepoWeaver CLI — fabric command entry point."""

from __future__ import annotations

from pathlib import Path

import typer

from repoweaver import __version__
from repoweaver.benchmark.cli import app as benchmark_app
from repoweaver.graph.store import GraphStore
from repoweaver.indexer import Indexer
from repoweaver.protocol import inject_agents_md

app = typer.Typer(
    name="fabric",
    help="RepoWeaver: code context fabric for AI coding agents.",
    no_args_is_help=True,
)

app.add_typer(benchmark_app, name="benchmark")


def _db_path(repo_root: Path) -> Path:
    return repo_root / ".repoweaver" / "graph.db"


@app.command()
def build(
    repo: str = typer.Argument(".", help="Path to the repository root."),
) -> None:
    """Build (or rebuild) the call-graph index for a repository."""
    repo_root = Path(repo).resolve()
    db_path = _db_path(repo_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with GraphStore(db_path) as store:
        stats = Indexer(repo_root, store).build()

    print(
        f"Indexed {stats.files} file(s): {stats.nodes} node(s), "
        f"{stats.edges} edge(s) in {stats.elapsed_seconds:.2f}s"
    )
    print(f"-> {db_path}")


@app.command()
def check(
    repo: str = typer.Argument(".", help="Path to the repository root."),
) -> None:
    """Check whether the index is fresh; exits non-zero if STALE."""
    repo_root = Path(repo).resolve()
    db_path = _db_path(repo_root)
    if not db_path.exists():
        print("STALE (not indexed — run: fabric build)")
        raise typer.Exit(code=1)

    with GraphStore(db_path) as store:
        indexer = Indexer(repo_root, store)
        fresh, stale_files = store.is_fresh(indexer.current_file_hashes())

    if fresh:
        print("OK")
        return

    print("STALE")
    for f in stale_files:
        print(f"  {f}")
    raise typer.Exit(code=1)


@app.command()
def init(
    repo: str = typer.Argument(".", help="Path to the repository root."),
) -> None:
    """Initialise a new RepoWeaver workspace (creates .repoweaver/ directory)."""
    repo_root = Path(repo).resolve()
    (repo_root / ".repoweaver").mkdir(parents=True, exist_ok=True)

    agents_path = repo_root / "AGENTS.md"
    changed = inject_agents_md(agents_path)

    print(f"Workspace ready: {repo_root / '.repoweaver'}")
    print(f"AGENTS.md {'updated' if changed else 'already up to date'}: {agents_path}")
    print("Next: fabric build")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Host to bind the MCP server."),
    port: int = typer.Option(8000, help="Port to bind the MCP server."),
) -> None:
    """Start the MCP server exposing the explore() tool."""
    from repoweaver.server.mcp import mcp

    mcp.run(transport="http", host=host, port=port)


@app.command()
def watch(
    repo: str = typer.Argument(".", help="Path to the repository root."),
    debounce_ms: int = typer.Option(
        2000, "--debounce-ms", help="Debounce window for batching filesystem events."
    ),
) -> None:
    """Watch a repository and keep the index fresh as `*.java` files change."""
    from repoweaver.watcher import watch_and_sync

    repo_root = Path(repo).resolve()
    db_path = _db_path(repo_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        print(f"No index found at {db_path} — running full build first.")
        with GraphStore(db_path) as store:
            stats = Indexer(repo_root, store).build()
        print(
            f"Indexed {stats.files} file(s): {stats.nodes} node(s), "
            f"{stats.edges} edge(s) in {stats.elapsed_seconds:.2f}s"
        )

    def _report(changed: set[str], deleted: set[str], stats) -> None:
        print(
            f"sync: changed={len(changed)} deleted={len(deleted)} "
            f"files={stats.files} nodes={stats.nodes} edges={stats.edges} "
            f"unresolved={stats.unresolved} elapsed={stats.elapsed_seconds:.2f}s"
        )

    print(f"Watching {repo_root} (debounce={debounce_ms}ms). Ctrl-C to stop.")
    with GraphStore(db_path) as store:
        try:
            watch_and_sync(repo_root, store, debounce_ms=debounce_ms, on_sync=_report)
        except KeyboardInterrupt:
            print("Stopped.")


@app.command()
def verify(
    level: str = typer.Option(
        "m1",
        "--level",
        help="Verification milestone: m1 | m2 | m3 | m4.",
    ),
    repo: str = typer.Argument(".", help="Path to the repository root."),
) -> None:
    """Run milestone verification suite against the index."""
    from repoweaver.verify import run_verification

    result = run_verification(level, Path(repo).resolve())
    for line in result.report_lines:
        print(line)
    if not result.passed:
        raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print the installed RepoWeaver version."""
    print(__version__)
