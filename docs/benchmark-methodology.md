# Benchmark methodology

RepoWeaver treats SOTA alignment as a release gate, not a marketing claim.
Every metric is generated from a pinned public repository or a checked-in
fixture, and every missing required metric blocks release.

## Why coverage is paired with ambiguity and precision

A graph can make coverage look perfect by connecting every unresolved call to
every same-named method. Such a graph is useless to an agent: it has high
recall but near-zero precision.

RepoWeaver therefore calls an edge **resolved** only when:

1. `confidence >= 0.5`; and
2. `ambiguous_candidates == []`.

`cross_file_dependent_coverage` counts a symbol-bearing file only when at least
one resolved edge reaches a node in that file from another file. When scope
prefixes are supplied, **both source and target files** must be in scope. This
prevents test-only dependents from inflating a production-source benchmark.
Coverage must always be read together with:

- `ambiguous_edge_rate`;
- fixture `edge_precision` and `edge_recall`;
- `deterministic_rebuild`.

The hard gates live in `benchmarks/sota-targets.yaml`. A required metric that is
missing or `null` is `SKIP` at the individual-gate level but makes the overall
comparison **FAIL**.

## Metrics

| Metric | Definition |
|---|---|
| `parse_error_rate` | Java files whose independent tree-sitter root has `has_error`, divided by Java files scanned |
| `ambiguous_edge_rate` | Edges with non-empty candidate lists divided by all edges |
| `cross_file_dependent_coverage` | Symbol-bearing files with at least one resolved incoming cross-file edge divided by symbol-bearing files |
| `node_recall` | Expected fixture nodes found with matching kind and qualified name |
| `edge_precision` | Correct resolved fixture edges divided by all resolved in-scope fixture edges |
| `edge_recall` | Correct resolved fixture edges divided by expected resolvable fixture edges |
| `query_topk_recall` / `query_mrr` | Retrieval correctness against checked-in query expectations |
| `deterministic_rebuild` | Two clean builds produce the same canonical node/edge hash |
| query latency p50/p95 | SearchEngine retrieval latency over a deterministic symbol query set |
| context tokens p50/p95 | Estimated verbatim source context returned under a 4,000-token budget |

Volatile timestamps, database row order and local absolute paths are excluded
from the deterministic graph signature.

## Public repositories

`benchmarks/repos.yaml` pins exact commits for Gson, Spring PetClinic, OkHttp
and MyBatis. Large repositories are not cloned in normal CI; scheduled or local
benchmark runs clone the pinned commits and store JSON reports under
`benchmarks/baselines/`.

The checked-in `gt_demo` fixture runs in every CI build and validates metric
semantics, correctness scoring and the gate mechanism without network access.

## SOTA references

CodeGraph publishes 93.3% Java cross-file coverage on Gson. We also ran
CodeGraph 1.5.0 and RepoWeaver 0.2.0 on pinned commit `dae37cf…` with the same
`gson/src/main/` scope and both endpoints constrained to that scope. Measured
coverage was 92.59% vs 90.12%. RepoWeaver additionally gates ambiguity at
<=10% (measured 6.36%) and fixture precision/recall. The published 93.3% remains
a directional historical reference; the checked-in same-commit reports are the
operational comparison.

## Adapters

- `repoweaver`: full metrics.
- `grep`: query latency and text-volume proxy; no fabricated graph metrics.
- `codegraph` / `graft`: optional presence detection. Use an explicit external
  command adapter to run locally installed tools.
- `external:<command>`: command receives `{repo}` and `{workdir}` and must print
  one JSON object matching known metric fields.

RepoWeaver never vendors or copies third-party implementations.

## Commands

```bash
fabric benchmark run \
  --repo /path/to/pinned/repo \
  --name gson-candidate \
  --scope-prefix gson/src/main/ \
  --output results/gson.json

fabric benchmark compare \
  --candidate results/gson.json \
  --target benchmarks/sota-targets.yaml

fabric benchmark report \
  --input results/gson.json \
  --targets benchmarks/sota-targets.yaml \
  --output results/gson.md

fabric verify --level benchmark
```
