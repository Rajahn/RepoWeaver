-- RepoWeaver graph schema (v1 frozen core + v1.1 additive — see docs/schema.md
-- and docs/adr/0001-*.md, docs/adr/0002-m2-resolution-and-freshness.md).
-- SQLite DDL for node, edge, evidence, file_meta, unresolved_reference, and
-- file_refs_cache tables. Applied automatically by GraphStore._apply_schema().

PRAGMA foreign_keys = ON;

-- ──────────────────────────────────────────────────────────────────────
-- node — indexed symbol (method, class, interface, field, …)
-- ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS node (
    id              TEXT    PRIMARY KEY,          -- "{kind}:{repo_slug}:{file}:{qualified_name}"
    kind            TEXT    NOT NULL,             -- class | interface | enum | enum_constant | method | constructor | field
    language        TEXT    NOT NULL DEFAULT 'java',
    repo            TEXT    NOT NULL DEFAULT '',  -- repository root path (local absolute)
    file            TEXT    NOT NULL DEFAULT '',  -- repo-relative source path
    span_start      INTEGER NOT NULL DEFAULT 0,   -- 1-based line, inclusive
    span_end        INTEGER NOT NULL DEFAULT 0,   -- 1-based line, inclusive
    qualified_name  TEXT    NOT NULL DEFAULT '',  -- fully-qualified symbol name
    simple_name     TEXT    NOT NULL DEFAULT '',  -- unqualified symbol name
    signature       TEXT    NOT NULL DEFAULT '',  -- method signature or class header
    commit_hash     TEXT    NOT NULL DEFAULT '',  -- git HEAD at index time; '' if not a git repo
    indexed_at      INTEGER NOT NULL DEFAULT 0,   -- unix epoch seconds
    -- v1.1 additive (M2): framework entry-point flag — never a self-loop edge.
    is_entry_point    INTEGER NOT NULL DEFAULT 0,
    entry_point_kind  TEXT    NOT NULL DEFAULT ''  -- '' | HTTP_CONTROLLER | HTTP_ROUTE | SCHEDULED | MESSAGE_LISTENER
);

CREATE INDEX IF NOT EXISTS idx_node_file   ON node (repo, file);
CREATE INDEX IF NOT EXISTS idx_node_qname  ON node (qualified_name);
CREATE INDEX IF NOT EXISTS idx_node_simple ON node (simple_name);

-- Full-text search over node names and signatures.
-- External-content FTS5 table: does NOT auto-sync. Triggers below are mandatory
-- (see docs/adr/0001-schema-and-explore-contract-v1.md #1).
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5 (
    simple_name,
    qualified_name,
    signature,
    content='node',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS node_ai AFTER INSERT ON node BEGIN
    INSERT INTO node_fts(rowid, simple_name, qualified_name, signature)
    VALUES (new.rowid, new.simple_name, new.qualified_name, new.signature);
END;

CREATE TRIGGER IF NOT EXISTS node_ad AFTER DELETE ON node BEGIN
    INSERT INTO node_fts(node_fts, rowid, simple_name, qualified_name, signature)
    VALUES ('delete', old.rowid, old.simple_name, old.qualified_name, old.signature);
END;

CREATE TRIGGER IF NOT EXISTS node_au AFTER UPDATE ON node BEGIN
    INSERT INTO node_fts(node_fts, rowid, simple_name, qualified_name, signature)
    VALUES ('delete', old.rowid, old.simple_name, old.qualified_name, old.signature);
    INSERT INTO node_fts(rowid, simple_name, qualified_name, signature)
    VALUES (new.rowid, new.simple_name, new.qualified_name, new.signature);
END;

-- ──────────────────────────────────────────────────────────────────────
-- edge — directed relationship between two nodes
-- ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS edge (
    id                   TEXT    PRIMARY KEY,   -- sha256(f"{from_id}|{to_id}|{type}")[:16]
    from_id              TEXT    NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    to_id                TEXT    NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    type                 TEXT    NOT NULL,      -- CALLS | EXTENDS | IMPLEMENTS | IMPORTS | ROUTES_TO | RUNTIME_CALLS
    provenance           TEXT    NOT NULL,      -- tree_sitter_java | scip_java | jdtls | otel_trace | rule_entry_point
    confidence           REAL    NOT NULL DEFAULT 1.0
                             CHECK (confidence BETWEEN 0.0 AND 1.0),
    observed_at          INTEGER NOT NULL DEFAULT 0,  -- unix epoch seconds
    source_hash          TEXT    NOT NULL DEFAULT '', -- hash of source file at index time
    ambiguous_candidates TEXT    NOT NULL DEFAULT '[]' -- JSON array of candidate node ids, if resolution was ambiguous
);

CREATE INDEX IF NOT EXISTS idx_edge_from ON edge (from_id);
CREATE INDEX IF NOT EXISTS idx_edge_to   ON edge (to_id);
CREATE INDEX IF NOT EXISTS idx_edge_type ON edge (type);

-- ──────────────────────────────────────────────────────────────────────
-- evidence — parser-level evidence backing an edge (one edge, many call sites)
-- ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS evidence (
    id                  TEXT    PRIMARY KEY,   -- sha256(f"{edge_id}|{file}|{line}")[:16]
    edge_id              TEXT    NOT NULL REFERENCES edge(id) ON DELETE CASCADE,
    file                TEXT    NOT NULL DEFAULT '',  -- repo-relative source path
    line                INTEGER NOT NULL DEFAULT 0,   -- 1-based line number
    parser_version      TEXT    NOT NULL DEFAULT '',  -- e.g. "tree-sitter-java 0.23.5"
    freshness_ts        INTEGER NOT NULL DEFAULT 0,   -- unix epoch seconds of file mtime at index time
    verification_status TEXT    NOT NULL DEFAULT 'verified'
                            CHECK (verification_status IN ('verified', 'stale', 'ambiguous'))
);

CREATE INDEX IF NOT EXISTS idx_evidence_edge ON evidence (edge_id);

-- ──────────────────────────────────────────────────────────────────────
-- file_meta — per-file content hash for freshness (`fabric check`)
-- ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS file_meta (
    file           TEXT    PRIMARY KEY,   -- repo-relative path
    content_hash   TEXT    NOT NULL DEFAULT '',  -- sha256 of file bytes at last index
    indexed_at     INTEGER NOT NULL DEFAULT 0,   -- unix epoch seconds
    node_count     INTEGER NOT NULL DEFAULT 0
);

-- ──────────────────────────────────────────────────────────────────────
-- unresolved_reference — v1.1 additive (M2). A call/type reference that
-- matched more than one equally-valid candidate. Never counted as a
-- resolved edge; a resolved `edge` row always points at exactly one target.
-- ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS unresolved_reference (
    id            TEXT    PRIMARY KEY,   -- sha256(f"{from_id}|{type}|{target_name}")[:16]
    from_id       TEXT    NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    type          TEXT    NOT NULL,      -- CALLS | EXTENDS | IMPLEMENTS
    target_name   TEXT    NOT NULL DEFAULT '',  -- method/type simple name that was ambiguous
    candidates    TEXT    NOT NULL DEFAULT '[]', -- JSON array of candidate node ids, length >= 2
    reason        TEXT    NOT NULL DEFAULT '',  -- ambiguous_owner_chain | ambiguous_type | ambiguous_global_fallback | ambiguous_supertype
    file          TEXT    NOT NULL DEFAULT '',  -- first-observed call/reference site
    line          INTEGER NOT NULL DEFAULT 0,
    site_count    INTEGER NOT NULL DEFAULT 1,   -- number of source sites merged into this row
    observed_at   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_unresolved_from ON unresolved_reference (from_id);
CREATE INDEX IF NOT EXISTS idx_unresolved_type ON unresolved_reference (type);

-- ──────────────────────────────────────────────────────────────────────
-- file_refs_cache — v1.1 additive (M2). Internal build-performance cache of
-- one file's raw (unresolved) parser output, keyed by content hash. Lets
-- `fabric watch`/incremental build skip re-parsing unchanged files while
-- still feeding them into every global re-resolution pass. Never read by
-- `explore()` — purely an indexer implementation detail, not part of the
-- response contract.
-- ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS file_refs_cache (
    file          TEXT    PRIMARY KEY,
    content_hash  TEXT    NOT NULL DEFAULT '',
    payload       TEXT    NOT NULL DEFAULT ''   -- JSON-serialized raw ParsedFile
);
