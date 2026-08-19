"""explore() — the single public MCP tool. See docs/explore-contract.md (frozen v1)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from codecontextfabric.graph.store import GraphStore
from codecontextfabric.indexer import Indexer
from codecontextfabric.search.engine import (
    SearchEngine,
    SearchQuery,
    SearchResult,
    personalized_pagerank,
)

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


_MIN_KEEP_FRACTION = 0.2
_FRAGMENT_LINE_THRESHOLD = 10


def _trim_to_budget(slices: list[dict], max_tokens: int) -> tuple[list[dict], int]:
    """Keep whole source lines while respecting the soft token budget.

    A single class can span thousands of lines. Returning it whole would make
    ``max_tokens`` meaningless, so an oversized slice is shortened at a line
    boundary and marked as truncated. The source remains verbatim and the
    reported ``span_end`` is adjusted to match the returned lines.

    A slice that would be cut down to less than 20% of its original line
    count (and has more than 10 lines to begin with) is dropped instead of
    emitted as a near-useless fragment; it doesn't consume any budget, so the
    next slice in priority order gets a chance to fit. Returns
    ``(slices, skipped_count)``.
    """
    out: list[dict] = []
    used = 0
    skipped = 0
    budget = max(1, max_tokens)

    for original in slices:
        remaining = budget - used
        if remaining <= 0:
            break

        source_lines = original["source"].splitlines()
        total_lines = len(source_lines)
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

        if (
            total_lines > _FRAGMENT_LINE_THRESHOLD
            and len(kept) < total_lines * _MIN_KEEP_FRACTION
        ):
            skipped += 1
            continue

        item = dict(original)
        item["source"] = "\n".join(kept)
        if len(kept) < total_lines or kept[-1] != source_lines[len(kept) - 1]:
            item["truncated"] = True
            item["span_end"] = item["span_start"] + len(kept) - 1
        out.append(item)
        used += _estimate_tokens(item["source"])

    return out, skipped


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
            "skipped_slices": 0,
            "freshness": "ok",
        },
        "blind_spots": BLIND_SPOTS,
    }


def _freshness(store: GraphStore, repo_root: Path) -> str:
    indexer = Indexer(repo_root, store)
    fresh, _stale = store.is_fresh(indexer.current_file_hashes())
    return "ok" if fresh else "stale"


_IDENT = r"[A-Za-z_$][\w$]*"

# T1 qualified-syntax pre-check: `Class#method`, `Class.method`, `method(Sig)`.
# All three accept an optional `(Sig)` suffix; a query matching one of these
# shapes is resolved directly against the graph (find_by_simple_name +
# owner/signature filtering) and, when it lands on exactly one node, used as
# the seed without ever going through BM25. See docs/adr/0004.
_HASH_FORM = re.compile(
    rf"^(?P<owner>{_IDENT}(?:\.{_IDENT})*)#(?P<name><init>|{_IDENT})"
    rf"(?:\((?P<sig>[^()]*)\))?$"
)
_DOT_FORM = re.compile(
    rf"^(?P<owner>{_IDENT}(?:\.{_IDENT})*)\.(?P<name>{_IDENT})"
    rf"(?:\((?P<sig>[^()]*)\))?$"
)
_BARE_SIG_FORM = re.compile(rf"^(?P<name>{_IDENT})\((?P<sig>[^()]*)\)$")


def _strip_type_decoration(raw: str) -> str:
    """Mirror parser.java._simple_type_name: drop generics/arrays/qualifiers
    so a user-typed signature ("Class<?>", "java.lang.String") lines up with
    the simple-name signature form stored in `qualified_name`."""
    raw = re.sub(r"<.*>", "", raw.strip())
    raw = raw.rstrip("[] ")
    if "." in raw:
        raw = raw.rsplit(".", 1)[-1]
    return raw.strip()


def _normalize_sig(sig: str) -> str:
    return ",".join(_strip_type_decoration(p) for p in sig.split(",") if p.strip())


def _member_sig(qualified_name: str) -> str | None:
    if "#" not in qualified_name:
        return None
    member = qualified_name.split("#", 1)[1]
    if "(" not in member:
        return None
    return member[member.index("(") + 1 : member.rindex(")")]


def _owner_of(qualified_name: str) -> str:
    return qualified_name.split("#", 1)[0]


def _lookup_qualified_member(
    store: GraphStore, owner: str | None, name: str, sig: str | None
) -> list[dict]:
    candidates = store.find_by_simple_name(name)
    if owner is not None:
        short_owner = owner.rsplit(".", 1)[-1]
        candidates = [
            n
            for n in candidates
            if _owner_of(n["qualified_name"]) == owner
            or _owner_of(n["qualified_name"]).rsplit(".", 1)[-1] == short_owner
        ]
    if sig is not None:
        norm_sig = _normalize_sig(sig)
        candidates = [
            n
            for n in candidates
            if _member_sig(n["qualified_name"]) is not None
            and _normalize_sig(_member_sig(n["qualified_name"])) == norm_sig
        ]
    return candidates


def _resolve_qualified_query(query: str, store: GraphStore) -> tuple[list[dict], bool]:
    """Returns (matching_nodes, is_qualified_form). `is_qualified_form` is True
    whenever the query shape matched one of the three qualified forms, even if
    it resolved to zero nodes (in which case the caller falls back to the
    normal BM25 path — the shape guess was wrong, e.g. a fully-qualified type
    name like "com.example.Foo" also parses as a dot-form owner.name)."""
    q = query.strip()
    if not q:
        return [], False
    for pattern in (_HASH_FORM, _DOT_FORM, _BARE_SIG_FORM):
        m = pattern.match(q)
        if not m:
            continue
        groups = m.groupdict()
        owner = groups.get("owner")
        return (
            _lookup_qualified_member(store, owner, groups["name"], groups.get("sig")),
            True,
        )
    return [], False


def _seed_from_node(node: dict) -> SearchResult:
    return SearchResult(
        node_id=node["id"],
        qualified_name=node["qualified_name"],
        simple_name=node["simple_name"],
        score=1.0,
        bm25_score=1.0,
        kind=node["kind"],
        file=node["file"],
        span_start=node["span_start"],
        span_end=node["span_end"],
        signature=node.get("signature") or "",
    )


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
        return {"error": "not_indexed", "hint": "run: ccf build"}

    depth = max(0, min(depth, 4))

    with GraphStore(db_path) as store:
        freshness = _freshness(store, repo_root)
        engine = SearchEngine(store)

        response = _base_response(query, task, repo)
        response["stats"]["freshness"] = freshness

        qualified_nodes, is_qualified = _resolve_qualified_query(query, store)

        if is_qualified and qualified_nodes:
            if len(qualified_nodes) == 1:
                seeds = [_seed_from_node(qualified_nodes[0])]
            elif task == "debug":
                seeds = [_seed_from_node(n) for n in qualified_nodes]
            else:
                response["candidates"] = _build_candidates(
                    store,
                    [
                        {
                            "node_id": n["id"],
                            "qualified_name": n["qualified_name"],
                            "file": n["file"],
                            "score": 1.0,
                        }
                        for n in qualified_nodes
                    ],
                    depth,
                    min_confidence,
                )
                return response
        else:
            seeds = engine.search(
                SearchQuery(
                    query=query,
                    max_results=30,
                    min_confidence=min_confidence,
                    depth=depth,
                    task=task,
                )
            )

            if not seeds:
                return response

            seeds = _prioritize_seeds(query, seeds)

            ambiguity = _check_ambiguity(query, seeds)
            if ambiguity is not None and task != "debug":
                response["candidates"] = _build_candidates(
                    store, ambiguity, depth, min_confidence
                )
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
                response,
                store,
                repo_root,
                query,
                seeds,
                depth,
                min_confidence,
                max_tokens,
            )

        return response


_TYPE_KINDS = {"class", "interface", "enum", "annotation"}


def _prioritize_seeds(query: str, seeds: list) -> list:
    """A bare-name query that exactly names a type declaration is almost
    always a "show me this class" request. Put exact-name type nodes first
    (stable within each tier by search score) so the class body leads the
    response instead of its same-named constructors/methods — which is a
    ranking concern, distinct from the genuine-ambiguity check below."""
    bare = query.strip()
    if not bare or any(c in bare for c in " ()#."):
        return seeds

    def tier(hit):
        exact_type = hit.simple_name == bare and hit.kind in _TYPE_KINDS
        return (0 if exact_type else 1, -hit.score)

    return sorted(seeds, key=tier)


def _check_ambiguity(query: str, seeds: list) -> list[dict] | None:
    """Disambiguate same-name hits, grouped by kind family.

    A type declaration (class/interface/enum/annotation) and its own
    constructor share `simple_name` by construction — that is not ambiguity,
    it's Java. So when any type-kind node matches the bare query, a unique
    type qualified_name wins outright regardless of how many same-named
    constructors/methods also matched. Candidates are only surfaced when two
    or more *distinct* symbols within the same kind family (two classes, or
    two methods, sharing a name) are a genuine toss-up.
    """
    bare = query.strip()
    if not bare or any(c in bare for c in " ()#."):
        return None  # only bare symbol-name queries are candidates for disambiguation
    matching = [s for s in seeds if s.simple_name == bare]
    if len(matching) < 2:
        return None

    type_seeds = [s for s in matching if s.kind in _TYPE_KINDS]
    type_qnames = {s.qualified_name for s in type_seeds}
    if len(type_qnames) == 1:
        return None  # a unique type node beats any same-named ctor/method noise
    group = type_seeds if len(type_qnames) >= 2 else matching

    distinct_qnames = {s.qualified_name for s in group}
    if len(distinct_qnames) < 2:
        return None

    top = sorted(group, key=lambda s: s.score, reverse=True)
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


_MAX_CANDIDATE_CALLERS = 5


def _candidate_callers(store: GraphStore, node_id: str) -> list[dict]:
    """Direct callers of a candidate. Java callers statically target the
    *declared* type — usually the interface method — so an implementation
    method's own in-edges are typically empty while its interface method
    carries the callers. Walk IMPLEMENTS/EXTENDS out from the candidate's
    owner to sibling declarations of the same member name and merge their
    callers in, marked `via` so the agent knows the path."""
    seen: dict[str, dict] = {}

    def _absorb(edges, via: str | None) -> None:
        for caller, edge_type, confidence in edges:
            entry = seen.get(caller["qualified_name"])
            if entry is None:
                entry = {
                    "qualified_name": caller["qualified_name"],
                    "file": caller["file"],
                    "edge_type": edge_type,
                    "confidence": confidence,
                }
                if via:
                    entry["via"] = via
                seen[caller["qualified_name"]] = entry

    _absorb(store.neighbors(node_id, "in", 0.0), None)

    node = store.get_node(node_id)
    if node is not None and "#" in node["qualified_name"]:
        owner_qname, member = node["qualified_name"].split("#", 1)
        member_name = member.split("(", 1)[0]
        for sup, edge_type, _conf in (
            store.neighbors(
                store.find_by_qualified_name(owner_qname)[0]["id"], "out", 0.0
            )
            if store.find_by_qualified_name(owner_qname)
            else []
        ):
            if edge_type not in ("IMPLEMENTS", "EXTENDS"):
                continue
            via = sup["qualified_name"]
            for sibling in store.find_by_simple_name(member_name):
                if sibling["id"] == node_id:
                    continue
                sib_owner = sibling["qualified_name"].split("#", 1)[0]
                if sib_owner == via:
                    _absorb(store.neighbors(sibling["id"], "in", 0.0), via)

    ordered = sorted(seen.values(), key=lambda c: -c["confidence"])
    return ordered[:_MAX_CANDIDATE_CALLERS]


def _candidate_blast_summary(
    store: GraphStore, node_id: str, depth: int, min_confidence: float
) -> dict[str, int]:
    """Count-by-risk-level summary of the reverse call-graph — the same BFS
    `_fill_impact` runs, without materializing slices, so a full-panic
    disambiguation response stays cheap."""
    summary: dict[str, int] = {}
    visited = {node_id}
    frontier = [node_id]
    for hop in range(1, max(depth, 1) + 1):
        next_frontier = []
        for nid in frontier:
            for caller, _edge_type, confidence in store.neighbors(
                nid, "in", min_confidence
            ):
                if caller["id"] in visited:
                    continue
                visited.add(caller["id"])
                next_frontier.append(caller["id"])
                risk = _risk_level(hop, confidence)
                summary[risk] = summary.get(risk, 0) + 1
        frontier = next_frontier
        if not frontier:
            break
    return summary


def _build_candidates(
    store: GraphStore, candidates: list[dict], depth: int, min_confidence: float
) -> list[dict]:
    """T2/T4: turns a bare (node_id, qualified_name, file, score) candidate
    list into a full disambiguation panorama — each candidate carries enough
    context (file/span/signature, its direct callers, and a blast-radius
    count-by-risk summary) that the ambiguity response answers the query on
    its own, without a follow-up round trip. Response size is bounded by
    len(candidates) * _MAX_CANDIDATE_CALLERS, not by graph size."""
    enriched = []
    for c in candidates:
        node = store.get_node(c["node_id"])
        item = dict(c)
        if node is not None:
            item["file"] = node["file"]
            item["span_start"] = node["span_start"]
            item["span_end"] = node["span_end"]
            item["signature"] = node.get("signature") or ""
        item["callers"] = _candidate_callers(store, c["node_id"])
        item["blast_summary"] = _candidate_blast_summary(
            store, c["node_id"], depth, min_confidence
        )
        enriched.append(item)
    return enriched


_TEST_SUFFIX = "Test"
_TYPE_KIND_WEIGHT = 1.5
_TEST_OWNER_WEIGHT = 0.5


def _cluster_weighted_score(seed) -> float:
    score = seed.score
    if seed.kind in _TYPE_KINDS:
        score *= _TYPE_KIND_WEIGHT
    owner_simple = _owner_of(seed.qualified_name).rsplit(".", 1)[-1]
    if owner_simple.endswith(_TEST_SUFFIX):
        score *= _TEST_OWNER_WEIGHT
    return score


def _split_identifier_tokens(name: str) -> set[str]:
    """Split a qualified name into lowercase word tokens: camelCase,
    snake_case, package dots, and `#` separators all split. Splitting must
    happen BEFORE lowercasing — lowercasing first fuses camelCase words
    (DutyJudgement -> dutyjudgement) and destroys the word boundaries this
    exists to find. FTS5 has the same fused-token limitation, which is why
    coverage scoring lives here in Python."""
    import re

    out: set[str] = set()
    for chunk in re.split(r"[^A-Za-z0-9]+", name):
        pos = 0
        while pos < len(chunk):
            m = re.match(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+", chunk[pos:])
            if not m:
                break
            out.add(m.group(0).lower())
            pos += m.end()
    return out


def _word_coverage(owner: str, query_words: list[str]) -> int:
    """How many distinct query words the owner's whole qualified name covers."""
    tokens = _split_identifier_tokens(owner)
    return sum(1 for w in query_words if w in tokens)


def _cluster_rerank(query: str, seeds: list) -> list:
    """T3: for multi-word queries, rank by *distinct query-word coverage*
    per owner cluster first, then by coverage-weighted score. A test class
    whose 12 same-prefix methods all match the `decide*` prefix covers only
    ONE query word repeatedly; a service class named after the actual
    subject (duty/judgement) covers different words and must win. Cluster
    size is only a weak tie-break, never a score multiplier."""
    query_words = [w.lower() for w in query.strip().split() if w]
    if len(query_words) < 2:
        return seeds

    owners: dict[str, list] = {}
    for s in seeds:
        owner = _owner_of(s.qualified_name)
        owners.setdefault(owner, []).append(s)

    def owner_key(owner: str):
        members = owners[owner]
        coverage = _word_coverage(owner, query_words)
        best = max(_cluster_weighted_score(s) for s in members)
        return (-coverage, -best, -len(members))

    ranked_owners = sorted(owners, key=owner_key)
    order = {owner: i for i, owner in enumerate(ranked_owners)}
    return sorted(
        seeds,
        key=lambda s: (order[_owner_of(s.qualified_name)], -_cluster_weighted_score(s)),
    )


def _fill_understand_or_locate(
    response, store, repo_root, query, seeds, depth, min_confidence, max_tokens
) -> None:
    """Seed slices keep their (already prioritized) order — a bare-name
    query's target class must lead the response. Neighbors are budget-ranked
    separately (confidence desc, then smallest-first) so a large seed never
    starves small high-confidence context slices.

    Multi-word queries get one more reorder pass first: T3 clusters same-owner
    hits (a class and its own matching methods/fields are one signal, not
    several) so the owning type leads instead of a same-owner method that
    happened to score slightly higher — see `_cluster_rerank`."""
    seeds = _cluster_rerank(query, seeds)
    seed_slices: list[dict] = []
    neighbor_slices: list[dict] = []
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
        seed_slices.append(
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
            neighbor_slices.append(
                _make_slice(repo_root, neighbor, confidence, "tree_sitter_java")
            )

    neighbor_slices.sort(
        key=lambda s: (-s["confidence"], s["span_end"] - s["span_start"])
    )
    slices = seed_slices + neighbor_slices

    response["slices"], skipped = _trim_to_budget(slices, max_tokens)
    response["stats"]["nodes_visited"] = nodes_visited
    response["stats"]["edges_traversed"] = edges_traversed
    response["stats"]["skipped_slices"] = skipped
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

    response["slices"], skipped = _trim_to_budget(slices, max_tokens)
    response["blast_radius"] = blast_radius
    response["stats"]["nodes_visited"] = nodes_visited
    response["stats"]["edges_traversed"] = edges_traversed
    response["stats"]["skipped_slices"] = skipped
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
            response, store, repo_root, query, seeds, depth, min_confidence, max_tokens
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
    response["slices"], skipped = _trim_to_budget(slices, max_tokens)
    response["stats"]["nodes_visited"] = nodes_visited
    response["stats"]["edges_traversed"] = edges_traversed
    response["stats"]["skipped_slices"] = skipped
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
