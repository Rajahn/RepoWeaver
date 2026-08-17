"""RepoWeaver CLI — fabric command entry point."""

from __future__ import annotations

from typing import Optional

import typer

from repoweaver import __version__

app = typer.Typer(
    name="fabric",
    help="RepoWeaver: code context fabric for AI coding agents.",
    no_args_is_help=True,
)


@app.command()
def build(
    repo: str = typer.Argument(".", help="Path to the repository root."),
) -> None:
    """Build (or rebuild) the call-graph index for a repository."""
    print("not implemented yet")


@app.command()
def check(
    repo: str = typer.Argument(".", help="Path to the repository root."),
) -> None:
    """Check whether the index is fresh; exits non-zero if STALE."""
    print("not implemented yet")


@app.command()
def init(
    repo: str = typer.Argument(".", help="Path to the repository root."),
) -> None:
    """Initialise a new RepoWeaver workspace (creates .repoweaver/ directory)."""
    print("not implemented yet")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Host to bind the MCP server."),
    port: int = typer.Option(8000, help="Port to bind the MCP server."),
) -> None:
    """Start the MCP server exposing the explore() tool."""
    print("not implemented yet")


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
    print("not implemented yet")


@app.command()
def version() -> None:
    """Print the installed RepoWeaver version."""
    print(__version__)
