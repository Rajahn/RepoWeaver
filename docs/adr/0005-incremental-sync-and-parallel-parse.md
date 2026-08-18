# ADR-0005: Incremental sync fast path and parallel parsing

## Status

Accepted (v0.5.0).

## Context

`Indexer._sync` rebuilt everything from scratch on every build/watch batch:
deserialize every cached `ParsedFile`, rebuild the full `SymbolTable` from
every node in the repo, and re-resolve every file — even for a one-line
edit. On an 884-file repo a single-file change cost the same as a full
build (1.29s+). Parsing itself is pure CPU with no shared state across
files, so it can run in a process pool without touching resolution.

## Decision

### Incremental fast path (P0-A)

`Indexer.build_incremental(changed, deleted)` dispatches to `_full_sync`
whenever `deleted` is non-empty or `changed` is empty. Otherwise it tries
`_try_fast_incremental(changed)`, which either returns a `BuildStats` for a
safe, changed-files-only resolve, or `None` to signal "fall back to
`_full_sync`" — never a partial or unsafe result.

The fast path is safe exactly when it can prove the repo-wide
`SymbolTable` and its `supertypes` map are unchanged. Both are pure
functions of `(kind, qualified_name, simple_name, signature)` across every
node in the repo, plus the current EXTENDS/IMPLEMENTS edge set. So:

1. Parse the changed files fresh. For each, compare the new node identity
   tuples against what's already stored for that file. Any mismatch (a
   symbol added, removed, renamed, or resignatured) means the symbol
   table could change repo-wide — bail to `None`.
2. Build a lightweight `SymbolTable` from a single
   `SELECT id, kind, qualified_name, simple_name, file, signature FROM node`
   query (no `ParsedFile` deserialization for untouched files), and
   populate `supertypes` from the existing `edge` table.
3. Resolve `resolve_type_refs` for the changed files only, and compare the
   resulting EXTENDS/IMPLEMENTS edges and unresolved-references against
   what's stored for those files' `from_id`s. Any mismatch means an
   inheritance relationship changed — bail to `None`, since that can
   affect `declared_lookup` for other files.
4. Only once both checks pass: resolve `resolve_calls`/`resolve_imports`/
   `resolve_type_uses` for the changed files and write only their rows.

Any change this can't safely reason about — a new file (could introduce
cross-file ambiguity for an existing zero-candidate reference, which is
silently dropped and leaves no trace to detect the collision from), a
signature change, a supertype change, or any deletion — degrades to the
existing full resolve. Correctness is enforced by construction: the fast
path either proves it's equivalent to a full resolve, or defers to one.

`tests/test_incremental_fast_path.py` is the correctness anchor: every case
builds twice (once via `build_incremental`, once via a from-scratch
`build()`) and asserts `graph_signature` is byte-identical, including a
12-step randomized watch-sequence simulation. `fabric verify --level perf`
runs the same style of check as a CI gate with a timing budget.

### Parallel parsing (P0-B)

`_parse_many` parses a batch of files with a `ProcessPoolExecutor`
(`min(8, os.cpu_count())` workers) once the batch is at least
`_PARALLEL_PARSE_THRESHOLD` (24) files, to amortize pool-startup cost;
smaller batches parse serially in-process. Workers are spawn-safe: the pool
uses a module-level `_init_parse_worker` initializer to build one
`JavaParser` per worker process (tree-sitter `Parser`/`Language` objects
aren't picklable, so each worker builds its own), and `_parse_in_worker` is
a module-level, picklable function. The dataclasses returned
(`ParsedFile`, `NodeRecord`, `CallRef`, `TypeRef`, `TypeUseRef`,
`ImportRef`) are plain `@dataclass`es with only primitive fields, so they
pickle across the process boundary without custom serialization.

Parsing has no shared state across files — each file's parse only reads
its own bytes and produces its own `ParsedFile` — so splitting it across
processes cannot change any result. Resolve and all store writes stay
single-threaded in the main process, since SQLite is single-writer.
`fabric verify --level perf` asserts a parallel and a forced-serial full
build produce an identical `graph_signature`.

## Consequences

- Single-file and 20-file-batch incremental syncs on an 884-file repo
  measure 0.36s and 0.61s respectively (target: <400ms for one file), both
  confirmed hash-identical to a full rebuild.
- The full 884-file build measures ~10.3s, down from an 11.9s baseline
  after also batching `GraphStore`'s per-file writes with `executemany`
  and removing `dataclasses.asdict`'s deepcopy cost from
  `_parsed_file_to_json` — but still short of the <6s target. Profiling
  found the dominant full-build cost is per-file SQLite write volume, not
  parsing; parallelizing parsing alone cannot close that gap. Closing it
  further needs a follow-up pass on the write path (e.g. a single
  multi-file write transaction instead of per-file `replace_file_*` calls).
- No change to edge types, confidence values, or resolution rules: the
  fast path only changes *which files get re-resolved*, never *how*
  resolution works, and the escalation conditions are deliberately
  conservative.
