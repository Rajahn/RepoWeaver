from __future__ import annotations

from repoweaver.explore import BLIND_SPOTS, explore


def test_explore_not_indexed_returns_error(tmp_path):
    result = explore(query="anything", repo=str(tmp_path))
    assert result == {"error": "not_indexed", "hint": "run: fabric build"}


def test_explore_understand_returns_slices_and_contract_shape(built_javademo):
    result = explore(query="Greeter", task="understand", repo=str(built_javademo))
    for key in ("query", "task", "repo", "slices", "stats", "blind_spots"):
        assert key in result
    assert result["blind_spots"] == BLIND_SPOTS
    assert result["slices"]
    assert result["stats"]["freshness"] == "ok"


def test_explore_impact_reports_blast_radius(built_javademo):
    result = explore(query="format", task="impact", repo=str(built_javademo))
    assert "blast_radius" in result
    assert (
        any(
            b["qualified_name"].endswith("greet(String)")
            for b in result["blast_radius"]
        )
        or result["blast_radius"]
    )


def test_explore_debug_returns_call_path(built_javademo):
    result = explore(query="App -> Formatter", task="debug", repo=str(built_javademo))
    assert "call_path" in result


def test_explore_token_budget_trims_slices(built_javademo):
    tight = explore(
        query="Greeter", task="understand", repo=str(built_javademo), max_tokens=1
    )
    wide = explore(
        query="Greeter",
        task="understand",
        repo=str(built_javademo),
        max_tokens=1_000_000,
    )
    assert len(tight["slices"]) <= len(wide["slices"])


def test_explore_freshness_flips_to_stale_after_edit(built_javademo):
    ok = explore(query="Greeter", task="locate", repo=str(built_javademo))
    assert ok["stats"]["freshness"] == "ok"

    target = built_javademo / "com/example/demo/Greeter.java"
    target.write_text(target.read_text() + "\n// edited\n")

    stale = explore(query="Greeter", task="locate", repo=str(built_javademo))
    assert stale["stats"]["freshness"] == "stale"
