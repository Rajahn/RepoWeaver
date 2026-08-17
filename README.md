# RepoWeaver

> **Code context fabric for AI coding agents** — deterministic call-graph indexing, hybrid retrieval, and a single-tool MCP interface.

RepoWeaver builds a local, zero-cost code intelligence layer that lets AI coding agents (Codex, Claude Code, CodeWiz, etc.) answer structural questions without reading entire files.

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
| M1 — Fabric MVP (Java parser + edges + FTS5 + PageRank + MCP) | v0.1.0 | 🔜 |
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

## Install (coming in v0.1.0)

```bash
pip install repoweaver
fabric build          # index current repo, $0, no LLM
fabric init           # inject AGENTS.md protocol + edit hook
fabric verify --level m1
```

## MCP tool

```
explore(
  query: str,
  task: "understand" | "impact" | "locate" | "debug",
  repo: str = ".",
  max_tokens: int = 4000
) → {
  slices: [{file, span, source}],
  call_paths: [...],
  blast_radius: [...],
  blind_spots: "DI / reflection / MQ edges not represented"
}
```

## Acknowledgements

Design patterns drawn from (not forked from):
[CodeGraph](https://github.com/colbymchenry/codegraph) · [Graft](https://github.com/NanoNets/Graft) · [GitNexus](https://github.com/abhigyanpatwari/GitNexus) · [Serena](https://github.com/oraios/serena) · [SCIP](https://github.com/sourcegraph/scip) · [Aider](https://github.com/Aider-AI/aider)

## License

MIT
