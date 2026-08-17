"""explore() — the single public MCP tool. See docs/explore-contract.md (frozen v1)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from repoweaver.graph.store import GraphStore
from repoweaver.indexer import Indexer
from repoweaver.search.engine import SearchEngine, SearchQuery, personalized_pagerank

BLIND_SPOTS = (
    "Static analysis only. Not represented: Spring bean injection dispatch beyond "
    "declared type, MQ listener call targets, reflection, config-driven routing, "
    'generated code (MyBatis Example, etc.). "No callers found" != dead code. '
    "Always verify with grep/source before concluding."
)

Task = Literal["understand", "impact", "locate", "debug"]


def db_path_for(repo: str) -> Path:
    return Path(repo).resolve() / ".repoweaver" / "graph.db"


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _read_slice(repo_root: Path, file: str, span_start: int, span_end: int) -> str:
    try:
        lines = (
            (repo_root / file)
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        return ""
    start = max(span_start - 1, 0)
    end = min(span_end, len(lines))
    return "\n".join(lines[start:end])


def _make_slice(
    repo_root: Path, node: dict, confidence: float, provenance: str
) -> dict:
    return {
        "node_id": node["id"],
        "file": node["file"],
        "span_start": node["span_start"],
        "span_end": node["span_end"],
        "source": _read_slice(
            repo_root, node["file"], node["span_start"], node["span_end"]
        ),
        "qualified_name": node["qualified_name"],
        "confidence": confidence,
        "provenance": provenance,
        # v1.1 additive (M2) — absent/false on any node predating entry-point
        # detection; existing v1 consumers that don't read this key are
        # unaffected.
        "entry_point": bool(node.get("is_entry_point")),
        "entry_point_kind": node.get("entry_point_kind") or "",
    }


def _trim_to_budget(slices: list[dict], max_tokens: int) -> list[dict]:
    """Keep whole source lines while respecting the soft token budget.

    A single class can span thousands of lines. Returning it whole would make
    ``max_tokens`` meaningless, so the first oversized slice is shortened at a
    line boundary and marked as truncated. The source remains verbatim and the
    reported ``span_end`` is adjusted to match the returned lines.
    """
    out: list[dict] = []
    used = 0
    budget = max(1, max_tokens)

    for original in slices:
        remaining = budget - used
        if remaining <= 0:
            break

        source_lines = original["source"].splitlines()
        kept: list[str] = []
        for line in source_lines:
            candidate = "\n".join([*kept, line])
            if _estimate_tokens(candidate) > remaining:
                if not kept:
                    # A pathological single long line: retain a verbatim prefix
                    # rather than violating the caller's budget.
                    kept.append(line[: remaining * 4])
                break
            kept.append(line)

        if not kept:
            break

        item = dict(original)
        item["source"] = "\n".join(kept)
        if len(kept) < len(source_lines) or kept[-1] != source_lines[len(kept) - 1]:
            item["truncated"] = True
            item["span_end"] = item["span_start"] + len(kept) - 1
        out.append(item)
        used += _estimate_tokens(item["source"])

    return out


def _base_response(query: str, task: str, repo: str) -> dict:
    return {
        "query": query,
        "task": task,
        "repo": repo,
        "slices": [],
        "stats": {
            "nodes_visited": 0,
            "edges_traversed": 0,
            "tokens_estimated": 0,
            "freshness": "ok",
        },
        "blind_spots": BLIND_SPOTS,
    }


def _freshness(store: GraphStore, repo_root: Path) -> str:
    indexer = Indexer(repo_root, store)
    fresh, _stale = store.is_fresh(indexer.current_file_hashes())
    return "ok" if fresh else "stale"


def explore(
    query: str,
    task: Task = "understand",
    repo: str = ".",
    max_tokens: int = 4000,
    depth: int = 2,
    min_confidence: float = 0.5,
) -> dict:
    repo_root = Path(repo).resolve()
    db_path = db_path_for(repo)
    if not db_path.exists():
        return {"error": "not_indexed", "hint": "run: fabric build"}

    depth = max(0, min(depth, 4))

    with GraphStore(db_path) as store:
        freshness = _freshness(store, repo_root)
        engine = SearchEngine(store)
        seeds = engine.search(
            SearchQuery(
                query=query,
                max_results=30,
                min_confidence=min_confidence,
                depth=depth,
                task=task,
            )
        )

        response = _base_response(query, task, repo)
        response["stats"]["freshness"] = freshness

        if not seeds:
            return response

        ambiguity = _check_ambiguity(query, seeds)
        if ambiguity is not None and task != "debug":
            response["candidates"] = ambiguity
            return response

        if task == "impact":
            _fill_impact(
                response, store, repo_root, seeds[0], depth, min_confidence, max_tokens
            )
        elif task == "debug":
            _fill_debug(
                response,
                store,
                repo_root,
                query,
                seeds,
                depth,
                min_confidence,
                max_tokens,
            )
        else:
            _fill_understand_or_locate(
                response, store, repo_root, seeds, depth, min_confidence, max_tokens
            )

        return response


def _check_ambiguity(query: str, seeds: list) -> list[dict] | None:
    bare = query.strip()
    if not bare or any(c in bare for c in " ()#."):
        return None  # only bare symbol-name queries are candidates for disambiguation
    distinct_qnames = {s.qualified_name for s in seeds if s.simple_name == bare}
    if len(distinct_qnames) < 2:
        return None
    top = sorted(
        (s for s in seeds if s.simple_name == bare), key=lambda s: s.score, reverse=True
    )
    if len(top) >= 2 and top[0].score > top[1].score * 1.5:
        return None  # a clear winner — not ambiguous
    return [
        {
            "node_id": s.node_id,
            "qualified_name": s.qualified_name,
            "file": s.file,
            "score": s.score,
        }
        for s in top
    ]


def _fill_understand_or_locate(
    response, store, repo_root, seeds, depth, min_confidence, max_tokens
) -> None:
    slices: list[dict] = []
    visited: set[str] = set()
    nodes_visited = 0
    edges_traversed = 0

    for seed in seeds:
        if seed.node_id in visited:
            continue
        visited.add(seed.node_id)
        node = store.get_node(seed.node_id)
        if node is None:
            continue
        nodes_visited += 1
        slices.append(
            _make_slice(repo_root, node, confidence=1.0, provenance="tree_sitter_java")
        )

        for neighbor, edge_type, confidence in store.neighbors(
            seed.node_id, "out", min_confidence
        ):
            edges_traversed += 1
            if neighbor["id"] in visited:
                continue
            visited.add(neighbor["id"])
            nodes_visited += 1
            slices.append(
                _make_slice(repo_root, neighbor, confidence, "tree_sitter_java")
            )

    response["slices"] = _trim_to_budget(slices, max_tokens)
    response["stats"]["nodes_visited"] = nodes_visited
    response["stats"]["edges_traversed"] = edges_traversed
    response["stats"]["tokens_estimated"] = sum(
        _estimate_tokens(s["source"]) for s in response["slices"]
    )


def _fill_impact(
    response, store, repo_root, seed, depth, min_confidence, max_tokens
) -> None:
    seed_node = store.get_node(seed.node_id)
    if seed_node is None:
        return

    slices = [_make_slice(repo_root, seed_node, 1.0, "tree_sitter_java")]
    blast_radius: list[dict] = []
    visited = {seed.node_id}
    frontier = [seed.node_id]
    nodes_visited = 1
    edges_traversed = 0

    for hop in range(1, depth + 1):
        next_frontier = []
        for nid in frontier:
            for caller, edge_type, confidence in store.neighbors(
                nid, "in", min_confidence
            ):
                edges_traversed += 1
                if caller["id"] in visited:
                    continue
                visited.add(caller["id"])
                nodes_visited += 1
                next_frontier.append(caller["id"])
                risk = _risk_level(hop, confidence)
                blast_radius.append(
                    {
                        "depth": hop,
                        "node_id": caller["id"],
                        "qualified_name": caller["qualified_name"],
                        "file": caller["file"],
                        "edge_type": edge_type,
                        "confidence": confidence,
                        "risk": risk,
                    }
                )
                slices.append(
                    _make_slice(repo_root, caller, confidence, "tree_sitter_java")
                )
        frontier = next_frontier
        if not frontier:
            break

    response["slices"] = _trim_to_budget(slices, max_tokens)
    response["blast_radius"] = blast_radius
    response["stats"]["nodes_visited"] = nodes_visited
    response["stats"]["edges_traversed"] = edges_traversed
    response["stats"]["tokens_estimated"] = sum(
        _estimate_tokens(s["source"]) for s in response["slices"]
    )


def _risk_level(depth: int, confidence: float) -> str:
    if depth == 1 and confidence >= 0.8:
        return "will_break"
    if depth <= 2 and confidence >= 0.5:
        return "likely_affected"
    return "possible"


_DEBUG_SPLIT = re.compile(r"\s*(?:->|to|→)\s*", re.IGNORECASE)


def _fill_debug(
    response, store, repo_root, query, seeds, depth, min_confidence, max_tokens
) -> None:
    parts = [p for p in _DEBUG_SPLIT.split(query.strip()) if p]
    from_seed = seeds[0]
    to_seed = None
    if len(parts) >= 2:
        engine = SearchEngine(store)
        to_hits = engine.search(
            SearchQuery(
                query=parts[-1],
                max_results=1,
                min_confidence=min_confidence,
                depth=depth,
                task="debug",
            )
        )
        if to_hits:
            to_seed = to_hits[0]

    if to_seed is None:
        _fill_understand_or_locate(
            response, store, repo_root, seeds, depth, min_confidence, max_tokens
        )
        response["call_path"] = []
        return

    path, nodes_visited, edges_traversed = _shortest_path(
        store,
        from_seed.node_id,
        to_seed.node_id,
        min_confidence,
        max_hops=max(depth, 4),
    )

    call_path = []
    slices = []
    for step, (nid, edge_type, confidence) in enumerate(path):
        node = store.get_node(nid)
        if node is None:
            continue
        call_path.append(
            {
                "step": step,
                "node_id": nid,
                "qualified_name": node["qualified_name"],
                "file": node["file"],
                "span_start": node["span_start"],
                "edge_type": edge_type,
                "confidence": confidence,
            }
        )
        slices.append(_make_slice(repo_root, node, confidence, "tree_sitter_java"))

    response["call_path"] = call_path
    response["slices"] = _trim_to_budget(slices, max_tokens)
    response["stats"]["nodes_visited"] = nodes_visited
    response["stats"]["edges_traversed"] = edges_traversed
    response["stats"]["tokens_estimated"] = sum(
        _estimate_tokens(s["source"]) for s in response["slices"]
    )


def _shortest_path(
    store: GraphStore, from_id: str, to_id: str, min_confidence: float, max_hops: int
) -> tuple[list[tuple[str, str, float]], int, int]:
    """BFS over CALLS-family edges; returns [(node_id, edge_type_into_it, confidence), ...]."""
    if from_id == to_id:
        return [(from_id, "seed", 1.0)], 1, 0

    frontier = [from_id]
    came_from: dict[str, tuple[str, str, float]] = {}
    visited = {from_id}
    nodes_visited = 1
    edges_traversed = 0

    for _ in range(max_hops):
        next_frontier = []
        for nid in frontier:
            for neighbor, edge_type, confidence in store.neighbors(
                nid, "out", min_confidence
            ):
                edges_traversed += 1
                nb_id = neighbor["id"]
                if nb_id in visited:
                    continue
                visited.add(nb_id)
                nodes_visited += 1
                came_from[nb_id] = (nid, edge_type, confidence)
                if nb_id == to_id:
                    return (
                        _reconstruct(came_from, from_id, to_id),
                        nodes_visited,
                        edges_traversed,
                    )
                next_frontier.append(nb_id)
        frontier = next_frontier
        if not frontier:
            break

    return [], nodes_visited, edges_traversed


def _reconstruct(
    came_from: dict[str, tuple[str, str, float]], from_id: str, to_id: str
) -> list[tuple[str, str, float]]:
    path = [(to_id, came_from[to_id][1], came_from[to_id][2])]
    cur = came_from[to_id][0]
    while cur != from_id:
        prev, edge_type, confidence = came_from[cur]
        path.append((cur, edge_type, confidence))
        cur = prev
    path.append((from_id, "seed", 1.0))
    path.reverse()
    return path


__all__ = ["BLIND_SPOTS", "db_path_for", "explore", "personalized_pagerank"]
