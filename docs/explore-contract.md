# `explore()` Contract v1.1

> Status: **FROZEN v1.1** (2026-08-18) — see [ADR-0001](adr/0001-schema-and-explore-contract-v1.md) and [ADR-0002](adr/0002-m2-resolution-and-freshness.md)
> This is the ONLY tool exposed via MCP. All other tools are hidden by default.

---

## Signature

```python
def explore(
    query: str,
    task: Literal["understand", "impact", "locate", "debug"] = "understand",
    repo: str = ".",
    max_tokens: int = 4000,
    depth: int = 2,
    min_confidence: float = 0.5,
) -> ExploreResult:
    ...
```

### Parameters

| param | type | description |
|-------|------|-------------|
| `query` | str | Natural-language or symbol-name query. Examples: `"who calls submitDuty"`, `"OrderService#place"`, `"authentication flow"` |
| `task` | enum | See Task Modes below |
| `repo` | str | Repo root path. Default `.` (cwd). Supports multiple indexed repos via absolute path |
| `max_tokens` | int | Soft budget for combined source slices in response. Default 4000 |
| `depth` | int | Graph diffusion hop depth. Default 2. Max 4 |
| `min_confidence` | float | Edge confidence threshold. Default 0.5 |

---

## Task Modes

| task | what it does | primary graph operation |
|------|-------------|------------------------|
| `understand` | Return symbol slices + immediate call context | FTS seed → 1-hop neighbours → verbatim source slices, sorted by PageRank |
| `impact` | Return blast radius of changing a symbol | Reverse BFS from seed: all callers up to `depth` hops + confidence-weighted risk level |
| `locate` | Find where a behaviour lives; return ranked candidates | FTS + graph diffusion; emphasise entry-point nodes |
| `debug` | Trace a call path from A to B | Shortest path in call graph + slices along path |

---

## Response schema

```typescript
interface ExploreResult {
  query: string;
  task: string;
  repo: string;

  // Ranked list of source slices
  slices: Slice[];

  // For task=impact: callers by depth
  blast_radius?: BlastRadiusEntry[];

  // For task=debug: call path
  call_path?: CallPathEntry[];

  // Disambiguation: populated when query matched >1 symbol
  candidates?: Candidate[];

  // Stats
  stats: {
    nodes_visited: number;
    edges_traversed: number;
    tokens_estimated: number;
    freshness: "ok" | "stale";   // stale → agent must run `fabric build` first
  };

  // MANDATORY — always present, never omitted
  blind_spots: string;  // fixed string (see below)
}

interface Slice {
  node_id: string;
  file: string;
  span_start: number;
  span_end: number;
  source: string;        // verbatim source lines, trimmed to max_tokens budget
  qualified_name: string;
  confidence: number;    // of the edge that led here; 1.0 for the seed
  provenance: string;
  truncated?: boolean;   // true when max_tokens shortened this verbatim slice
  entry_point?: boolean; // additive v1.1 node metadata
  entry_point_kind?: string;
}

interface BlastRadiusEntry {
  depth: number;
  node_id: string;
  qualified_name: string;
  file: string;
  edge_type: string;
  confidence: number;
  risk: "will_break" | "likely_affected" | "possible";
}

interface CallPathEntry {
  step: number;
  node_id: string;
  qualified_name: string;
  file: string;
  span_start: number;
  edge_type: string;
  confidence: number;
}

interface Candidate {
  node_id: string;
  qualified_name: string;
  file: string;
  score: number;
}
```

### `blind_spots` fixed value (MUST NOT be modified by the server)

```
Static analysis only. Not represented: Spring bean injection dispatch beyond
declared type, MQ listener call targets, reflection, config-driven routing,
generated code (MyBatis Example, etc.). "No callers found" ≠ dead code.
Always verify with grep/source before concluding.
```

---

## Error / freshness behaviour

| condition | behaviour |
|-----------|-----------|
| `stats.freshness == "stale"` | Result still returned, but `freshness` is `"stale"`. Agent MUST run `fabric build` or start `fabric watch` before acting |
| Repo not indexed | Error: `{"error": "not_indexed", "hint": "run: fabric build"}` |
| No results above `min_confidence` | `slices: []`, `blind_spots` still present |
| Multiple candidates, unresolved | `slices: []`, `candidates` populated with ranked list |

---

## Hidden tools (available via env `FABRIC_MCP_TOOLS=status,reindex,debug_graph`)

| tool | purpose |
|------|---------|
| `status` | Index stats, freshness, last build time |
| `reindex` | Trigger incremental or full rebuild |
| `debug_graph` | Raw node/edge dump for a symbol (diagnosis only) |

---

## Coverage of Graft's five commands (gap check)

| Graft command | Covered by `explore()` | how |
|--------------|----------------------|-----|
| `graft ask` | ✅ | `task=understand` or `task=locate` |
| `graft callers` | ✅ | `task=impact` (reverse BFS) |
| `graft skeleton` | ✅ | `task=understand` with file path in query → slices = API surface |
| `graft map` | ✅ | `task=locate` with broad query → PageRank highlights hubs |
| `graft grep` | ✅ | literal query string triggers FTS exhaustive match |

No gaps. Contract covers all five command shapes.

---

## Changelog

| version | date | change |
|---------|------|--------|
| v1 | 2026-08-17 | initial draft |
| v1 (frozen) | 2026-08-17 | M1 response and four task modes. See ADR-0001. |
| v1.1 (frozen) | 2026-08-18 | Backward-compatible slice entry-point metadata; graph traversal may include `REFERENCES`; ambiguous references remain outside resolved edges. See ADR-0002. |
| v1.1 (unchanged) | 2026-08-18 | M3 typed overlay (ADR-0003) needs no contract bump: `edge_type`/`provenance` are already free-text strings, so slices may now surface `CALLS_TYPED`/`REFERENCES_TYPED`/`EXTENDS_TYPED`/`IMPLEMENTS_TYPED` and `scip_java`/`scip_java+tree_sitter_java` values without any new field or schema change on the response side. |
