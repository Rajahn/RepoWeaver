# Code Context Fabric

> **Code context fabric for AI coding agents** — deterministic call-graph indexing, hybrid retrieval, and a single-tool MCP interface.

Code Context Fabric builds a local, zero-cost code intelligence layer that lets AI coding agents (Codex, Claude Code, and other MCP clients) answer structural questions without reading entire files.

```
"Who calls this method, and what would break if I change its signature?"
→ explore(query, task="impact", repo=".", max_tokens=4000)
→ [verbatim source slices + call paths + blast radius + known blind spots]
```

## Why

AI agents that rely on grep or full-file reads waste tokens and miss cross-file call chains.
Existing tools either require cloud APIs, embed LLMs in the index layer, or carry restrictive licenses.
Code Context Fabric takes only the verified-consensus patterns from the landscape and assembles them locally.

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
| M4-0 — Query facade (qualified syntax, ambiguity panorama, cluster ranking, configurable entry points) | v0.4.0 | ✅ shipped |
| M4 — Runtime overlay (OTel/Jaeger trace → edge weights) | v0.4.1 | planned |

Each milestone ships a `ccf verify --level mN` gate that runs in CI.

## Verification approach

Benchmarks use public repos only — no proprietary code enters this repository.

| Benchmark repo | Purpose |
|----------------|---------|
| `spring-projects/spring-petclinic` | Spring DI-heavy, tests injection-aware edges |
| `google/gson` | Comparable to CodeGraph's published 93.3% Java coverage |
| `square/okhttp` | Call-chain depth |
| `mybatis/mybatis-3` | Generated-code noise filtering |

## SOTA alignment — measured, not claimed

Code Context Fabric ships a reproducible benchmark harness and refuses to count
low-confidence or ambiguous edges as resolved coverage.

Pinned commit `dae37cf…`; apples-to-apples scope `gson/src/main/`; both source
and target files must be inside the scope.

| Metric | v0.1 whole-repo baseline | Code Context Fabric v0.2 core | CodeGraph 1.5 core | Gate |
|---|---:|---:|---:|---:|
| Resolved cross-file dependent coverage | 35.3% | **90.12%** | 92.59% | >=90% |
| Ambiguous edge rate | 89.3% | **6.36%** | not exposed | <=10% |
| Fixture edge precision / recall | 1.0 / 1.0 | **1.0 / 1.0** | not measured | >=.95 / >=.90 |

Code Context Fabric passes its release gates and is in the same measured coverage band,
while remaining 2.47 percentage points behind CodeGraph on this benchmark. We
therefore claim **alignment**, not universal superiority. Coverage cannot be
improved by adding ambiguous edges: the gate pairs it with ambiguity and
fixture precision.

```bash
ccf verify --level benchmark
ccf benchmark run --repo /path/to/gson --name gson \
  --scope-prefix gson/src/main/ --output gson.json
ccf benchmark compare --candidate gson.json --target benchmarks/sota-targets.yaml
```

See [benchmark methodology](docs/benchmark-methodology.md), the checked-in
[Code Context Fabric v0.2 baseline](benchmarks/baselines/v0.2.0-gson-core.md), and the
[CodeGraph 1.5 comparison](benchmarks/baselines/codegraph-1.5.0-gson-core.md).

### Indexing performance (v0.5.0, measured on an 884-file / 16.7k-node repo)

| Operation | Time |
|---|---|
| Full build (884 files) | ~10.3s |
| Incremental sync, 1 changed file | 0.36s |
| Incremental sync, 20 changed files | 0.61s |

`Indexer.build_incremental` takes a fast path — re-resolving only the
changed files — whenever it can prove the repo-wide symbol table is
unaffected (see [ADR-0005](docs/adr/0005-incremental-sync-and-parallel-parse.md));
otherwise it falls back to a full rebuild. Both incremental cases above are
confirmed byte-identical (`graph_signature`) to a full rebuild. Full builds
parse files in parallel via `ProcessPoolExecutor` once a batch is large
enough to amortize pool startup.

## Install

```bash
git clone https://github.com/Rajahn/code-context-fabric && cd code-context-fabric
uv sync --extra dev

uv run ccf build /path/to/your/java/repo     # index a repo, $0, no LLM
uv run ccf check /path/to/your/java/repo      # OK | STALE (content-hash freshness)
uv run ccf init /path/to/your/java/repo       # inject AGENTS.md protocol block
uv run ccf watch /path/to/your/java/repo      # OS-event auto-sync, 2s debounce
uv run ccf verify --level m2                  # watcher + incremental consistency gate
uv run ccf overlay scip --repo . --index path/to/index.scip  # layer typed edges (M3)
uv run ccf verify --level m3                  # typed overlay merge/precision gate
uv run ccf verify --level query               # query-facade gate (qualified syntax, panorama, cluster rank)
uv run ccf serve                              # start the MCP server (explore() tool)
```

`fabric` remains available as a compatibility alias for every `ccf` command above.

v0.3.0 supports Java only, via tree-sitter plus an optional SCIP-derived
typed overlay. M2 adds conservative overload/type resolution, `REFERENCES`,
annotation symbols, unresolved-candidate storage, framework entry-point
metadata and auto-sync. M3 adds `ccf overlay scip`, which merges
compiler-derived `*_TYPED` edges onto the existing graph without ever
dropping an edge — see `docs/adr/0003-typed-overlay.md`. v0.4.0 adds a
query facade in front of the same graph — see `docs/adr/0004-query-facade.md`
— plus an optional `.repoweaver/entrypoints.yaml` in your own repo to extend
or replace the built-in (public-annotation-only) entry-point table with your
own project's annotations, without ever committing internal names here. No
LLM or external network call is made at runtime.

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
