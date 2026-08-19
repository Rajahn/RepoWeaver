"""`fabric benchmark compare` — evaluate a candidate metrics JSON against
`release_gates` from sota-targets.yaml. PASS only if every required gate
passes. A gate whose metric is `null` is reported SKIP, and the overall result
is FAIL: missing evidence must never silently satisfy a release gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_OPS = {
    "<=": lambda value, target: value <= target,
    ">=": lambda value, target: value >= target,
    "==": lambda value, target: value == target,
    "<": lambda value, target: value < target,
    ">": lambda value, target: value > target,
}

# Maps a release-gate key to where its value lives in the candidate JSON.
_GATE_PATHS: dict[str, tuple[str, ...]] = {
    "parse_error_rate": ("parse_error_rate",),
    "ambiguous_edge_rate": ("ambiguous_edge_rate",),
    "cross_file_dependent_coverage": ("cross_file_dependent_coverage",),
    "fixture_node_recall": ("correctness", "node_recall"),
    "fixture_edge_precision": ("correctness", "edge_precision"),
    "fixture_edge_recall": ("correctness", "edge_recall"),
    "deterministic_rebuild": ("deterministic_rebuild",),
}


@dataclass
class GateResult:
    key: str
    status: str  # PASS | FAIL | SKIP
    op: str
    target: object
    value: object
    gap: float | None
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "status": self.status,
            "op": self.op,
            "target": self.target,
            "value": self.value,
            "gap": self.gap,
            "description": self.description,
        }


@dataclass
class ComparisonResult:
    status: str  # PASS | FAIL
    gates: list[GateResult]

    def to_dict(self) -> dict:
        return {"status": self.status, "gates": [g.to_dict() for g in self.gates]}


def _lookup(candidate: dict, path: tuple[str, ...]) -> object:
    value = candidate
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def load_release_gates(target_path: str | Path) -> dict:
    data = yaml.safe_load(Path(target_path).read_text(encoding="utf-8"))
    return data.get("release_gates", data)


def evaluate_gates(candidate: dict, gates: dict) -> ComparisonResult:
    results: list[GateResult] = []
    for key, gate in gates.items():
        path = _GATE_PATHS.get(key, (key,))
        value = _lookup(candidate, path)
        op = gate["op"]
        target = gate["value"]
        description = gate.get("description", "")

        if value is None:
            results.append(GateResult(key, "SKIP", op, target, None, None, description))
            continue

        passed = _OPS[op](value, target)
        gap = (
            (value - target)
            if isinstance(value, (int, float)) and isinstance(target, (int, float))
            else None
        )
        results.append(
            GateResult(
                key, "PASS" if passed else "FAIL", op, target, value, gap, description
            )
        )

    overall = "PASS" if results and all(g.status == "PASS" for g in results) else "FAIL"
    return ComparisonResult(status=overall, gates=results)


def compare(candidate: dict, target_path: str | Path) -> ComparisonResult:
    gates = load_release_gates(target_path)
    return evaluate_gates(candidate, gates)


__all__ = [
    "ComparisonResult",
    "GateResult",
    "compare",
    "evaluate_gates",
    "load_release_gates",
]
