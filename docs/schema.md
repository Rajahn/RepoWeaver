# Schema v1.1 — RepoWeaver Evidence Graph

> Status: **FROZEN v1.1** (2026-08-18) — see [ADR-0001](adr/0001-schema-and-explore-contract-v1.md) and [ADR-0002](adr/0002-m2-resolution-and-freshness.md)
> Rule: once frozen, all downstream tables/queries must migrate in place; no silent drops.

---

## Rationale

Every fact in the graph must be traceable to its source and measurable for staleness.
Core facts live in `node`, `edge`, and `evidence`. M2 adds
`unresolved_reference` (ambiguity is not an edge), `file_meta` (freshness),
and `file_refs_cache` (parse-incremental/global-resolution correctness).

---

## Tables

### `node`

```sql
CREATE TABLE node (
    id             TEXT    PRIMARY KEY,          -- "{kind}:{repo}:{file}:{qualified_name}"
    kind           TEXT    NOT NULL,             -- class | interface | method | field | enum | enum_constant
    language       TEXT    NOT NULL DEFAULT 'java',
    repo           TEXT    NOT NULL,             -- repo root path (local absolute)
    file           TEXT    NOT NULL,             -- repo-relative path, e.g. "src/.../Foo.java"
    span_start     INTEGER NOT NULL,             -- 1-based line, inclusive
    span_end       INTEGER NOT NULL,             -- 1-based line, inclusive
    qualified_name TEXT    NOT NULL,             -- "com.example.Foo#bar(String)"
    simple_name    TEXT    NOT NULL,             -- "bar"  (FTS anchor)
    signature      TEXT,                         -- return type + param types, normalised
    commit_hash    TEXT,                         -- git HEAD at index time; NULL if not a git repo
    indexed_at     INTEGER NOT NULL,             -- unix epoch seconds
    is_entry_point INTEGER NOT NULL DEFAULT 0,
    entry_point_kind TEXT NOT NULL DEFAULT ''
);

CREATE INDEX node_file     ON node(repo, file);
CREATE INDEX node_qname    ON node(qualified_name);
CREATE INDEX node_simple   ON node(simple_name);

-- FTS5 external-content table for BM25 retrieval.
-- External-content FTS5 tables do NOT auto-sync with their content table —
-- triggers are mandatory or the index silently drifts. See ADR-0001 §1.
CREATE VIRTUAL TABLE node_fts USING fts5(
    simple_name,
    qualified_name,
    signature,
    content='node',
    content_rowid='rowid'
);

CREATE TRIGGER node_ai AFTER INSERT ON node BEGIN
    INSERT INTO node_fts(rowid, simple_name, qualified_name, signature)
    VALUES (new.rowid, new.simple_name, new.qualified_name, new.signature);
END;

CREATE TRIGGER node_ad AFTER DELETE ON node BEGIN
    INSERT INTO node_fts(node_fts, rowid, simple_name, qualified_name, signature)
    VALUES ('delete', old.rowid, old.simple_name, old.qualified_name, old.signature);
END;

CREATE TRIGGER node_au AFTER UPDATE ON node BEGIN
    INSERT INTO node_fts(node_fts, rowid, simple_name, qualified_name, signature)
    VALUES ('delete', old.rowid, old.simple_name, old.qualified_name, old.signature);
    INSERT INTO node_fts(rowid, simple_name, qualified_name, signature)
    VALUES (new.rowid, new.simple_name, new.qualified_name, new.signature);
END;
```

**id convention**: `{kind}:{repo_slug}:{file_path}:{qualified_name}`
Example: `method:repoweaver:src/main/java/com/example/Foo.java:com.example.Foo#bar(String)`

**Disambiguation rule (v1.1)**: a uniquely resolved relation becomes an
`edge`; multiple equally valid candidates are stored in `unresolved_reference`
and never counted as resolved coverage. The legacy
`edge.ambiguous_candidates` column remains for backward compatibility and is
empty on the v1.1 resolver path.

**id stability note**: `id` is deterministic (derived from `qualified_name`, not a rowid), so re-indexing an unchanged symbol produces the same `id` and updates the row in place instead of delete+insert. This is what makes edges pointing *into* a symbol from files that were not re-indexed in the same build survive a partial rebuild. See ADR-0001 §3 for the full replace-by-file algorithm.

---

### `edge`

```sql
CREATE TABLE edge (
    id                   TEXT    PRIMARY KEY,   -- sha256("{from_id}|{to_id}|{type}")[:16]
    from_id              TEXT    NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    to_id                TEXT    NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    type                 TEXT    NOT NULL,      -- see Edge Types below
    provenance           TEXT    NOT NULL,      -- see Provenance below
    confidence           REAL    NOT NULL       -- 0.0–1.0; see Confidence below
                         CHECK(confidence BETWEEN 0.0 AND 1.0),
    observed_at          INTEGER NOT NULL,      -- unix epoch seconds
    source_hash          TEXT    NOT NULL,      -- hash of source file at index time
    ambiguous_candidates TEXT                   -- JSON array of candidate node ids, if resolution was ambiguous
);

CREATE INDEX edge_from ON edge(from_id);
CREATE INDEX edge_to   ON edge(to_id);
CREATE INDEX edge_type ON edge(type);
```

`evidence.edge_id` also carries `REFERENCES edge(id) ON DELETE CASCADE`, so deleting an edge (because one of its endpoint nodes was deleted) cleans up its evidence rows in the same statement — no orphaned evidence.

**Edge deletion / replace-by-file consistency (v1 fix).** `ON DELETE CASCADE` alone is not enough: naively deleting *all* node rows for a file before re-inserting would cascade-delete inbound edges from every *other*, not-yet-rebuilt file that happens to call into this file — a full rebuild recovers by the end of the transaction (every file is reprocessed), but a partial/incremental rebuild of a single file would silently lose those inbound edges. The store's replace-by-file algorithm (ADR-0001 §3) avoids this by using `INSERT ... ON CONFLICT(id) DO UPDATE` for symbols whose `id` is unchanged (no cascade fires) and only lets `DELETE` + cascade touch symbols that genuinely disappeared from the file.

#### Edge Types

| type | meaning | default confidence |
|------|---------|--------------------|
| `CALLS` | method A calls method B (textual resolution) | 0.70 |
| `CALLS_TYPED` | A calls B (type-resolved via SCIP/jdtls, M3) | 0.95 |
| `IMPORTS` | file A imports symbol B (including static-import owner types) | 1.00 |
| `REFERENCES` | a signature/body/annotation uses a uniquely resolved type | 0.95 |
| `EXTENDS` | class A extends class B | 1.00 |
| `IMPLEMENTS` | class A implements interface B | 1.00 |
| `ROUTES_TO` | framework route → handler (Spring MVC, etc.) | 0.80 |
| `RUNTIME_CALLS` | observed in OTel/Jaeger trace (M4) | 1.00 |

**Entry points (v1.1).** Entry-ness is a node property, never a self-loop.
`is_entry_point` and `entry_point_kind` are populated from recognized HTTP,
scheduled and message-listener annotations. This marks externally invoked
symbols without pretending static analysis knows their runtime callers.

> **Nakedness rule**: no edge may be stored without `provenance` and `confidence`. Assertion in the insert path.

#### Provenance values

| value | meaning |
|-------|---------|
| `tree_sitter_java` | extracted by tree-sitter Java grammar |
| `scip_java` | extracted from scip-java index (M3) |
| `jdtls` | extracted from jdtls LSP response (M3) |
| `otel_trace` | observed in OpenTelemetry trace (M4) |
| `rule_entry_point` | matched by annotation/pattern rule (M2) |

---

### `evidence`

```sql
CREATE TABLE evidence (
    id                  TEXT    PRIMARY KEY,
    edge_id             TEXT    NOT NULL REFERENCES edge(id),
    file                TEXT    NOT NULL,
    line                INTEGER NOT NULL,
    parser_version      TEXT    NOT NULL,   -- e.g. "tree-sitter-java 0.23.4"
    freshness_ts        INTEGER NOT NULL,   -- unix epoch seconds of file mtime at index time
    verification_status TEXT    NOT NULL    -- verified | stale | ambiguous
                        CHECK(verification_status IN ('verified','stale','ambiguous'))
);

CREATE INDEX evidence_edge ON evidence(edge_id);
```

---

### `file_meta`

Added in the v1 implementation (not in the original draft) because freshness cannot be computed
without a durable record of the hash last observed *per file*:

```sql
CREATE TABLE file_meta (
    file           TEXT    PRIMARY KEY,   -- repo-relative path
    content_hash   TEXT    NOT NULL,      -- sha256 of file bytes at last index
    indexed_at     INTEGER NOT NULL,      -- unix epoch seconds
    node_count     INTEGER NOT NULL DEFAULT 0
);
```

`fabric check` recomputes the hash of every `*.java` file under the repo and compares it against
`file_meta.content_hash`; any file that is new, changed, or removed since the last build/sync
marks the repo `STALE`.

### `unresolved_reference` (v1.1)

Stores `(from_id, type, target_name, candidates, reason, file, line,
site_count)` for references that cannot be uniquely resolved. Candidate sets
are diagnostic evidence, not graph edges.

### `file_refs_cache` (v1.1)

Stores `(file, content_hash, payload)` where payload is deterministic raw parser
output. Watch mode reparses changed files, reuses unchanged payloads, and still
runs global resolution over every current file.

---

## Freshness model

Content hash → staleness:

```
file_hash = sha256(file_content)
node/edge are stale when: stored source_hash ≠ current file_hash
fabric check → STALE if any node/edge in repo has stale source_hash
```

M2 reparses only changed files using `file_refs_cache`, then globally
re-resolves the current symbol table. `fabric watch` uses OS file events and a
2-second debounce. Delete/rename batches are accepted only when their canonical
graph hash equals a clean full rebuild.

---

## Blind spots (mandatory in every `explore()` response)

Static analysis over this schema CANNOT represent:
- Calls dispatched through injected Spring beans (beyond the declared type)
- Message-queue listeners as call targets
- Reflection-based invocation
- Configuration-driven routing
- Generated code call chains (MyBatis Example methods, etc.)

`explore()` MUST append a `blind_spots` field to every response, verbatim.

---

## Schema changelog

| version | date | change |
|---------|------|--------|
| v1 | 2026-08-17 | initial draft |
| v1 (frozen) | 2026-08-17 | FTS5 triggers, cascade-safe replace-by-file, file freshness. See ADR-0001. |
| v1.1 (frozen) | 2026-08-18 | Additive entry-point columns, `unresolved_reference`, `file_refs_cache`, `REFERENCES`, annotation symbols and parse-incremental/global-resolution semantics. See ADR-0002. |
