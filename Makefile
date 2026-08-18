.PHONY: verify-p0 verify-m1 verify-m2 verify-m3 verify-benchmark ci baseline install

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

verify-benchmark: install
	uv run fabric verify --level benchmark

ci: verify-m1 verify-m2 verify-m3 verify-benchmark

baseline: verify-benchmark
