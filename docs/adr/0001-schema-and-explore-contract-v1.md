# ADR-0001: Freeze schema v1 and `explore()` contract v1 for M1

- Status: **Accepted**
- Date: 2026-08-17

## Context

`docs/schema.md` and `docs/explore-contract.md` were drafted before any implementation existed.
Implementing M1 (Java parser + SQLite graph store + FTS5/PageRank search + `explore()`) surfaced
four risks that would have blocked a correct implementation if left as originally drafted. This
ADR records the decisions and freezes both documents at v1.

## Decisions

### 1. FTS5 external-content sync requires triggers

`node_fts` is declared `content='node', content_rowid='rowid'` — an *external content* FTS5
table. SQLite does **not** keep such tables in sync automatically; without `INSERT`/`UPDATE`/
`DELETE` triggers on `node`, the FTS index silently drifts from the source table the first time a
node is updated or removed, and BM25 queries return stale or dangling rows. Decision: ship the
three standard sync triggers (`node_ai`, `node_ad`, `node_au`) as part of the frozen schema, not as
an optional add-on.

### 2. `ENTRY_POINT` is not an edge

An edge type needs two distinct endpoints. "This node is a known entry point" is a property of a
single node, and modeling it as a self-loop edge (`from_id == to_id`) is a hack that would also
break the `edge_from`/`edge_to` index assumptions used by BFS traversal (self-loops must be
filtered everywhere or they cause infinite walks). Decision: drop `ENTRY_POINT` from the M1 edge
enum entirely; defer it to M2 as `node.is_entry_point`. M1's `explore(task="locate")` does not
attempt an entry-point boost — this is documented as a known gap rather than faked with a
misleading schema shape.

### 3. Replace-by-file must not cascade-delete edges owned by other files

Naively deleting every `node` row for a file before re-inserting fresh rows works for a *full*
rebuild (every file gets reprocessed in the same transaction, so anything cascade-deleted is
recreated by the time the transaction commits) but breaks a *partial* rebuild of a single file:
`ON DELETE CASCADE` on `edge.to_id` would also remove inbound edges from every other file that
calls into the rebuilt file, and if those other files are not being reprocessed in the same pass,
those edges are lost until the next full rebuild.

Decision: `GraphStore.replace_file()` splits nodes into three sets — unchanged (same `id`,
`UPDATE` in place, no cascade fires), removed (genuinely gone from the file, `DELETE` + cascade is
correct because the symbol no longer exists), and new (`INSERT`). `id` is deterministic
(`{kind}:{repo_slug}:{file}:{qualified_name}`), which is what makes "same symbol → same row" work.
Edges *emitted by* a file are always fully replaced (`DELETE ... WHERE from_id IN (...)` then
re-insert), since those are exactly what re-parsing that file recomputes.

M1's `fabric build` always does a full-repo rebuild in one transaction (see §5), so this mechanism
is exercised and tested, but incremental single-file rebuilds are not yet wired into the CLI —
that is explicitly left to M2, matching the roadmap's "auto-sync" milestone.

### 4. Ambiguity is recorded, never guessed

For `CALLS`/`EXTENDS`/`IMPLEMENTS` resolution, when a call site's simple name matches more than
one candidate node and the receiver type cannot narrow it to exactly one, the resolver still emits
an edge (so the symbol isn't invisible to BFS) but at a confidence low enough (0.35) to be excluded
by the contract's `min_confidence=0.5` default, and populates `edge.ambiguous_candidates` with
every candidate's node id. Callers that want the full candidate set can lower `min_confidence` or
query the hidden `debug_graph` tool. No candidate is ever silently dropped, and no single candidate
is ever presented as certain when it isn't.

### 5. `fabric build` is a full rebuild in M1

Per-file incremental diffing (skip re-parsing unchanged files) is deferred to M2. M1 always
re-parses every `*.java` file and re-resolves the whole graph in one SQLite transaction. This is
simple, correct by construction (no partial-state bugs), and fast enough for the fixture-scale and
single-service-scale repos M1 targets. `fabric check` still does real per-file content-hash
comparison against `file_meta`, so freshness reporting is accurate even though rebuilding is
all-or-nothing.

## Consequences

- Schema v1 and contract v1 are now implementation-verified, not just designed. Both documents are
  marked **FROZEN**; further changes go through a new ADR and an additive migration.
- `ENTRY_POINT`/`is_entry_point` and incremental single-file rebuild are explicit M2 scope, not
  silent gaps.
- The blast radius of a rename (a node whose `qualified_name` changes) is: edges pointing at the
  old id are cascade-deleted on next build of that file, and any file that still calls into the old
  name will show `0` callers for the *new* name until it too is rebuilt (which `fabric build`
  always does, since M1 has no incremental mode). No user-visible staleness window exists in M1
  because there is no incremental mode yet.
