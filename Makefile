.DEFAULT_GOAL := help

.PHONY: help setup generate-api check-api format format-check lint typecheck test build check up down logs smoke browser-e2e live-providers live-quality clean

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
	uv run --project backend ruff format backend scripts
	npm run format --prefix frontend

format-check: ## Check formatting without changing files
	uv run --project backend ruff format --check backend scripts
	npm run format:check --prefix frontend

lint: ## Run backend and frontend linters
	uv run --project backend ruff check backend scripts
	npm run lint --prefix frontend

typecheck: ## Run strict backend and frontend type checks
	uv run --project backend mypy backend/src backend/tests
	uv run --project backend mypy --strict scripts
	npm run typecheck --prefix frontend
	npm run typecheck:e2e --prefix frontend

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

smoke: ## Run credential-free HTTP and concurrency checks
	@set -e; \
		trap 'docker compose down --volumes --remove-orphans' EXIT; \
		GEMINI_API_KEY='' TEST_SUMMARIZER_ENABLED=true docker compose up --build --detach --wait; \
		./scripts/smoke_test.sh; \
		docker compose cp scripts/generate_sample_pdf.py api:/app/generate_sample_pdf.py; \
		docker compose cp scripts/concurrency_smoke_test.py api:/app/concurrency_smoke_test.py; \
		docker compose exec -T --env APP_URL=http://127.0.0.1:8000 api python /app/concurrency_smoke_test.py

browser-e2e: ## Run the browser workflow against the credential-free stack
	@set -e; \
		trap 'docker compose down --volumes --remove-orphans' EXIT; \
		frontend/node_modules/.bin/playwright install chromium; \
		TEST_SUMMARIZER_ENABLED=true docker compose up --build --detach --wait; \
		python3 scripts/generate_sample_pdf.py .tmp/playwright/sample.pdf >/dev/null; \
		npm run test:e2e --prefix frontend

live-providers: ## Exercise live Gemini PDF extraction and summarization
	@set -e; \
		if [ ! -f .env ]; then \
			echo "Create an untracked .env with GEMINI_API_KEY first" >&2; \
			exit 1; \
		fi; \
		trap 'docker compose down --volumes --remove-orphans' EXIT; \
		TEST_SUMMARIZER_ENABLED=false docker compose up --build --detach --wait; \
		./scripts/live_provider_test.sh

live-quality: ## Check one live Gemini job in a runner-owned disposable stack
	uv run --project backend python scripts/live_quality_test.py

clean: ## Remove generated build and test artifacts
	rm -rf backend/dist frontend/dist .tmp .pytest_cache backend/.pytest_cache .mypy_cache backend/.mypy_cache .ruff_cache backend/.ruff_cache htmlcov .coverage
