# ADR-0002: M2 resolution, references, entry points and freshness

- Status: Accepted
- Date: 2026-08-18
- Schema: v1.1 (additive)
- Explore contract: v1.1 (backward compatible)

## Context

M1 proved the local indexing/retrieval loop but produced 89.3% ambiguous edge
mass and only 35.3% strict cross-file coverage on the pinned whole-repository
Gson baseline. The causes were measurable: global name-only call resolution,
no same-arity overload scoring, no type-use references, annotation declarations
missing from the symbol set, and no automatic refresh.

## Decisions

1. **Ambiguity is not an edge.** A reference with more than one equally valid
   candidate is stored in `unresolved_reference`; it never creates N low-value
   `edge` rows and never counts as resolved coverage.
2. **Resolution order is evidence-first.** Receiver owner, method name, arity,
   argument type hints, explicit/same-package/wildcard imports and inheritance
   are applied before any global fallback. A unique winner is required.
3. **Overload scoring is conservative.** Exact, boxing, widening and known
   repo-local subtype matches contribute evidence. Unknown reference relations
   remain neutral. `null` never breaks a tie between reference overloads.
4. **Nested calls may supply a type only when stable.** If every matching
   nested callee declares the same non-void return type, that type may be used
   as an argument hint; otherwise it remains unknown.
5. **Type use is a first-class `REFERENCES` edge.** Field, return, parameter,
   local-variable, generic, annotation, throws, cast, instanceof, class-literal
   and object-creation uses create an edge only after unique type resolution.
6. **Annotation declarations are symbol nodes.** Java `@interface` types use
   node kind `annotation` and participate in imports/references.
7. **Entry point is a node property.** `node.is_entry_point` and
   `node.entry_point_kind` record annotation-derived HTTP, scheduled and
   message-listener entry points. No self-loop edge is created.
8. **Incremental means parse-incremental, resolve-global.** Changed files are
   re-parsed; unchanged raw parser output comes from `file_refs_cache`; the
   complete graph is globally re-resolved so delete/rename/target changes cannot
   leave stale inbound edges.
9. **Watcher batches filesystem truth.** `watchfiles` provides OS events with a
   2-second default debounce. For every touched path, existence on disk decides
   changed vs deleted; event ordering is not trusted.
10. **Benchmark scopes are explicit.** Apples-to-apples comparisons record
    source prefixes and require both source and target files to be inside the
    scope. Missing required metrics fail the release gate.

## Consequences

- Schema v1.1 adds `node.is_entry_point`, `node.entry_point_kind`,
  `unresolved_reference` and `file_refs_cache`. Existing v1 databases migrate
  additively.
- Explore v1 responses remain valid. Slice entries may additionally include
  entry-point metadata; ambiguous symbol queries continue to return candidates.
- The pinned Gson core-source benchmark reaches the release gates while still
  reporting a 2.47 percentage-point coverage gap to the measured CodeGraph
  1.5.0 baseline. This is alignment, not a claim of universal superiority.
- Reflection, runtime DI dispatch, configuration routing and dynamic MQ dispatch
  remain outside static truth.
