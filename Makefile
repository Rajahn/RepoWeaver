.PHONY: verify-p0 verify-m1 verify-m2 verify-m3 verify-query verify-benchmark verify-perf ci baseline install

install:
	uv sync --extra dev

verify-p0: install
	uv run pytest tests/ -v
	uv run fabric --help > /dev/null
	uv run python scripts/check_public.py

verify-m1: install
	uv run ruff check src tests scripts
	uv run pytest tests/ -v
	uv run fabric verify --level m1
	uv run python scripts/check_public.py

verify-m2: install
	uv run fabric verify --level m2

verify-m3: install
	uv run fabric verify --level m3

verify-query: install
	uv run fabric verify --level query

verify-benchmark: install
	uv run fabric verify --level benchmark

verify-perf: install
	uv run fabric verify --level perf

ci: verify-m1 verify-m2 verify-m3 verify-query verify-benchmark verify-perf

baseline: verify-benchmark
