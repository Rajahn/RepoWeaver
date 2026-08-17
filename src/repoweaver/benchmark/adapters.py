"""Benchmark adapters — a common interface so RepoWeaver can be measured
side-by-side with other tools without ever vendoring their code.

- `RepoWeaverAdapter` — the real, fully-implemented adapter for this project.
- `ExternalCommandAdapter` — a generic JSON protocol for any external tool:
  it shells out to a configured command and parses one JSON object from
  stdout. See docs/benchmark-methodology.md for the wire format.
- `CodeGraphAdapter` / `GraftAdapter` — presence-detection only. If the
  tool's CLI isn't on PATH, the run is SKIPPED. We never reimplement or copy
  their logic; we only *invoke* the binary the user already has installed.
- `GrepBaselineAdapter` — the "what would grep-only tooling see" baseline:
  symbol locate via text search, wall-clock time, and a byte/4 token proxy.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from repoweaver.benchmark.metrics import BenchmarkMetrics, collect_metrics


class AdapterSkipped(Exception):
    """Raised when an adapter's tool is not available; the run is SKIP, not FAIL."""


class BenchmarkAdapter(Protocol):
    name: str

    def run(self, repo_root: Path, name: str, workdir: Path) -> BenchmarkMetrics: ...


@dataclass
class RepoWeaverAdapter:
    """The real adapter: builds and measures RepoWeaver's own index."""

    name: str = "repoweaver"

    def run(self, repo_root: Path, name: str, workdir: Path) -> BenchmarkMetrics:
        return collect_metrics(repo_root, name=name, workdir=workdir, adapter=self.name)


@dataclass
class ExternalCommandAdapter:
    """Generic adapter for any external tool that speaks RepoWeaver's simple
    JSON benchmark protocol.

    Protocol: `command` is formatted with `{repo}` and `{workdir}`, run via
    the shell, and must print exactly one JSON object to stdout containing
    zero or more of the `BenchmarkMetrics` field names (unknown keys are
    ignored; missing keys stay `None` — never fabricated).
    """

    command: str
    name: str = "external"
    timeout_sec: float = 300.0

    def run(self, repo_root: Path, name: str, workdir: Path) -> BenchmarkMetrics:
        cmd = self.command.format(repo=repo_root, workdir=workdir)
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdapterSkipped(
                f"{self.name}: command failed to run — {exc}"
            ) from None
        elapsed = time.perf_counter() - started

        if proc.returncode != 0:
            raise AdapterSkipped(
                f"{self.name}: exited {proc.returncode}: {proc.stderr.strip()[:500]}"
            )
        try:
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise AdapterSkipped(
                f"{self.name}: did not print a JSON object — {exc}"
            ) from None

        known_fields = set(BenchmarkMetrics.__dataclass_fields__)
        filtered = {k: v for k, v in payload.items() if k in known_fields}
        filtered.setdefault("index_time_sec", elapsed)
        return BenchmarkMetrics(
            name=name, repo=str(repo_root), adapter=self.name, **filtered
        )


@dataclass
class _OptionalCLIAdapter:
    """Base for adapters that only detect whether a third-party CLI is
    installed. Never executes proprietary logic on RepoWeaver's behalf —
    presence-check only, so we never risk vendoring someone else's code."""

    cli_name: str
    name: str = "optional-cli"

    def run(self, repo_root: Path, name: str, workdir: Path) -> BenchmarkMetrics:
        if shutil.which(self.cli_name) is None:
            raise AdapterSkipped(
                f"{self.name}: '{self.cli_name}' not found on PATH — SKIP "
                "(install it yourself to benchmark it; RepoWeaver does not "
                "vendor or reimplement its logic)"
            )
        raise AdapterSkipped(
            f"{self.name}: '{self.cli_name}' is present but no benchmark "
            "invocation is configured — use ExternalCommandAdapter with an "
            "explicit `command` to actually run it"
        )


@dataclass
class CodeGraphAdapter(_OptionalCLIAdapter):
    cli_name: str = "codegraph"
    name: str = "codegraph"


@dataclass
class GraftAdapter(_OptionalCLIAdapter):
    cli_name: str = "graft"
    name: str = "graft"


@dataclass
class GrepBaselineAdapter:
    """What a grep-only agent would see: no graph, no confidence, no
    resolution — just "where does this literal name appear" and how much
    text an agent would have to read to use that answer."""

    name: str = "grep"

    def run(self, repo_root: Path, name: str, workdir: Path) -> BenchmarkMetrics:
        from repoweaver.benchmark.metrics import count_java_files, fixed_query_set
        from repoweaver.graph.store import GraphStore
        from repoweaver.indexer import Indexer

        java_files = count_java_files(repo_root)

        # Reuse RepoWeaver's own symbol list purely to pick a fair, repo-agnostic
        # query set for the grep baseline — grep itself does no indexing.
        tmp_db = workdir / "grep_probe.db"
        tmp_db.parent.mkdir(parents=True, exist_ok=True)
        with GraphStore(tmp_db) as store:
            Indexer(repo_root, store).build()
            queries = fixed_query_set(store)

        latencies = []
        tokens = []
        for query in queries:
            started = time.perf_counter()
            proc = subprocess.run(
                ["grep", "-rn", "--include=*.java", query, str(repo_root)],
                capture_output=True,
                text=True,
                check=False,
            )
            latencies.append((time.perf_counter() - started) * 1000)
            tokens.append(max(1, len(proc.stdout) // 4))

        from repoweaver.benchmark.metrics import QuerySample, summarize_query_samples

        samples = [
            QuerySample(query=q, latency_ms=lat, context_tokens=tok)
            for q, lat, tok in zip(queries, latencies, tokens, strict=True)
        ]
        summary = summarize_query_samples(samples)

        return BenchmarkMetrics(
            name=name,
            repo=str(repo_root),
            adapter=self.name,
            java_files=java_files,
            **summary,
        )


ADAPTERS: dict[str, type] = {
    "repoweaver": RepoWeaverAdapter,
    "codegraph": CodeGraphAdapter,
    "graft": GraftAdapter,
    "grep": GrepBaselineAdapter,
}


def build_adapter(spec: str) -> BenchmarkAdapter:
    """`spec` is an adapter name ("repoweaver", "grep", "codegraph", "graft")
    or "external:<shell command template>" for ExternalCommandAdapter."""
    if spec.startswith("external:"):
        return ExternalCommandAdapter(command=spec[len("external:") :])
    try:
        return ADAPTERS[spec]()
    except KeyError:
        raise ValueError(
            f"Unknown adapter '{spec}'. Choices: {sorted(ADAPTERS)} or 'external:<command>'."
        ) from None


__all__ = [
    "ADAPTERS",
    "AdapterSkipped",
    "BenchmarkAdapter",
    "CodeGraphAdapter",
    "ExternalCommandAdapter",
    "GraftAdapter",
    "GrepBaselineAdapter",
    "RepoWeaverAdapter",
    "build_adapter",
]
