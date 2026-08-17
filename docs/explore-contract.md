# `explore()` Tool Contract

This document specifies the public interface of the `explore()` MCP tool
exposed by the RepoWeaver server. Agents and integrators should treat this
document as the authoritative contract; the implementation in
`src/repoweaver/server/mcp.py` must conform to it.

---

## Function Signature

```python
def explore(
    query: str,
    task: str = "understand",
    repo: str = ".",
    max_tokens: int = 4000,
    depth: int = 2,
    min_confidence: float = 0.5,
) -> dict: ...
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | `str` | *(required)* | Symbol name, qualified name, or natural-language description |
| `task` | `str` | `"understand"` | Query mode — see Task Modes below |
| `repo` | `str` | `"."` | Path to repository root (absolute or CWD-relative) |
| `max_tokens` | `int` | `4000` | Soft budget for the response payload in tokens |
| `depth` | `int` | `2` | Maximum graph-traversal depth from seed nodes |
| `min_confidence` | `float` | `0.5` | Minimum edge confidence threshold (0.0–1.0) |

---

## Task Modes

| Mode | Description |
|---|---|
| `understand` | Return symbol definition, direct callers, callees, and type hierarchy. Default mode. |
| `impact` | Blast-radius analysis: all transitive callers within `depth` hops. |
| `locate` | Best-match symbol lookup — useful when the exact qualified name is unknown. |
| `debug` | Call-path tracing between two symbols (supply `query` as `"A -> B"`). |

---

## Response Structure

```typescript
interface ExploreResponse {
  query: string;
  task: string;
  slices: Slice[];
  stats: Stats;
  blind_spots: string;   // fixed string — see below
  _note?: string;        // present only when index is absent or stale
}

interface Slice {
  node_id: string;
  qualified_name: string;
  simple_name: string;
  kind: string;             // method | class | interface | field | constructor
  file: string;             // repo-relative path
  span_start: number;       // byte offset
  span_end: number;         // byte offset
  score: float;             // combined retrieval score
  callers?: Candidate[];    // present for task=understand|impact
  callees?: Candidate[];    // present for task=understand
  blast_radius?: BlastRadiusEntry[];   // present for task=impact
  call_path?: CallPathEntry[];         // present for task=debug
}

interface Candidate {
  node_id: string;
  qualified_name: string;
  confidence: float;
  edge_type: string;
}

interface BlastRadiusEntry {
  node_id: string;
  qualified_name: string;
  depth: number;            // hops from seed
  confidence: float;        // product of edge confidences along path
}

interface CallPathEntry {
  from_id: string;
  to_id: string;
  edge_type: string;
  confidence: float;
  file: string;
  line: number;
}

interface Stats {
  nodes_visited: number;
  edges_traversed: number;
  tokens_estimated: number;
  freshness: "ok" | "stale" | "missing";
}
```

---

## `blind_spots` — Fixed String

The `blind_spots` field always contains exactly the following text:

```
Static analysis only. Not represented: Spring bean injection dispatch beyond
declared type, MQ listener call targets, reflection, config-driven routing,
generated code (MyBatis Example, etc.).
'No callers found' != dead code. Always verify with grep/source before concluding.
```

Agents **must not** suppress or truncate this field.

---

## Error and Freshness Behaviour

| Condition | `stats.freshness` | `_note` present | `slices` |
|---|---|---|---|
| Index present and fresh | `"ok"` | No | Populated |
| Index present but stale | `"stale"` | Yes | Populated (may be stale) |
| Index missing | `"missing"` | Yes (`"stub — run \`fabric build\` first"`) | `[]` |
| `repo` path not found | — | — | Raises `ValueError` |

---

## Hidden Tools

Two additional diagnostic tools are available when the `FABRIC_MCP_TOOLS`
environment variable contains their names (comma-separated):

| Tool | Activation | Description |
|---|---|---|
| `status` | `FABRIC_MCP_TOOLS=status` | Index status, freshness, last build time |
| `reindex` | `FABRIC_MCP_TOOLS=reindex` | Trigger incremental or full index rebuild |

These tools are intentionally hidden from the default tool list to keep the
agent's context window clean.

---

## Graft Five-Command Coverage Mapping

RepoWeaver is designed to subsume the five core Graft commands via the single
`explore()` tool. The mapping is:

| Graft Command | `explore()` Equivalent | Coverage |
|---|---|---|
| `graft symbols <query>` | `explore(query, task="locate")` | ✅ |
| `graft callers <symbol>` | `explore(symbol, task="understand")` → `slices[].callers` | ✅ |
| `graft callees <symbol>` | `explore(symbol, task="understand")` → `slices[].callees` | ✅ |
| `graft impact <symbol>` | `explore(symbol, task="impact")` → `slices[].blast_radius` | ✅ |
| `graft path <A> <B>` | `explore("A -> B", task="debug")` → `slices[].call_path` | ✅ |

---

## Changelog

| Version | Date | Notes |
|---|---|---|
| 0.0.1-dev | 2026-08-17 | Initial contract — stub implementation, full spec |
