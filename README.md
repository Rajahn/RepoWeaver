# RepoWeaver

> **Code context fabric for AI coding agents** — deterministic call-graph indexing, hybrid retrieval, and a single-tool MCP interface.

RepoWeaver builds a local, zero-cost code intelligence layer that lets AI coding agents (Codex, Claude Code, and other MCP clients) answer structural questions without reading entire files.

```
"Who calls this method, and what would break if I change its signature?"
→ explore(query, task="impact", repo=".", max_tokens=4000)
→ [verbatim source slices + call paths + blast radius + known blind spots]
```

## Why

AI agents that rely on grep or full-file reads waste tokens and miss cross-file call chains.
Existing tools either require cloud APIs, embed LLMs in the index layer, or carry restrictive licenses.
RepoWeaver takes only the verified-consensus patterns from the landscape and assembles them locally.

## Design

Six primitives, all independently validated across multiple open-source tools:

| # | Primitive | Validated by |
|---|-----------|-------------|
| 1 | tree-sitter deterministic parsing, local $0 | CodeGraph · GitNexus · Graft · Aider |
| 2 | Call / inherit / import edges + blast-radius | CodeGraph · GitNexus · Graft · CodeQL |
| 3 | BM25/FTS + PageRank graph diffusion | Graft · CodeGraph · Aider |
| 4 | Content-hash incremental freshness | Graft · Codebase-Memory · CodeGraph |
| 5 | MCP single strong tool `explore()` | CodeGraph (empirically validated) |
| 6 | AGENTS.md protocol injection + edit hook | GitNexus · CodeGraph · Graft · Potpie |

**Not included**: LLM in the index layer (rejected by every serious tool), code sent to external APIs, opinionated semantic summaries.

## Roadmap

| Milestone | Tag | Status |
|-----------|-----|--------|
| M1 — Fabric MVP (Java parser + edges + FTS5 + PageRank + MCP) | v0.1.0 | ✅ shipped |
| M2 — Freshness & confidence (auto-sync + disambiguation + confidence edges) | v0.2.0 | ✅ shipped |
| M3 — Type precision overlay (SCIP) | v0.3.0 | ✅ shipped |
| M4 — Runtime overlay (OTel/Jaeger trace → edge weights) | v0.4.0 | planned |

Each milestone ships a `fabric verify --level mN` gate that runs in CI.

## Verification approach

Benchmarks use public repos only — no proprietary code enters this repository.

| Benchmark repo | Purpose |
|----------------|---------|
| `spring-projects/spring-petclinic` | Spring DI-heavy, tests injection-aware edges |
| `google/gson` | Comparable to CodeGraph's published 93.3% Java coverage |
| `square/okhttp` | Call-chain depth |
| `mybatis/mybatis-3` | Generated-code noise filtering |

## SOTA alignment — measured, not claimed

RepoWeaver ships a reproducible benchmark harness and refuses to count
low-confidence or ambiguous edges as resolved coverage.

Pinned commit `dae37cf…`; apples-to-apples scope `gson/src/main/`; both source
and target files must be inside the scope.

| Metric | v0.1 whole-repo baseline | RepoWeaver v0.2 core | CodeGraph 1.5 core | Gate |
|---|---:|---:|---:|---:|
| Resolved cross-file dependent coverage | 35.3% | **90.12%** | 92.59% | >=90% |
| Ambiguous edge rate | 89.3% | **6.36%** | not exposed | <=10% |
| Fixture edge precision / recall | 1.0 / 1.0 | **1.0 / 1.0** | not measured | >=.95 / >=.90 |

RepoWeaver passes its release gates and is in the same measured coverage band,
while remaining 2.47 percentage points behind CodeGraph on this benchmark. We
therefore claim **alignment**, not universal superiority. Coverage cannot be
improved by adding ambiguous edges: the gate pairs it with ambiguity and
fixture precision.

```bash
fabric verify --level benchmark
fabric benchmark run --repo /path/to/gson --name gson \
  --scope-prefix gson/src/main/ --output gson.json
fabric benchmark compare --candidate gson.json --target benchmarks/sota-targets.yaml
```

See [benchmark methodology](docs/benchmark-methodology.md), the checked-in
[RepoWeaver v0.2 baseline](benchmarks/baselines/v0.2.0-gson-core.md), and the
[CodeGraph 1.5 comparison](benchmarks/baselines/codegraph-1.5.0-gson-core.md).

## Install

```bash
git clone <this-repo> && cd repoweaver
uv sync --extra dev

uv run fabric build /path/to/your/java/repo     # index a repo, $0, no LLM
uv run fabric check /path/to/your/java/repo      # OK | STALE (content-hash freshness)
uv run fabric init /path/to/your/java/repo       # inject AGENTS.md protocol block
uv run fabric watch /path/to/your/java/repo      # OS-event auto-sync, 2s debounce
uv run fabric verify --level m2                  # watcher + incremental consistency gate
uv run fabric overlay scip --repo . --index path/to/index.scip  # layer typed edges (M3)
uv run fabric verify --level m3                  # typed overlay merge/precision gate
uv run fabric serve                              # start the MCP server (explore() tool)
```

v0.3.0 supports Java only, via tree-sitter plus an optional SCIP-derived
typed overlay. M2 adds conservative overload/type resolution, `REFERENCES`,
annotation symbols, unresolved-candidate storage, framework entry-point
metadata and auto-sync. M3 adds `fabric overlay scip`, which merges
compiler-derived `*_TYPED` edges onto the existing graph without ever
dropping an edge — see `docs/adr/0003-typed-overlay.md`. No LLM or external
network call is made at runtime.

## MCP tool

```
explore(
  query: str,
  task: "understand" | "impact" | "locate" | "debug",
  repo: str = ".",
  max_tokens: int = 4000
) → {
  query, task, repo,
  slices: [{node_id, file, span_start, span_end, source, qualified_name, confidence, provenance}],
  stats: {nodes_visited, edges_traversed, tokens_estimated, freshness},
  blind_spots: "<frozen incompleteness contract string>",
  # task == "impact": + blast_radius: [{depth, node_id, qualified_name, file, edge_type, confidence, risk}]
  # task == "debug":  + call_path: [{step, node_id, qualified_name, file, edge_type, confidence}]
}
```

See [`docs/explore-contract.md`](docs/explore-contract.md) (frozen v1.1) for the full contract.

## Acknowledgements

Design patterns drawn from (not forked from):
[CodeGraph](https://github.com/colbymchenry/codegraph) · [Graft](https://github.com/NanoNets/Graft) · [GitNexus](https://github.com/abhigyanpatwari/GitNexus) · [Serena](https://github.com/oraios/serena) · [SCIP](https://github.com/sourcegraph/scip) · [Aider](https://github.com/Aider-AI/aider)

## License

MIT
