"""`fabric benchmark report` — render a metrics JSON (optionally with a gate
comparison) as Markdown."""

from __future__ import annotations

from repoweaver.benchmark.compare import (
    ComparisonResult,
    evaluate_gates,
    load_release_gates,
)

_METRIC_ROWS: list[tuple[str, str]] = [
    ("java_files", "Java files"),
    ("symbol_files", "Symbol-bearing files"),
    ("parse_error_count", "Parse errors"),
    ("parse_error_rate", "Parse error rate"),
    ("nodes", "Nodes"),
    ("edges_total", "Edges (total)"),
    ("edges_resolved", "Edges resolved (conf>=0.5, non-ambiguous)"),
    ("edges_ambiguous", "Edges ambiguous"),
    ("ambiguous_edge_rate", "Ambiguous edge rate"),
    ("cross_file_dependent_total", "Symbol-bearing files (denominator)"),
    ("cross_file_dependent_resolved", "Files with resolved incoming cross-file edge"),
    ("cross_file_dependent_coverage", "Cross-file dependent coverage"),
    ("index_time_sec", "Index time (s)"),
    ("db_size_bytes", "DB size (bytes)"),
    ("query_latency_ms_p50", "Query latency p50 (ms)"),
    ("query_latency_ms_p95", "Query latency p95 (ms)"),
    ("context_tokens_p50", "Context tokens p50"),
    ("context_tokens_p95", "Context tokens p95"),
    ("deterministic_rebuild", "Deterministic rebuild"),
    ("deterministic_rebuild_hash", "Rebuild hash"),
]


def _fmt(value: object) -> str:
    if value is None:
        return "_not measured_"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_metrics_table(candidate: dict) -> list[str]:
    lines = ["| Metric | Value |", "|---|---|"]
    for key, label in _METRIC_ROWS:
        lines.append(f"| {label} | {_fmt(candidate.get(key))} |")
    return lines


def render_correctness_table(candidate: dict) -> list[str]:
    correctness = candidate.get("correctness")
    if not correctness:
        return []
    lines = ["", "## Correctness (ground truth)", "", "| Metric | Value |", "|---|---|"]
    for key in (
        "node_recall",
        "edge_precision",
        "edge_recall",
        "expected_ambiguous_correctly_flagged_rate",
        "query_topk_recall",
        "query_mrr",
    ):
        lines.append(f"| {key} | {_fmt(correctness.get(key))} |")
    return lines


def render_gates_table(comparison: ComparisonResult) -> list[str]:
    lines = [
        "",
        f"## Release gates — **{comparison.status}**",
        "",
        "| Gate | Status | Op | Target | Value | Gap |",
        "|---|---|---|---|---|---|",
    ]
    for gate in comparison.gates:
        lines.append(
            f"| {gate.key} | {gate.status} | {gate.op} | {_fmt(gate.target)} "
            f"| {_fmt(gate.value)} | {_fmt(gate.gap)} |"
        )
    return lines


def render_report(candidate: dict, targets_path: str | None = None) -> str:
    lines = [f"# Benchmark report — {candidate.get('name', 'unnamed')}", ""]
    lines.append(f"- Repo: `{candidate.get('repo', '?')}`")
    lines.append(f"- Adapter: `{candidate.get('adapter', '?')}`")
    lines.append(f"- Status: `{candidate.get('status', 'UNKNOWN')}`")
    scope = candidate.get("scope_prefixes")
    if scope:
        lines.append(f"- Scope prefixes: `{', '.join(scope)}`")
    lines.append("")
    lines.append("## Metrics")
    lines.append("")
    lines.extend(render_metrics_table(candidate))
    lines.extend(render_correctness_table(candidate))

    if targets_path is not None:
        gates = load_release_gates(targets_path)
        comparison = evaluate_gates(candidate, gates)
        lines.extend(render_gates_table(comparison))

    return "\n".join(lines) + "\n"


__all__ = ["render_report"]
