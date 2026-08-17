# RepoWeaver Benchmarks

This directory contains benchmark cases and tooling used to validate
RepoWeaver's call-graph accuracy and retrieval quality.

## Structure

```
benchmarks/
├── README.md       — this file
└── cases.yaml      — benchmark repository list and test cases
```

## Benchmark Repos

Four public Java repositories are used as ground truth:

| Repo | Purpose |
|---|---|
| [spring-petclinic](https://github.com/spring-projects/spring-petclinic) | Spring DI-heavy; tests injection-aware edges |
| [gson](https://github.com/google/gson) | Comparable to CodeGraph published 93.3 % Java coverage |
| [okhttp](https://github.com/square/okhttp) | Call-chain depth |
| [mybatis-3](https://github.com/mybatis/mybatis-3) | Generated-code noise filtering |

## Running Baselines

```bash
make baseline
```

Baseline population is planned for milestone **T0.1**.

## Metrics

| Metric | Target (T0.1) | Notes |
|---|---|---|
| Edge precision | ≥ 0.90 | Static call edges only |
| Edge recall | ≥ 0.85 | Excluding DI/reflection |
| FTS hit@5 | ≥ 0.80 | BM25 + PageRank re-rank |
| Index latency | ≤ 60 s | For gson (~10 k methods) |
