-- RepoWeaver graph schema
-- SQLite DDL for node, edge, and evidence tables.
-- Applied automatically by GraphStore._apply_schema().

-- ──────────────────────────────────────────────────────────────────────
-- node — indexed symbol (method, class, interface, field, …)
-- ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS node (
    id              TEXT    PRIMARY KEY,          -- stable hash of (repo, file, qualified_name)
    kind            TEXT    NOT NULL,             -- method | class | interface | field | constructor
    language        TEXT    NOT NULL DEFAULT 'java',
    repo            TEXT    NOT NULL DEFAULT '',  -- repository root path or remote URL
    file            TEXT    NOT NULL DEFAULT '',  -- repo-relative source path
    span_start      INTEGER NOT NULL DEFAULT 0,   -- byte offset of declaration start
    span_end        INTEGER NOT NULL DEFAULT 0,   -- byte offset of declaration end
    qualified_name  TEXT    NOT NULL DEFAULT '',  -- fully-qualified symbol name
    simple_name     TEXT    NOT NULL DEFAULT '',  -- unqualified symbol name
    signature       TEXT    NOT NULL DEFAULT '',  -- method signature or class header
    commit_hash     TEXT    NOT NULL DEFAULT '',  -- git commit at index time
    indexed_at      TEXT    NOT NULL DEFAULT (datetime('now'))  -- ISO-8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_node_file   ON node (file);
CREATE INDEX IF NOT EXISTS idx_node_qname  ON node (qualified_name);
CREATE INDEX IF NOT EXISTS idx_node_simple ON node (simple_name);

-- Full-text search over node names and signatures
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5 (
    simple_name,
    qualified_name,
    signature,
    content='node',
    content_rowid='rowid'
);

-- ──────────────────────────────────────────────────────────────────────
-- edge — directed relationship between two nodes
-- ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS edge (
    id                   TEXT    PRIMARY KEY,   -- stable hash of (from_id, to_id, type)
    from_id              TEXT    NOT NULL REFERENCES node(id),
    to_id                TEXT    NOT NULL REFERENCES node(id),
    type                 TEXT    NOT NULL,      -- calls | implements | extends | overrides |
                                               -- uses_field | throws | annotated_by | instantiates
    provenance           TEXT    NOT NULL DEFAULT 'static',  -- static | inferred | manual
    confidence           REAL    NOT NULL DEFAULT 1.0
                             CHECK (confidence BETWEEN 0 AND 1),
    observed_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    source_hash          TEXT    NOT NULL DEFAULT '',   -- hash of the source file at index time
    ambiguous_candidates TEXT    NOT NULL DEFAULT '[]'  -- JSON array of alternative to_id candidates
);

CREATE INDEX IF NOT EXISTS idx_edge_from ON edge (from_id);
CREATE INDEX IF NOT EXISTS idx_edge_to   ON edge (to_id);

-- ──────────────────────────────────────────────────────────────────────
-- evidence — parser-level evidence backing an edge
-- ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS evidence (
    id                  TEXT    PRIMARY KEY,   -- stable hash of (edge_id, file, line)
    edge_id             TEXT    NOT NULL REFERENCES edge(id),
    file                TEXT    NOT NULL DEFAULT '',  -- repo-relative source path
    line                INTEGER NOT NULL DEFAULT 0,   -- 1-based line number
    parser_version      TEXT    NOT NULL DEFAULT '',  -- tree-sitter grammar version
    freshness_ts        TEXT    NOT NULL DEFAULT (datetime('now')),
    verification_status TEXT    NOT NULL DEFAULT 'verified'
                            CHECK (verification_status IN ('verified', 'stale', 'ambiguous'))
);

CREATE INDEX IF NOT EXISTS idx_evidence_edge ON evidence (edge_id);
