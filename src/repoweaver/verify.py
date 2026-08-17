"""`fabric verify` — milestone verification suite.

M1 checks that the whole closed loop actually works end-to-end against the
bundled Java fixture: parsing, edge resolution, retrieval, freshness, the
explore() contract shape, and token-budget trimming. It is deliberately not a
unit test — it's a black-box smoke test of the built CLI/library, meant to
catch "works in pytest, broken for real users" gaps.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from repoweaver.explore import BLIND_SPOTS, explore
from repoweaver.graph.store import GraphStore
from repoweaver.indexer import Indexer

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "javademo"


@dataclass
class VerifyResult:
    passed: bool
    report_lines: list[str] = field(default_factory=list)


class _Check:
    def __init__(self, report: list[str]) -> None:
        self.report = report
        self.failures = 0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.report.append(f"  [PASS] {name}")
        else:
            self.failures += 1
            suffix = f" — {detail}" if detail else ""
            self.report.append(f"  [FAIL] {name}{suffix}")


@contextmanager
def _built_fixture_repo():
    with tempfile.TemporaryDirectory(prefix="repoweaver-verify-") as tmp:
        repo_root = Path(tmp) / "javademo"
        shutil.copytree(_FIXTURE, repo_root)
        db_path = repo_root / ".repoweaver" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with GraphStore(db_path) as store:
            Indexer(repo_root, store).build()
        yield repo_root


def run_verification(level: str, repo: Path) -> VerifyResult:
    if level != "m1":
        return VerifyResult(
            False,
            [f"Level '{level}' is not implemented in M1. Only 'm1' is supported."],
        )

    report: list[str] = [f"RepoWeaver verify --level m1  (fixture: {_FIXTURE})"]
    c = _Check(report)

    if not _FIXTURE.exists():
        c.check("fixture present", False, str(_FIXTURE))
        report.append("FAIL — cannot continue without fixture")
        return VerifyResult(False, report)

    with _built_fixture_repo() as repo_root:
        db_path = repo_root / ".repoweaver" / "graph.db"

        with GraphStore(db_path) as store:
            report.append("-- parsing & node extraction --")
            classes = [
                node
                for node in store.find_by_simple_name("EnglishGreeter")
                if node["kind"] == "class"
            ]
            c.check("class EnglishGreeter indexed", len(classes) == 1)
            greeter_iface = store.find_by_qualified_name("com.example.demo.Greeter")
            c.check(
                "interface Greeter indexed",
                len(greeter_iface) == 1 and greeter_iface[0]["kind"] == "interface",
            )
            level_enum = store.find_by_qualified_name("com.example.demo.Level")
            c.check(
                "enum Level indexed",
                len(level_enum) == 1 and level_enum[0]["kind"] == "enum",
            )
            methods = store.find_by_simple_name("greet")
            c.check("method greet() indexed (>=2 overrides)", len(methods) >= 2)
            all_ctors = store.conn.execute(
                "SELECT COUNT(*) FROM node WHERE kind = 'constructor'"
            ).fetchone()[0]
            c.check("at least one constructor indexed", all_ctors >= 1, str(all_ctors))
            fields = store.conn.execute(
                "SELECT COUNT(*) FROM node WHERE kind = 'field'"
            ).fetchone()[0]
            c.check("at least one field indexed", fields >= 1, str(fields))

            report.append("-- edge resolution --")
            stats = store.stats()
            edge_types = stats["edge_types"]
            c.check(
                "IMPLEMENTS edges resolved",
                edge_types.get("IMPLEMENTS", 0) >= 2,
                str(edge_types),
            )
            c.check(
                "EXTENDS edges resolved",
                edge_types.get("EXTENDS", 0) >= 1,
                str(edge_types),
            )
            c.check(
                "CALLS edges resolved", edge_types.get("CALLS", 0) >= 3, str(edge_types)
            )
            c.check(
                "IMPORTS edges resolved",
                edge_types.get("IMPORTS", 0) >= 1,
                str(edge_types),
            )

            english_greeter = store.find_by_qualified_name(
                "com.example.demo.EnglishGreeter"
            )[0]
            implements_edges = [
                e
                for e in store.neighbors(english_greeter["id"], "out", 0.0)
                if e[1] == "IMPLEMENTS"
            ]
            c.check(
                "EnglishGreeter --IMPLEMENTS--> Greeter",
                any(
                    e[0]["qualified_name"] == "com.example.demo.Greeter"
                    for e in implements_edges
                ),
            )

        report.append("-- retrieval (FTS + PPR) --")
        response = explore(query="Greeter", task="understand", repo=str(repo_root))
        c.check(
            "explore() returns slices for 'Greeter'",
            len(response.get("slices", [])) > 0,
        )
        hit_files = {s["file"] for s in response.get("slices", [])}
        c.check(
            "retrieval finds Greeter.java",
            any("Greeter.java" in f for f in hit_files),
            str(hit_files),
        )

        report.append("-- freshness --")
        response_fresh = explore(query="Greeter", task="locate", repo=str(repo_root))
        c.check(
            "freshness reports 'ok' right after build",
            response_fresh["stats"]["freshness"] == "ok",
        )
        (repo_root / "com/example/demo/Greeter.java").write_text(
            (repo_root / "com/example/demo/Greeter.java").read_text() + "\n// touched\n"
        )
        response_stale = explore(query="Greeter", task="locate", repo=str(repo_root))
        c.check(
            "freshness reports 'stale' after edit",
            response_stale["stats"]["freshness"] == "stale",
        )

        report.append("-- explore() contract shape --")
        for key in ("query", "task", "repo", "slices", "stats", "blind_spots"):
            c.check(f"response has key '{key}'", key in response)
        c.check(
            "blind_spots matches frozen contract string",
            response.get("blind_spots") == BLIND_SPOTS,
        )
        for key in (
            "nodes_visited",
            "edges_traversed",
            "tokens_estimated",
            "freshness",
        ):
            c.check(f"stats has key '{key}'", key in response.get("stats", {}))

        report.append("-- impact / debug tasks --")
        impact = explore(query="format", task="impact", repo=str(repo_root))
        c.check("impact task returns blast_radius key", "blast_radius" in impact)
        debug = explore(query="App -> Formatter", task="debug", repo=str(repo_root))
        c.check("debug task returns call_path key", "call_path" in debug)

        report.append("-- token budget --")
        tight = explore(
            query="Greeter", task="understand", repo=str(repo_root), max_tokens=1
        )
        est = tight["stats"]["tokens_estimated"]
        c.check(
            "token budget trims response to the requested budget",
            est <= 1 and len(tight["slices"]) <= 1,
            str(est),
        )
        wide = explore(
            query="Greeter", task="understand", repo=str(repo_root), max_tokens=100000
        )
        c.check(
            "wider budget yields at least as many slices",
            len(wide["slices"]) >= len(tight["slices"]),
        )

    passed = c.failures == 0
    report.append("")
    report.append("PASS" if passed else f"FAIL ({c.failures} check(s) failed)")
    return VerifyResult(passed, report)
