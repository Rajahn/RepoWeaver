# Benchmark report — codegraph-1.5.0-gson-core

- Tool: CodeGraph `1.5.0` (https://github.com/colbymchenry/codegraph)
- Repo: `google/gson@dae37cf0fe12235b76fb09f01118a0a8c8823f42`
- Scope prefixes: `gson/src/main/`
- Status: `MEASURED_PARTIAL` — only the fields below were actually observed from
  a real run; everything else is `null` (not measured), never zero/assumed.

## Metrics actually observed

| Metric | Value |
|---|---|
| Symbol-bearing files (scope) | 81 |
| Files with resolved incoming cross-file edge (scope) | 75 |
| Cross-file dependent coverage (scope) | 0.9259 |
| Nodes (whole repo, CodeGraph does not support scope filtering) | 8662 |
| Edges (whole repo, CodeGraph does not support scope filtering) | 23120 |
| DB size (bytes, whole repo) | 30298112 |
| Index time (s) | 2.592 |

## Non-comparable fields (not filled in, not compared)

CodeGraph has no `--scope`-equivalent filter, so its whole-repo `nodes` /
`edges` / `db_size_bytes` counters are **not directly comparable** to
RepoWeaver's scope-filtered `nodes` / `edges_total` in
`v0.2.0-gson-core.json` — one denominator is the whole gson repo, the other
is `gson/src/main/` only. Do not diff these two numbers as if they measured
the same thing.

CodeGraph also has no published, independently-verifiable definition for
`ambiguous_edge_rate`, `edge_precision`/`edge_recall` against a ground-truth
fixture, or `deterministic_rebuild` — these are left `null` rather than
guessed or assumed from its README.

## The one apples-to-apples comparison point

Both tools define coverage the same way: share of symbol-bearing files with
at least one resolved cross-file incoming edge, over the same scope
(`gson/src/main/`, 81 symbol-bearing files) at the same pinned commit.

| Tool | Scope | Covered / Total | Coverage |
|---|---|---|---|
| CodeGraph 1.5.0 | gson/src/main/ | 75 / 81 | 0.9259 |
| RepoWeaver (this candidate) | gson/src/main/ | 73 / 81 | 0.9012 |

RepoWeaver trails CodeGraph by **2.47 percentage points** on this one
directly-comparable metric, on the same scope and commit. RepoWeaver's own
release gates (`ambiguous_edge_rate<=0.10`, `cross_file_dependent_coverage>=0.90`,
fixture precision/recall, deterministic rebuild) all PASS on this same run —
see `v0.2.0-gson-core.md` — but that does not mean RepoWeaver has caught up
to or surpassed CodeGraph overall; it means RepoWeaver clears its own bar
while still measurably behind on coverage.
