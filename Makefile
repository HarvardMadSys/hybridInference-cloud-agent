.PHONY: help setup-dev format lint typecheck test test-db test-all check docs ci ci-backend ci-database ci-frontend clean

.DEFAULT_GOAL := help

# Allow overriding uv run flags, e.g.:
#   make lint UV_RUN="uv run --active"
UV_RUN ?= uv run
PYTHON_VERSION ?= 3.12
# Match CI's execution mode; use PYTEST_WORKERS=0 for a serial debugging run.
PYTEST_WORKERS ?= 4

RESET := \033[0m
BOLD := \033[1m
GREEN := \033[32m
YELLOW := \033[33m
BLUE := \033[34m

help:  ## Show this help message
	@echo "$(BOLD)Available targets:$(RESET)"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(BLUE)%-12s$(RESET) %s\n", $$1, $$2}'

setup-dev:  ## Create the virtualenv and install dependencies
	uv venv -p $(PYTHON_VERSION)
	uv sync
	@echo "$(GREEN)OK Dev environment ready (activate with: source .venv/bin/activate)$(RESET)"

format:  ## Format code with ruff
	@echo "$(YELLOW)Running formatter...$(RESET)"
	$(UV_RUN) ruff format .
	$(UV_RUN) ruff check --fix .
	@echo "$(GREEN)OK Code formatted$(RESET)"

lint:  ## Run linters (ruff format check, ruff lint, pydocstyle)
	@echo "$(YELLOW)Running linters...$(RESET)"
	$(UV_RUN) ruff format --check .
	$(UV_RUN) ruff check --no-fix .
	$(UV_RUN) pydocstyle apps/backend/cloud_agent apps/backend/cloud_agent_host apps/backend/cloud_agent_shared
	@echo "$(GREEN)OK Linting passed$(RESET)"

typecheck:  ## Type-check all three packages (mypy; config in pyproject.toml)
	@echo "$(YELLOW)Running mypy...$(RESET)"
	$(UV_RUN) mypy
	@echo "$(GREEN)OK Type check passed$(RESET)"

test:  ## Run tests (exclude external and db-dependent)
	@echo "$(YELLOW)Running tests (not external, not dbtest)...$(RESET)"
	$(UV_RUN) pytest -n $(PYTEST_WORKERS) --dist loadfile -q -m "not external and not dbtest"
	@echo "$(GREEN)OK Tests passed$(RESET)"

test-db:  ## Run the dbtest tier (needs Postgres; see TEST_DB_* in ENVIRONMENT.md)
	$(UV_RUN) pytest -vv -m "dbtest"

test-all:  ## Run all tests except external (includes db-dependent)
	$(UV_RUN) pytest -q -m "not external"

check: format lint typecheck test  ## Format, lint, type-check, and test — the gate every task must pass

# Warnings are errors, and --keep-going reports every one instead of stopping
# at the first. CI's Docs Build job runs this target, so a build that is clean
# here is clean there.
docs:  ## Build the developer documentation into docs/build/html
	$(UV_RUN) sphinx-build -b html -W --keep-going docs/developer docs/build/html
	@echo "$(GREEN)OK Docs built: docs/build/html/index.html$(RESET)"

ci:  ## Run all local CI tiers (check-only; needs uv, Python 3.12, Node 22, and Docker)
	./scripts/ci-local.sh

ci-backend:  ## Run local backend CI (lint, mypy, and non-external/non-db tests)
	./scripts/ci-local.sh backend

ci-database:  ## Run local database CI against disposable PostgreSQL 15
	./scripts/ci-local.sh database

ci-frontend:  ## Run local frontend CI (lint, format check, types, tests, and build guards)
	./scripts/ci-local.sh frontend

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage coverage.xml htmlcov build dist *.egg-info apps/backend/*.egg-info docs/build
	find . -type d \( -name .venv -o -name venv -o -name node_modules \) -prune -o -type d -name __pycache__ -exec rm -rf {} +
