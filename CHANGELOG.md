# Changelog

## v0.3.2 — Cross-audit clean-up

Two independent v0.3.1 audits were cross-checked against each other; the
three findings both flagged as clear cuts (PROVE-CUT) are applied here,
along with closing a contract debt the second audit surfaced. No schema or
`explore()` response-shape changes.

### Fixed

- **Duplicated locked-DB error text**: `cli.py`'s `_exit_on_locked_db` and
  `typed/cli.py`'s inline `overlay scip` lock-error branch printed the same
  message from two copies. Consolidated into
  `repoweaver.cli_errors.exit_on_locked_db`, shared by both call sites.
- **Unused `BenchmarkAdapter` protocol**: `benchmark/adapters.py` defined a
  `Protocol` that no code checked against — every adapter is looked up by
  name via the `ADAPTERS`/`build_adapter` registry, never via structural
  typing. Removed; `build_adapter`'s return type is now the concrete union
  of adapter classes it can actually return.
- **`_percentile`**: the hand-rolled linear-interpolation percentile now
  delegates to `statistics.quantiles(data, n=100, method="inclusive")`,
  which computes the identical interpolation — verified against the old
  implementation across randomized inputs — instead of maintaining a
  bespoke reimplementation. The empty-list and single-element guard
  clauses are unchanged; `summarize_query_samples()` output is unchanged
  field-for-field.

### Added

- **`debug_graph` hidden diagnostic tool**: `docs/explore-contract.md` has
  documented `debug_graph` under `FABRIC_MCP_TOOLS` since v1, but
  `server/mcp.py` never implemented it — a contract/code gap the second
  audit surfaced. Implemented: given a symbol, returns a raw dump of its
  matching node row(s) plus outgoing/incoming edges (`type`/`confidence`/
  `provenance`/`ambiguous_candidates`) and any `unresolved_reference` rows
  originating from it. Diagnostic only — no ranking, trimming, or
  `blind_spots`; not part of the `explore()` contract. Hidden by default,
  opt-in alongside `status`/`reindex` via `FABRIC_MCP_TOOLS=debug_graph`.

## v0.3.1 — Fix-up release

A round of internal audit review of v0.3.0 surfaced several correctness and
robustness gaps; this release fixes all of them. No schema, contract, or
benchmark-definition changes.

### Fixed

- **`explore()` ambiguity detection** (`locate`/`understand`/`impact`/`debug`):
  a class query no longer collides with its own same-named constructor and
  gets misreported as ambiguous — disambiguation now groups by
  `(simple_name, kind-family)` and gives a unique type node (class/interface/
  enum/annotation) priority over same-named constructors/methods. Genuine
  same-name collisions across distinct classes, and existing method-vs-method
  ambiguity (e.g. two unrelated `close()` overrides), are still reported via
  `candidates` exactly as before.
- **Token-budget trimming**: neighbor slices are now sorted by
  `(confidence desc, span size asc)` before budget allocation, and a slice
  that would be cut down to less than 20% of its own line count (and has more
  than 10 lines) is skipped entirely instead of emitted as a near-useless
  fragment. A response's `stats.skipped_slices` counter reports how many
  slices were dropped this way.
- **Incremental-build cache size**: `file_refs_cache` no longer duplicates a
  file's full source text inside its cached JSON payload — source is read
  back off disk when needed. An older cache row that still embeds `source` is
  safely ignored (never trusted) and the file is re-read from disk instead.
- **Concurrent `build`/`watch` access**: `GraphStore` now sets an explicit
  10-second `PRAGMA busy_timeout` (overridable via `FABRIC_BUSY_TIMEOUT_MS`),
  and `fabric build`/`watch`/`overlay scip` print a human-readable message —
  instead of a raw traceback — when another process is holding a write lock.
- **Watcher robustness**: if a file is deleted in the brief window between
  being reported as changed and being read for (re)parsing, incremental build
  now treats it as deleted instead of letting the `OSError` crash the watch
  process.
- **`fabric overlay scip`**: a corrupted or unreadable `.scip` index now
  prints a friendly error and exits with status 1, instead of a raw
  traceback.
- **`scripts/check_public.py`**: switched the leak-detection file scanner
  from an allowlist of text suffixes to a blocklist of known-binary ones, so
  `.java`, `.sh`, and extensionless files are scanned by default instead of
  silently skipped.
- Removed `_split_member_qname`, an unused function with no remaining
  callers.
- Internal: `verify.py`'s four `_run_*_verification` functions were split
  into small per-section helpers sharing a common report/pass-fail skeleton;
  output and pass/fail behavior are unchanged.

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
