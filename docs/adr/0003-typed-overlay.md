# ADR-0003: M3 type-precision overlay (SCIP)

- Status: Accepted
- Date: 2026-08-18
- Schema: v1.2 (additive — new edge `type`/`provenance` values, no column changes)
- Explore contract: v1.1 (unchanged; typed edges are transparent to callers)

## Context

M2's tree-sitter resolver already scores same-arity overloads and resolves
receiver-typed calls (e.g. `shape.area()` where `shape: Shape`) using local
declared-type tracking and argument-type hints. It has no access to a real
type checker, though: it cannot see generic instantiation, cross-module
inheritance beyond what's locally visible, or compiler-verified overload
resolution. M3 adds a second, independent evidence source — a compiler-derived
[SCIP](https://github.com/sourcegraph/scip) index (produced by `scip-java`) —
and layers it onto the existing graph as an overlay rather than a
replacement, so every claim RepoWeaver makes about a typed call, reference,
`extends`, or `implements` relationship can (optionally) be backed by
compile-time evidence.

## Decisions

1. **The overlay never replaces the base graph; it upgrades or extends it.**
   `fabric overlay scip` reads a `.scip` index, resolves each occurrence to a
   RepoWeaver node pair, and either:
   - **merges** into an existing tree-sitter edge for the same
     `(from_id, to_id)`, upgrading it to the `*_TYPED` type with
     `provenance = "scip_java+tree_sitter_java"` and
     `confidence = max(existing, 0.95)` — both sides' evidence rows are kept
     under the new edge id (evidence is copied, never dropped), or
   - **adds** a new edge directly when no tree-sitter edge exists for that
     pair, with `provenance = "scip_java"` and `confidence = 0.95`.

   No edge is ever deleted outright; `_merge_pair` only ever replaces a base
   edge with its typed upgrade in the same transaction that copies the old
   evidence forward. This is the arbitration rule referenced in the M3 spec.

2. **Four typed edge types, one per base type.** `CALLS_TYPED`,
   `REFERENCES_TYPED`, `EXTENDS_TYPED`, `IMPLEMENTS_TYPED` mirror `CALLS`,
   `REFERENCES`, `EXTENDS`, `IMPLEMENTS`. `edge.type` has no `CHECK`
   constraint, so this needs no schema migration (see `docs/schema.md`).

3. **Merge is idempotent and deterministic by construction, not by special
   casing.** Edge ids are `sha256(from_id, to_id, type)`-derived and evidence
   ids are `sha256(edge_id, file, line)`-derived (`graph/store.py`), so
   re-running the overlay against the same index re-inserts the same rows
   and `ON CONFLICT DO NOTHING`/`DO UPDATE` collapses them harmlessly.
   `graph_signature()` — already excluding volatile timestamp columns — is
   therefore stable across repeated overlay runs without any bespoke
   idempotency logic in `overlay.py` itself. Verified in
   `fabric verify --level m3` and `tests/test_typed_overlay.py`.

4. **Symbol alignment never guesses.** `symbol_map.py` parses a SCIP symbol
   string into `(owner, member)` and looks up candidates in the *already
   built* RepoWeaver graph (`find_by_qualified_name`/`find_by_simple_name`).
   When an owner+name pair has exactly one candidate, that candidate wins
   outright — no disambiguator decoding needed. When there are multiple
   candidates (an overload), the SCIP method descriptor's JVM disambiguator
   (e.g. `(I)` or `(Ljava/lang/String;)`) is decoded to erased simple type
   names and matched against each candidate's own erased parameter types
   (mirroring `_simple_type_name` in `parser/java.py`). A unique match maps;
   zero or ambiguous matches are recorded under `skip_reasons` and the
   reference is dropped from the merge — never assigned to a guessed
   candidate. `SkipReason` enumerates: `malformed_symbol`, `local_symbol`,
   `unsupported_descriptor` (SCIP `Parameter`/`TypeParameter` descriptors,
   which RepoWeaver has no node kind for), `owner_not_found`,
   `member_not_found`, `ambiguous_overload_unaligned`.

5. **Known alignment boundary: generic erasure mismatch.** RepoWeaver's own
   parameter-type erasure keeps an unresolved generic type parameter as its
   literal name (`Box<T>.identity(T)` records the parameter type as `"T"`).
   JVM/SCIP method descriptors instead encode the *erased* bound
   (`Ljava/lang/Object;` → `"Object"`) per normal type erasure rules. For a
   non-overloaded method this is harmless (single-candidate resolution never
   reaches the disambiguator-matching code path). For a *generic method that
   is also overloaded*, the two erasure schemes would disagree and the
   overload would be reported `ambiguous_overload_unaligned` rather than
   silently mismatched — an accepted limitation of the "never guess"
   principle, not a bug. The `m3typed` fixture's `Box<T>` methods are
   deliberately non-overloaded so this boundary doesn't need exercising to
   validate the merge/idempotency guarantees this ADR is otherwise about.

6. **Edge-kind classification is a heuristic over occurrence position, since
   SCIP occurrences carry no edge-kind of their own** (unlike tree-sitter,
   which sees the `extends`/`implements` grammar node directly).
   `classify_edge_type` treats a reference that (a) is attributed to a
   type/interface/enum's own definition scope and (b) shares its source line
   with that same type's definition occurrence as a supertype relation
   (`EXTENDS_TYPED` for a class target, `IMPLEMENTS_TYPED` for an interface
   target); everything else attributed to a method/constructor target is
   `CALLS_TYPED`, and any remaining reference is `REFERENCES_TYPED`. Known
   boundary: a multi-line `implements`/`extends` clause, or an
   interface-extending-interface declaration, will not share the header
   line and falls back to `REFERENCES_TYPED`/`IMPLEMENTS_TYPED` respectively
   rather than being misclassified as a call. This is an accepted
   approximation, not a hazard — it never produces a *wrong* typed call, at
   worst it under-classifies a supertype edge as a plain reference.

7. **Caller attribution rebuilds nesting from `enclosing_range`, not from a
   real AST.** `scip_index.py` treats every *definition* occurrence's
   `enclosing_range` as a scope, and attributes each *reference* occurrence
   to the innermost scope containing its start position. References with no
   containing definition scope (e.g., hypothetical static-initializer-level
   code) are dropped from the overlay rather than mis-attributed to the
   wrong caller — none of the `m3typed` fixture's references hit this case.

8. **The decoder is a hand-rolled protobuf subset, not a general library.**
   `scip_proto.py` implements only what `scip.proto`'s `Index`/`Metadata`/
   `Document`/`Occurrence`/`SymbolInformation` messages need: varint,
   length-delimited, and both packed/unpacked repeated-scalar wire encodings.
   Unknown field numbers are skipped rather than raising, so it tolerates
   newer scip.proto fields RepoWeaver doesn't consume. `SymbolRole` is only
   ever read for its `Definition` bit (`0x1`); every other bit RepoWeaver
   doesn't use.

9. **The bundled `index.scip` fixture is hand-encoded, not scip-java
   output.** This environment could not complete a `scip-java` binary
   install (GitHub Releases network access was available but far too slow
   to finish within a reasonable session — the release asset is ~86MB) nor a
   Maven-based `semanticdb-javac` + `javac -Xplugin` build (no cached
   dependencies, same network constraint). `scripts/gen_deterministic_scip_fixture.py`
   hand-encodes the exact scip.proto wire bytes for
   `tests/fixtures/m3typed`'s five source files, using line/column offsets
   verified against the actual fixture source. `scripts/build_scip_fixture.sh`
   prefers a real `scip-java` binary when `SCIP_JAVA_BIN` or `PATH` provides
   one (e.g. in a network-unconstrained environment) and falls back to the
   generator otherwise — the decoder/mapper/merge pipeline is exactly the
   same either way, since it only depends on valid scip.proto wire bytes,
   not on which tool produced them. Per the "no internal information" policy
   this repo runs under, neither script hardcodes a specific download URL or
   mirror.

## Consequences

- Schema v1.2: `edge.type` gains four new free-text values
  (`CALLS_TYPED`/`REFERENCES_TYPED`/`EXTENDS_TYPED`/`IMPLEMENTS_TYPED`) and
  `edge.provenance` gains `scip_java` and the composite
  `scip_java+tree_sitter_java`. No column or constraint changes — existing
  v1.1 databases need no migration to read or write typed edges.
- `explore()`'s response contract is unaffected: typed edges are still plain
  `edge` rows, so existing slice/neighbor traversal code sees them for free
  without any contract version bump.
- Because M2's tree-sitter resolver already resolves the `m3typed` fixture's
  interface dispatch and overloads correctly on its own, the fixture mostly
  exercises the **merge** path (upgrading existing edges) rather than the
  **addition** path (SCIP-only discoveries). A repo with call sites tree-sitter
  cannot resolve today (e.g. dispatch through a field typed by a supertype
  declared in a dependency jar with no local source) is the addition path's
  actual value; the fixture's `Box<T>` methods and `Circle`/`Square`
  constructors demonstrate that both paths are exercised together in one run.
- A real Gson-scale before/after overlay comparison was attempted but not
  completed in this session due to the same network constraint noted above
  (see `benchmarks/baselines/` for the explicit SKIP record); the merge
  guarantees are instead machine-verified against the `m3typed` fixture via
  `fabric verify --level m3`, which is CI-gated.
