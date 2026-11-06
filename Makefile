PYTHON ?= python3

.PHONY: install format lint typecheck test build check

install:
	$(PYTHON) -m pip install -e '.[dev]'

format:
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

typecheck:
	$(PYTHON) -m mypy

test:
	$(PYTHON) -m pytest --cov --cov-report=term-missing

build:
	$(PYTHON) -m build --no-isolation
	$(PYTHON) -m twine check --strict dist/*

check: lint typecheck test build
