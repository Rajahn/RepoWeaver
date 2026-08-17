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
| M2 — Freshness & confidence (auto-sync + disambiguation + confidence edges) | v0.2.0 | planned |
| M3 — Type precision overlay (SCIP/jdtls) | v0.3.0 | planned |
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

| Metric on pinned `google/gson` | v0.1.0 baseline | Alignment target | Status |
|---|---:|---:|---|
| Resolved cross-file dependent coverage | 35.3% | >=90% (CodeGraph publishes 93.3% under its policy) | ❌ gap |
| Ambiguous edge rate | 89.3% | <=10% | ❌ gap |

This baseline is intentionally visible: v0.1 proves the closed loop, not SOTA
resolution quality. The next engineering work targets receiver/type resolution,
disambiguation and framework entry points. Coverage may not improve by adding
ambiguous edges; release gates pair it with ambiguity and fixture precision.

```bash
fabric verify --level benchmark
fabric benchmark run --repo /path/to/gson --name gson --output gson.json
fabric benchmark compare --candidate gson.json --target benchmarks/sota-targets.yaml
```

See [benchmark methodology](docs/benchmark-methodology.md) and the checked-in
[v0.1 Gson baseline](benchmarks/baselines/v0.1.0-gson.json).

## Install

```bash
git clone <this-repo> && cd repoweaver
uv sync --extra dev

uv run fabric build /path/to/your/java/repo     # index a repo, $0, no LLM
uv run fabric check /path/to/your/java/repo      # OK | STALE (content-hash freshness)
uv run fabric init /path/to/your/java/repo       # inject AGENTS.md protocol block
uv run fabric verify --level m1                  # closed-loop self-check against the bundled fixture
uv run fabric serve                              # start the MCP server (explore() tool)
```

v0.1.0 (M1) supports Java only, via tree-sitter. No LLM and no external network
call is ever made at runtime — parsing, resolution, and retrieval are all local.

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

See [`docs/explore-contract.md`](docs/explore-contract.md) (frozen v1) for the full contract.

## Acknowledgements

Design patterns drawn from (not forked from):
[CodeGraph](https://github.com/colbymchenry/codegraph) · [Graft](https://github.com/NanoNets/Graft) · [GitNexus](https://github.com/abhigyanpatwari/GitNexus) · [Serena](https://github.com/oraios/serena) · [SCIP](https://github.com/sourcegraph/scip) · [Aider](https://github.com/Aider-AI/aider)

## License

MIT
