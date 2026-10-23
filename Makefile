.DEFAULT_GOAL := help

.PHONY: help setup generate-api check-api format format-check lint typecheck test build check up down logs smoke clean

help: ## Show available commands
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target>\n\n"} /^[a-zA-Z_-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install locked backend and frontend dependencies
	uv sync --project backend --all-groups --locked
	npm ci --prefix frontend

generate-api: ## Export OpenAPI and generate frontend transport types
	uv run --project backend python backend/scripts/export_openapi.py frontend/openapi.json
	npm run generate:api --prefix frontend

check-api: ## Verify generated API artifacts are current
	@set -e; \
		rm -rf .tmp/openapi-check; \
		mkdir -p .tmp/openapi-check; \
		trap 'rm -rf .tmp/openapi-check' EXIT; \
		uv run --project backend python backend/scripts/export_openapi.py .tmp/openapi-check/openapi.json; \
		frontend/node_modules/.bin/openapi-typescript .tmp/openapi-check/openapi.json -o .tmp/openapi-check/api.ts >/dev/null; \
		frontend/node_modules/.bin/oxfmt .tmp/openapi-check/openapi.json .tmp/openapi-check/api.ts >/dev/null; \
		cmp frontend/openapi.json .tmp/openapi-check/openapi.json; \
		cmp frontend/src/generated/api.ts .tmp/openapi-check/api.ts

format: ## Format Python and frontend files
	uv run --project backend ruff format backend scripts/generate_sample_pdf.py
	npm run format --prefix frontend

format-check: ## Check formatting without changing files
	uv run --project backend ruff format --check backend scripts/generate_sample_pdf.py
	npm run format:check --prefix frontend

lint: ## Run backend and frontend linters
	uv run --project backend ruff check backend scripts/generate_sample_pdf.py
	npm run lint --prefix frontend

typecheck: ## Run strict backend and frontend type checks
	uv run --project backend mypy backend/src backend/tests
	npm run typecheck --prefix frontend

test: ## Run backend and frontend tests once
	uv run --project backend pytest backend/tests
	npm test --prefix frontend

build: ## Build backend package and frontend assets
	uv build --project backend
	npm run build --prefix frontend

check: check-api format-check lint typecheck test build ## Run every local quality gate

up: ## Build and start the complete application
	docker compose up --build --detach --wait

down: ## Stop the application and remove its containers
	docker compose down --remove-orphans

logs: ## Show recent API and worker logs
	docker compose logs api worker

smoke: ## Run the credential-free Compose smoke test
	@set -e; \
		trap 'docker compose down --volumes --remove-orphans' EXIT; \
		TEST_SUMMARIZER_ENABLED=true docker compose up --build --detach --wait; \
		./scripts/smoke_test.sh

clean: ## Remove generated build and test artifacts
	rm -rf backend/dist frontend/dist .tmp .pytest_cache backend/.pytest_cache .mypy_cache backend/.mypy_cache .ruff_cache backend/.ruff_cache htmlcov .coverage
