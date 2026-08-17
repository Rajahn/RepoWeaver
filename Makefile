.PHONY: verify-p0 verify-m1 ci baseline install

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

ci: verify-m1

baseline:
	@echo "Benchmark baseline — coming in T0.1"
