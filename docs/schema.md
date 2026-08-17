# RepoWeaver Graph Schema

This document describes the SQLite schema used by RepoWeaver to store the
call-graph index. The authoritative DDL is in
[`src/repoweaver/graph/schema.sql`](../src/repoweaver/graph/schema.sql).

---

## Tables

### `node` — Indexed Symbols

```sql
CREATE TABLE IF NOT EXISTS node (
    id              TEXT    PRIMARY KEY,
    kind            TEXT    NOT NULL,
    language        TEXT    NOT NULL DEFAULT 'java',
    repo            TEXT    NOT NULL DEFAULT '',
    file            TEXT    NOT NULL DEFAULT '',
    span_start      INTEGER NOT NULL DEFAULT 0,
    span_end        INTEGER NOT NULL DEFAULT 0,
    qualified_name  TEXT    NOT NULL DEFAULT '',
    simple_name     TEXT    NOT NULL DEFAULT '',
    signature       TEXT    NOT NULL DEFAULT '',
    commit_hash     TEXT    NOT NULL DEFAULT '',
    indexed_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
```

| Column | Type | Description |
|---|---|---|
| `id` | TEXT PK | Stable hash of `(repo, file, qualified_name)` |
| `kind` | TEXT | `method` \| `class` \| `interface` \| `field` \| `constructor` |
| `language` | TEXT | Source language (default: `java`) |
| `repo` | TEXT | Repository root path or remote URL |
| `file` | TEXT | Repo-relative source path |
| `span_start` | INTEGER | Byte offset of declaration start |
| `span_end` | INTEGER | Byte offset of declaration end |
| `qualified_name` | TEXT | Fully-qualified symbol name |
| `simple_name` | TEXT | Unqualified symbol name |
| `signature` | TEXT | Method signature or class header |
| `commit_hash` | TEXT | Git commit at index time |
| `indexed_at` | TEXT | ISO-8601 UTC timestamp |

**Indexes:**
- `idx_node_file` on `(file)`
- `idx_node_qname` on `(qualified_name)`
- `idx_node_simple` on `(simple_name)`

**FTS virtual table:**
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5 (
    simple_name,
    qualified_name,
    signature,
    content='node',
    content_rowid='rowid'
);
```

---

### `edge` — Directed Relationships

```sql
CREATE TABLE IF NOT EXISTS edge (
    id                   TEXT    PRIMARY KEY,
    from_id              TEXT    NOT NULL REFERENCES node(id),
    to_id                TEXT    NOT NULL REFERENCES node(id),
    type                 TEXT    NOT NULL,
    provenance           TEXT    NOT NULL DEFAULT 'static',
    confidence           REAL    NOT NULL DEFAULT 1.0
                             CHECK (confidence BETWEEN 0 AND 1),
    observed_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    source_hash          TEXT    NOT NULL DEFAULT '',
    ambiguous_candidates TEXT    NOT NULL DEFAULT '[]'
);
```

| Column | Type | Description |
|---|---|---|
| `id` | TEXT PK | Stable hash of `(from_id, to_id, type)` |
| `from_id` | TEXT FK | Source node id |
| `to_id` | TEXT FK | Target node id |
| `type` | TEXT | Edge type (see table below) |
| `provenance` | TEXT | `static` \| `inferred` \| `manual` |
| `confidence` | REAL | 0.0–1.0; enforced by `CHECK` constraint |
| `observed_at` | TEXT | ISO-8601 UTC timestamp |
| `source_hash` | TEXT | Hash of source file at index time |
| `ambiguous_candidates` | TEXT | JSON array of alternative `to_id` values |

**Indexes:**
- `idx_edge_from` on `(from_id)`
- `idx_edge_to` on `(to_id)`

#### Edge Types

| Type | Default Confidence | Description |
|---|---|---|
| `calls` | 1.0 | Direct method invocation |
| `implements` | 1.0 | Class implements interface |
| `extends` | 1.0 | Class or interface inheritance |
| `overrides` | 0.95 | Method override (resolved at parse time) |
| `uses_field` | 0.9 | Read or write access to a field |
| `throws` | 1.0 | Declared or inferred exception |
| `annotated_by` | 1.0 | Symbol carries an annotation |
| `instantiates` | 0.9 | `new Foo(…)` constructor call |

#### Provenance Values

| Value | Meaning |
|---|---|
| `static` | Resolved deterministically by the tree-sitter parser |
| `inferred` | Heuristically resolved (e.g. type-hierarchy lookup) |
| `manual` | Added by a human or post-processor |

---

### `evidence` — Parser-Level Evidence

```sql
CREATE TABLE IF NOT EXISTS evidence (
    id                  TEXT    PRIMARY KEY,
    edge_id             TEXT    NOT NULL REFERENCES edge(id),
    file                TEXT    NOT NULL DEFAULT '',
    line                INTEGER NOT NULL DEFAULT 0,
    parser_version      TEXT    NOT NULL DEFAULT '',
    freshness_ts        TEXT    NOT NULL DEFAULT (datetime('now')),
    verification_status TEXT    NOT NULL DEFAULT 'verified'
                            CHECK (verification_status IN ('verified', 'stale', 'ambiguous'))
);
```

| Column | Type | Description |
|---|---|---|
| `id` | TEXT PK | Stable hash of `(edge_id, file, line)` |
| `edge_id` | TEXT FK | Parent edge id |
| `file` | TEXT | Repo-relative source path |
| `line` | INTEGER | 1-based line number of the call site |
| `parser_version` | TEXT | tree-sitter grammar version used |
| `freshness_ts` | TEXT | ISO-8601 UTC timestamp of last verification |
| `verification_status` | TEXT | `verified` \| `stale` \| `ambiguous` |

**Index:**
- `idx_evidence_edge` on `(edge_id)`

---

## Freshness Model

An index is **fresh** when all of these conditions hold:

1. Every `node.commit_hash` matches the current `HEAD` of the repository.
2. No `evidence.verification_status = 'stale'` rows exist.
3. The last full build completed without errors.

`fabric check` evaluates these conditions and prints `OK` or `STALE`.

---

## Blind Spots

The schema stores only what static analysis can resolve. The following
categories of dynamic dispatch are **not represented**:

- Spring / CDI bean injection beyond declared type
- MQ listener call targets (runtime binding)
- Reflection (`Class.forName`, `Method.invoke`, etc.)
- Config-driven routing (`@ConditionalOnProperty`, etc.)
- Generated code (MyBatis Example, Lombok, APT, etc.)

> **"No callers found" ≠ dead code.**

---

## Changelog

| Version | Date | Notes |
|---|---|---|
| 0.0.1-dev | 2026-08-17 | Initial schema — three tables, FTS5, indexes |
