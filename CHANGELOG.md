# Changelog

## v0.1.0 — M1 (Fabric MVP)

Initial working closed loop: index a Java repo, retrieve context, serve it through a single MCP tool. Zero LLM, zero external network calls at runtime.

### Added

- **Parser**: tree-sitter-based Java extractor (`repoweaver.parser.java`) — package/import/class/interface/enum/enum-constant/method/constructor/field, with 1-based line spans, qualified names, and signatures.
- **Graph store**: SQLite-backed `GraphStore` (`repoweaver.graph.store`) — node/edge/evidence/file_meta schema, FTS5 BM25 full-text search with mandatory external-content sync triggers, replace-by-file semantics that update node ids in place and only cascade-delete truly removed symbols.
- **Indexer**: repo-wide symbol resolution (`repoweaver.indexer`) producing `CALLS`/`EXTENDS`/`IMPLEMENTS`/`IMPORTS` edges with explicit provenance and confidence. Ambiguous resolutions are never guessed — they're recorded with low confidence and a candidate list.
- **Search**: hybrid BM25 + simplified Personalized PageRank retrieval (`repoweaver.search.engine`), a bounded random-walk-with-restart with no linear-algebra dependency.
- **explore() contract v1**: the single public MCP tool (`repoweaver.explore`), covering all four task modes — `understand`, `impact`, `locate`, `debug` — with a fixed `blind_spots` disclosure and token-budget trimming.
- **CLI** (`fabric`): `build`, `check`, `init`, `serve`, `verify`, `version`.
- **MCP server**: `fabric serve` exposes `explore()` as the sole public tool; `status`/`reindex` diagnostics are hidden behind `FABRIC_MCP_TOOLS`.
- **AGENTS.md injection**: idempotent, marker-delimited block injection (`repoweaver.protocol`) that never touches user content outside the RepoWeaver block.
- **`fabric verify --level m1`**: end-to-end closed-loop check against a bundled Java fixture — parsing, edge resolution, retrieval, freshness, contract shape, and token-budget trimming — with explicit PASS/FAIL.
- **ADR-0001**: records the schema/contract decisions behind FTS5 sync triggers, the ENTRY_POINT deferral to M2, the replace-by-file algorithm, the never-guess-on-ambiguity rule, and the full-rebuild-in-M1 scope.
- `docs/schema.md` and `docs/explore-contract.md` promoted from DRAFT to **FROZEN v1**.
- **Public-repo smoke test**: indexed `google/gson` (264 Java files) without parser errors; produced 4,866 nodes / 59,321 candidate edges in 6.08s and passed freshness check. This is a robustness smoke test, not a precision/coverage claim.
- **Public-source leak gate**: CI rejects credential patterns and protected private references.

### Known limitations (M1 scope)

- Java only. No incremental single-file rebuild (`fabric build` is always a full-repo rebuild); `fabric check` freshness is still real, per-file content-hash comparison.
- No auto-watch (M2), no SCIP/jdtls type-precision overlay (M3), no OTel runtime overlay (M4).
- Reflection, DI container wiring, MQ listener dispatch, and generated code are not represented in the graph — always disclosed via `blind_spots`.
- Wildcard and static imports are not resolved to specific edges.
- No published benchmark numbers against public repos yet — the benchmark harness (`benchmarks/`) is scaffolded but unpopulated.
