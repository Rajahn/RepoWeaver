"""`fabric benchmark` sub-commands: run, compare, report."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from repoweaver.benchmark.compare import compare as compare_metrics
from repoweaver.benchmark.report import render_report
from repoweaver.benchmark.runner import run_benchmark

app = typer.Typer(
    name="benchmark",
    help="Measure and compare RepoWeaver against SOTA alignment targets.",
    no_args_is_help=True,
)


@app.command()
def run(
    repo: str = typer.Option(
        ..., "--repo", help="Path to the repository to benchmark."
    ),
    name: str = typer.Option(..., "--name", help="Name for this benchmark run."),
    output: str = typer.Option(..., "--output", help="Path to write the result JSON."),
    adapter: str = typer.Option(
        "repoweaver",
        "--adapter",
        help="repoweaver | grep | codegraph | graft | external:<shell command>",
    ),
    ground_truth: str | None = typer.Option(
        None,
        "--ground-truth",
        help="Path to a ground_truth.yaml to score correctness against.",
    ),
    scope_prefix: Annotated[
        list[str] | None,
        typer.Option(
            "--scope-prefix",
            help="Repo-relative source prefix; repeat to define an apples-to-apples scope.",
        ),
    ] = None,
) -> None:
    """Run a benchmark and write metrics JSON to --output."""
    result = run_benchmark(
        repo=Path(repo),
        name=name,
        adapter=adapter,
        ground_truth=Path(ground_truth) if ground_truth else None,
        scope_prefixes=scope_prefix,
    )
    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(f"[{result.get('status', 'UNKNOWN')}] {name} -> {out_path}")
    if result.get("status") == "SKIP":
        print(f"  reason: {result.get('reason')}")


@app.command()
def compare(
    candidate: str = typer.Option(
        ..., "--candidate", help="Path to a metrics JSON from `benchmark run`."
    ),
    target: str = typer.Option(
        ...,
        "--target",
        help="Path to sota-targets.yaml (or a release_gates YAML/JSON).",
    ),
) -> None:
    """Compare a candidate metrics JSON against release gates; exits non-zero on FAIL."""
    candidate_data = json.loads(Path(candidate).read_text(encoding="utf-8"))
    result = compare_metrics(candidate_data, target)

    print(f"{result.status}")
    for gate in result.gates:
        print(
            f"  [{gate.status}] {gate.key}: value={gate.value} {gate.op} target={gate.target}"
            + (f" (gap={gate.gap:+.4f})" if gate.gap is not None else "")
        )

    if result.status == "FAIL":
        raise typer.Exit(code=1)


@app.command()
def report(
    input_: str = typer.Option(
        ..., "--input", help="Path to a metrics JSON from `benchmark run`."
    ),
    output: str = typer.Option(
        ..., "--output", help="Path to write the Markdown report."
    ),
    targets: str | None = typer.Option(
        None,
        "--targets",
        help="Optional sota-targets.yaml to embed a release-gates table.",
    ),
) -> None:
    """Render a metrics JSON as a Markdown report."""
    candidate_data = json.loads(Path(input_).read_text(encoding="utf-8"))
    markdown = render_report(candidate_data, targets_path=targets)
    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    print(f"-> {out_path}")


__all__ = ["app"]
