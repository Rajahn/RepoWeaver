# Schema v1 — RepoWeaver Evidence Graph

> Status: **DRAFT for review** (2026-08-17)  
> Freeze after: human sign-off on T0.3  
> Rule: once frozen, all downstream tables/queries must migrate in place; no silent drops.

---

## Rationale

Every fact in the graph must be traceable to its source and measurable for staleness.
Three tables; nothing more until M3.

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
    indexed_at     INTEGER NOT NULL              -- unix epoch seconds
);

CREATE INDEX node_file     ON node(repo, file);
CREATE INDEX node_qname    ON node(qualified_name);
CREATE INDEX node_simple   ON node(simple_name);

-- FTS5 for BM25 retrieval
CREATE VIRTUAL TABLE node_fts USING fts5(
    id UNINDEXED,
    simple_name,
    qualified_name,
    signature,
    content='node',
    content_rowid='rowid'
);
```

**id convention**: `{kind}:{repo_slug}:{file_path}:{qualified_name}`  
Example: `method:repoweaver:src/main/java/com/example/Foo.java:com.example.Foo#bar(String)`

**Disambiguation rule (v1)**: when the same `simple_name` appears in ≥2 files, ALL candidates are retained; none is silently dropped. The `edge` table records which candidates were ambiguous at resolution time (see `edge.ambiguous_candidates`).

---

### `edge`

```sql
CREATE TABLE edge (
    id                   TEXT    PRIMARY KEY,   -- sha256("{from_id}|{to_id}|{type}")[:16]
    from_id              TEXT    NOT NULL REFERENCES node(id),
    to_id                TEXT    NOT NULL REFERENCES node(id),
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

#### Edge Types

| type | meaning | default confidence |
|------|---------|--------------------|
| `CALLS` | method A calls method B (textual resolution) | 0.70 |
| `CALLS_TYPED` | A calls B (type-resolved via SCIP/jdtls, M3) | 0.95 |
| `IMPORTS` | file A imports symbol B | 1.00 |
| `EXTENDS` | class A extends class B | 1.00 |
| `IMPLEMENTS` | class A implements interface B | 1.00 |
| `ROUTES_TO` | framework route → handler (Spring MVC, etc.) | 0.80 |
| `ENTRY_POINT` | node is a known entry (REST/MQ/scheduled, M2) | 1.00 |
| `RUNTIME_CALLS` | observed in OTel/Jaeger trace (M4) | 1.00 |

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

## Freshness model

Content hash → staleness:

```
file_hash = sha256(file_content)
node/edge are stale when: stored source_hash ≠ current file_hash
fabric check → STALE if any node/edge in repo has stale source_hash
```

Rebuild is incremental: only nodes/edges with changed `source_hash` are re-extracted.  
Auto-sync (M2): OS file-event watcher triggers incremental rebuild with 2s debounce.

---

## Blind spots (mandatory in every `explore()` response)

Static analysis over this schema CANNOT represent:
- Calls dispatched through injected Spring beans (beyond the declared type)
- Message-queue listeners as call targets
- Reflection-based invocation
- Configuration-driven routing (without M2 entry-point rules)
- Generated code call chains (MyBatis Example methods, etc.)

`explore()` MUST append a `blind_spots` field to every response, verbatim.

---

## Schema changelog

| version | date | change |
|---------|------|--------|
| v1 | 2026-08-17 | initial draft |
