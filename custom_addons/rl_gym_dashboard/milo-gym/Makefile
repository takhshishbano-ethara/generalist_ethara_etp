.PHONY: lint format typecheck test test-fast clean install all

lint:
	ruff check src/ tests/ scripts/

format:
	ruff format src/ tests/ scripts/

typecheck:
	mypy src/

test:
	pytest tests/ -v

test-fast:
	pytest tests/ -v -x --tb=short

clean:
	rm -rf __pycache__ .mypy_cache .pytest_cache dist/ .ruff_cache

install:
	pip install -e ".[dev]"

all: lint typecheck test
