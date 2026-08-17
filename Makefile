.PHONY: verify-p0 ci baseline install

install:
	pip install -e ".[dev]"

verify-p0: install
	pytest tests/ -v
	fabric --help > /dev/null

ci: install
	ruff check src/ tests/ || true
	pytest tests/ -v

baseline:
	@echo "Benchmark baseline — coming in T0.1"
