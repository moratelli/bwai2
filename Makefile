.DEFAULT_GOAL := help

.PHONY: help setup format format-check lint typecheck test build check up down clean

help: ## Show available commands
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target>\n\n"} /^[a-zA-Z_-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install locked backend and frontend dependencies
	uv sync --project backend --all-groups --locked
	npm ci --prefix frontend

format: ## Format Python and frontend files
	uv run --project backend ruff format backend
	npm run format --prefix frontend

format-check: ## Check formatting without changing files
	uv run --project backend ruff format --check backend
	npm run format:check --prefix frontend

lint: ## Run backend and frontend linters
	uv run --project backend ruff check backend
	npm run lint --prefix frontend

typecheck: ## Run strict backend and frontend type checks
	uv run --project backend mypy backend/src
	npm run typecheck --prefix frontend

test: ## Run backend and frontend tests once
	uv run --project backend pytest backend/tests
	npm test --prefix frontend

build: ## Build backend package and frontend assets
	uv build --project backend
	npm run build --prefix frontend

check: format-check lint typecheck test build ## Run every local quality gate

up: ## Build and start the complete application
	docker compose up --build

down: ## Stop the application and remove its containers
	docker compose down --remove-orphans

clean: ## Remove generated build and test artifacts
	rm -rf backend/dist frontend/dist .pytest_cache backend/.pytest_cache .mypy_cache backend/.mypy_cache .ruff_cache backend/.ruff_cache htmlcov .coverage
