.PHONY: help setup-dev format lint test test-db test-all check clean

.DEFAULT_GOAL := help

# Allow overriding uv run flags, e.g.:
#   make lint UV_RUN="uv run --active"
UV_RUN ?= uv run
PYTHON_VERSION ?= 3.12

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
	$(UV_RUN) pydocstyle cloud_agent cloud_agent_host cloud_agent_contracts cloud_agent_git
	@echo "$(GREEN)OK Linting passed$(RESET)"

typecheck:  ## Type-check both packages (mypy; config in pyproject.toml)
	@echo "$(YELLOW)Running mypy...$(RESET)"
	$(UV_RUN) mypy
	@echo "$(GREEN)OK Type check passed$(RESET)"

test:  ## Run tests (exclude external and db-dependent)
	@echo "$(YELLOW)Running tests (not external, not dbtest)...$(RESET)"
	$(UV_RUN) pytest -q -m "not external and not dbtest"
	@echo "$(GREEN)OK Tests passed$(RESET)"

test-db:  ## Run the dbtest tier (needs Postgres; see TEST_DB_* in ENVIRONMENT.md)
	$(UV_RUN) pytest -vv -m "dbtest"

test-all:  ## Run all tests except external (includes db-dependent)
	$(UV_RUN) pytest -q -m "not external"

check: format lint typecheck test  ## Format, lint, type-check, and test — the gate every task must pass

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov build dist *.egg-info
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +
