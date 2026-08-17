"""`fabric benchmark run` orchestration: build the index once via an adapter,
optionally score it against ground_truth.yaml, and return a JSON-serializable
result dict. No release-gate judgement happens here — that's `compare`'s job."""

from __future__ import annotations

import tempfile
from pathlib import Path

from repoweaver.benchmark.adapters import AdapterSkipped, build_adapter
from repoweaver.benchmark.groundtruth import GroundTruth, evaluate
from repoweaver.graph.store import GraphStore
from repoweaver.indexer import Indexer


def run_benchmark(
    repo: Path,
    name: str,
    adapter: str = "repoweaver",
    ground_truth: Path | None = None,
) -> dict:
    repo_root = Path(repo).resolve()

    with tempfile.TemporaryDirectory(prefix="repoweaver-benchmark-") as tmp:
        workdir = Path(tmp)
        try:
            metrics = build_adapter(adapter).run(repo_root, name, workdir)
        except AdapterSkipped as exc:
            return {
                "name": name,
                "repo": str(repo_root),
                "adapter": adapter,
                "status": "SKIP",
                "reason": str(exc),
            }

        result = metrics.to_dict()

        if ground_truth is not None:
            gt = GroundTruth.load(ground_truth)
            # `fixture` in ground_truth.yaml is repo-root-relative (i.e. relative to
            # the current working directory a `fabric` invocation runs from), not
            # relative to the yaml file's own directory.
            gt_repo = Path(gt.fixture).resolve()
            db_path = workdir / "ground_truth.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with GraphStore(db_path) as store:
                Indexer(gt_repo, store).build()
                result["correctness"] = evaluate(gt, store)

        result["status"] = "MEASURED"
        return result


__all__ = ["run_benchmark"]
