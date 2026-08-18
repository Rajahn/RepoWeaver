# ADR-0004: Query facade layer (M4-0)

- Status: Accepted
- Date: 2026-08-18
- Schema: unchanged (v1.2 — no table/column changes)
- Explore contract: v1.2 (additive — see `docs/explore-contract.md`)

## Context

A real-repo verification pass against a third-party Java codebase (tracked
outside this repo, referenced here only as "gt-verify") found that the graph
itself was first-tier: parsing, edge resolution, and confidence scoring all
held up. The gap was entirely in `explore()`'s retrieval layer:

1. A caller who already knows the exact symbol (`Class#method`, copied from
   a stack trace or an IDE) had no way to say so — every query went through
   BM25 full-text search, which can rank a same-named decoy above the exact
   target, or worse, miss it if the tokenizer's prefix matching doesn't favor
   it.
2. When a query legitimately matched several symbols, the old `candidates`
   list was a bare disambiguation menu (`node_id`/`qualified_name`/`file`/
   `score`) — the caller had to make a *second* `explore()` call per
   candidate just to find out which one was relevant. For a 13-candidate
   ambiguity, that is up to 13 extra round trips.
3. Multi-word natural-language queries ranked every hit independently, so a
   test method whose name happens to repeat several query words (a `*Test`
   class exercising the real target's vocabulary) could outrank the actual
   service class the caller meant.

None of this required touching the parser, resolver, or graph schema — the
graph already had the right edges and nodes; `explore()` just wasn't using
them well. This ADR scopes the fix to the retrieval/response layer only.

## Decisions

1. **Qualified-syntax pre-check, ahead of BM25.** `_resolve_qualified_query`
   recognizes three shapes — `Class#method`, `Class.method`, `method(Sig)`
   (all three optionally carrying a `(Sig)` suffix) — and resolves them
   directly via `find_by_simple_name` plus owner/signature filtering, before
   any search-engine call runs. `owner` may be a short (simple) class name;
   full package-qualified owners also match. A signature is normalized the
   same way `parser.java._simple_type_name` normalizes declared parameter
   types (strip generics/arrays/qualifiers to a simple name), so
   `fromJson(String, Class<?>)` and `fromJson(String,Class)` resolve
   identically.

   Priority order when a query could be read multiple ways: **qualified
   syntax > exact type-name node > BM25 diffusion.** A qualified-shape query
   that resolves to zero nodes (e.g. a bare fully-qualified type name, which
   also parses as a `Class.method` dot-form) falls through to normal search
   rather than returning empty — the shape match was a false positive, not a
   verified "not found".

2. **Ambiguity is answered, not just reported.** Any time resolution lands on
   two or more distinct nodes — whether via the qualified-syntax path or the
   pre-existing bare-name `_check_ambiguity` path — every candidate in the
   response is enriched (`_build_candidates`) with `file`/`span_start`/
   `span_end`/`signature` (so the caller can read the declaration without a
   second call) and with `callers` (direct, depth-1, confidence-sorted, capped
   at 5) and `blast_summary` (a risk-level → count rollup, computed with the
   same reverse-BFS `_fill_impact` already runs, just without materializing
   slices). Response size is bounded by `len(candidates) * 5`, not by graph
   size — this is a panorama, not a second `task=impact` call.

3. **Multi-word queries rank by owner cluster, not raw per-hit score.**
   `_cluster_rerank` groups same-owner hits (a class and the methods/fields on
   it that also matched the query) into one cluster, scores the cluster as
   `max(member scores) + 0.1 * cluster_size`, and sorts clusters ahead of
   individual hits. Type-kind nodes (class/interface) get a `1.5x` weight
   inside their own cluster score (a matched class is a stronger "this is the
   thing" signal than a matched method), and any hit whose owner class ends
   in `Test` is downweighted `0.5x` (test classes disproportionately repeat
   production vocabulary in test-method names without being the read
   target). This only fires for queries with 2+ words — a bare symbol name
   still goes through the pre-existing `_prioritize_seeds` exact-type-first
   ordering, unchanged.

4. **Entry-point annotations become configurable, not hardcoded.**
   `Indexer` now resolves its annotation → `entry_point_kind` table via
   `load_entry_point_annotations(repo_root)`: the built-in public table
   (`ENTRY_POINT_ANNOTATIONS` — Spring `@RestController`, `@Scheduled`,
   `@KafkaListener`, etc.) is used as-is unless
   `.repoweaver/entrypoints.yaml` exists, in which case its `annotations` map
   is either merged over the built-ins (`mode: merge`, default) or used
   verbatim (`mode: replace`). A missing or malformed config file silently
   falls back to the built-in table — a bad YAML file must never break a
   build. No internal/company-specific annotation names are checked into
   this repo; a caller with a private annotation taxonomy configures it via
   their own `.repoweaver/entrypoints.yaml`, which is user-repo-local and
   gitignored by the caller's own repo, not this one.

## Consequences

- `explore-contract.md` bumps to v1.2, additive only: three new optional
  `Candidate` fields and a documented qualified-syntax query shape. No
  `Slice`/`BlastRadiusEntry`/`CallPathEntry` field changes, no schema
  changes — existing v1/v1.1 consumers that only read `slices`/`blast_radius`
  are unaffected.
- `fabric verify --level query` machine-verifies all four query-facade
  behaviors (T1-T4) against the bundled `javademo`/`overloads` fixtures plus
  a purpose-built owner-cluster-vs-Test-method fixture, wired into
  `make ci` alongside `m1`/`m2`/`m3`/`benchmark`.
- This ADR does not touch `parser/java.py`, `resolver.py`, or
  `graph/store.py`'s schema — the graph construction pipeline is unchanged;
  only `explore.py` (retrieval/response) and `indexer.py`'s entry-point-table
  resolution (configuration, not graph logic) were modified.
