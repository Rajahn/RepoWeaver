# Changelog

## v0.3.0 — M3 (Type-precision overlay)

### Added

- `src/repoweaver/typed/`: a dependency-free SCIP pipeline —
  `scip_proto.py` (hand-rolled protobuf wire decoder for `scip.proto`'s
  Index/Document/Occurrence/SymbolInformation messages), `scip_index.py`
  (flattens occurrences into caller/target reference tuples via
  enclosing-range scope nesting), `symbol_map.py` (SCIP symbol → RepoWeaver
  `qualified_name`, including JVM-descriptor overload alignment; unmappable
  symbols are recorded as skipped, never guessed), `overlay.py` (merge/CLI
  entry point).
- `fabric overlay scip --repo . --index path/to/index.scip [--dry-run]`:
  merges typed references into the graph as `CALLS_TYPED`/
  `REFERENCES_TYPED`/`EXTENDS_TYPED`/`IMPLEMENTS_TYPED` edges (confidence
  0.95, provenance `scip_java`), upgrading a matching existing textual edge
  in place (provenance `scip_java+tree_sitter_java`) rather than duplicating
  it. Never drops an edge. Idempotent and deterministic across repeated runs.
- `tests/fixtures/m3typed`: interface + two implementations, same-name
  overloads distinguished by parameter type, and a generic method — used by
  both the pytest suite and `fabric verify --level m3`.
- `scripts/build_scip_fixture.sh` / `scripts/gen_deterministic_scip_fixture.py`:
  build the fixture's `index.scip`, preferring a real `scip-java` binary
  (via `SCIP_JAVA_BIN`/`PATH`, never a hardcoded download URL) and falling
  back to a hand-encoded deterministic wire-format generator.
- `fabric verify --level m3`: interface-typed dispatch precision, overload
  disambiguation, lossless typed/textual merge, idempotency and
  graph-signature determinism — wired into CI and `make verify-m3`.
- ADR-0003 and additive schema v1.2 (`edge.type`/`provenance` new values
  only — no column/constraint changes; `explore()` contract stays v1.1).

### Remaining limitations

- The bundled `tests/fixtures/m3typed/index.scip` is produced by the
  deterministic generator, not a real `scip-java` run — this session could
  not complete a `scip-java` binary download or a from-scratch Maven
  `semanticdb-javac` build within a reasonable time (see
  `docs/adr/0003-typed-overlay.md` §9). The decoder/mapper/merge pipeline
  itself is validated against real scip.proto wire bytes either way.
- A Gson-scale before/after overlay benchmark was not completed for the same
  reason; see `benchmarks/baselines/v0.3.0-gson-core-typed-overlay.md`
  (status `SKIP`). `v0.2.0-gson-core` remains the current measured baseline.
- Overload alignment via JVM disambiguator only matches non-generic
  parameter types precisely; a method that is both generic and overloaded
  falls back to `ambiguous_overload_unaligned` rather than a guess.
- Java only.

## v0.2.0 — M2 (Resolution, references and freshness)

### Added

- Conservative owner/name/arity/argument-type overload resolution, including stable nested-call return hints.
- `REFERENCES` edges for signatures, local variables, generics, annotations, casts, class literals and object creation.
- Java annotation declaration nodes and static-import owner references.
- `unresolved_reference`: ambiguity is diagnostic evidence, never N polluted graph edges.
- Annotation-derived HTTP/scheduled/message-listener entry-point metadata.
- Parse-incremental/global-resolution cache and `fabric watch` with a 2-second default debounce.
- `fabric verify --level m2`: watcher latency, edit/delete/rename equivalence, ambiguity, entry points and edge-evidence checks.
- Explicit benchmark scopes and checked-in RepoWeaver/CodeGraph Gson core-source comparison.
- ADR-0002 and backward-compatible schema/explore contract v1.1.

### Measured validation

- Pinned Gson core scope: 90.12% strict resolved cross-file coverage, 6.36% ambiguous edge rate, fixture edge precision/recall 1.0/1.0.
- CodeGraph 1.5.0 on the same commit/scope: 92.59% coverage. RepoWeaver is aligned to the release band but remains 2.47 percentage points behind.
- M2 watcher sync <=5 seconds and incremental canonical graph hash equals a clean full rebuild after edit/delete/rename.

### Remaining limitations

- No compiler/type-system overlay (SCIP/jdtls, planned M3).
- Runtime DI/reflection/config routing and dynamic MQ dispatch remain outside static truth.
- Java only.

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
