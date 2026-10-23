# Tasks

## 1. Establish the monorepo toolchain

- [x] 1.1 Create the Python 3.12 backend package, uv dependency groups, Ruff/mypy/pytest configuration, `.python-version`, and `uv.lock`; verify `uv sync --project backend --all-groups --locked` succeeds.
- [x] 1.2 Create the strict TypeScript React/Vite/SWC frontend with pinned Oxfmt, Oxlint, TypeScript, and Vitest scripts plus `package-lock.json`; verify `npm ci --prefix frontend` and the empty production build succeed.
- [x] 1.3 Add repository ignores, `.env.example`, and a root Makefile whose setup, format, lint, typecheck, test, build, check, up, down, and clean targets delegate to locked project commands; verify a dry run shows no global pip or npm dependency installation.

## 2. Implement document submission test-first

- [x] 2.1 Write failing domain and service tests for document states, parser identifiers, batch limits, byte limits, media type, PDF signature, provider availability, and atomic acceptance; implement typed models, configuration, protocols, and the submission service until `uv run --project backend pytest backend/tests/unit/test_submission.py` passes.
- [x] 2.2 Write failing ASGI tests for `POST /api/v1/documents`, including multi-file HTTP 202 responses and 413/422/503 error contracts; implement the FastAPI app, dependency wiring, schemas, safe error envelope, and thin upload route until the focused API tests pass.
- [x] 2.3 Add submission API examples and limit/configuration notes to the README while the contract is fresh; verify each documented curl field and parser value matches the OpenAPI schema.

## 3. Add Redis persistence and Streams behavior test-first

- [x] 3.1 Write failing repository tests for queued metadata, source bytes, 24-hour expiry, status reads, atomic completed/failed writes, raw-source deletion, and lock ownership; implement the async Redis document repository until the focused tests pass.
- [x] 3.2 Write failing queue tests for transactional batch publication, consumer-group creation, new reads, acknowledgement, pending-entry reclaim, and terminal redelivery; implement Redis Streams operations until the focused tests pass.
- [x] 3.3 Add a Redis-backed integration test that submits a batch and observes stream entries and retrievable queued documents; verify it passes against Redis 7 without external provider calls.

## 4. Implement parsers and summarization test-first

- [x] 4.1 Write failing PyPDF tests using generated text, blank-page, corrupt, and encrypted fixtures; implement page-preserving extraction in a worker thread until `uv run --project backend pytest backend/tests/unit/test_pypdf_parser.py` passes.
- [ ] 4.2 Write failing Gemini adapter tests for inline PDF input, schema-valid ordered pages, malformed/duplicate page indexes, safe provider errors, and configurable `gemini-3.1-flash-lite`; implement async Gemini extraction behind the parser protocol with all SDK calls mocked.
- [ ] 4.3 Write failing Mistral adapter tests for base64 PDF input, `mistral-ocr-latest` page normalization, empty pages, and safe provider errors; implement the synchronous SDK boundary in a worker thread with all SDK calls mocked.
- [ ] 4.4 Write failing summarizer tests for single-request, page-boundary chunking, final synthesis, empty extraction, and safe retry classification; implement Gemini Markdown summarization until the focused tests pass.
- [ ] 4.5 Document parser accuracy, OCR, latency, credential, and cost trade-offs plus PyPDF's scanned-page limitation; verify the README and UI copy use the same parser names and defaults.

## 5. Implement the resilient worker test-first

- [ ] 5.1 Write failing processing-service tests for queued-to-processing-to-completed state, parser dispatch, one summary, atomic terminal publication, and source deletion; implement the processing use case until the focused tests pass.
- [ ] 5.2 Write failing worker tests for retryable backoff, three-attempt exhaustion, permanent failure, stale pending recovery, processing locks, terminal redelivery, acknowledgement order, and graceful cancellation; implement the consumer loop until the focused worker tests pass.
- [ ] 5.3 Add structured, content-safe logging tests and worker operation notes; verify logs include document identifiers and state changes but omit PDF text, provider payloads, and credentials.

## 6. Implement result and health APIs test-first

- [ ] 6.1 Write failing ASGI tests for queued, processing, completed, failed, unknown, and expired `GET /api/v1/documents/{document_id}` responses; implement discriminated response models and the result route until the focused tests pass.
- [ ] 6.2 Write failing liveness/readiness tests for healthy Redis, unavailable Redis, and missing Gemini configuration; implement `/health/live` and `/health/ready` until the focused tests pass without exposing secret values.
- [ ] 6.3 Regenerate and inspect the OpenAPI document; verify upload, status, terminal result, health, and safe error schemas match the capability specs.

## 7. Build the focused React interface test-first

- [ ] 7.1 Write failing Vitest tests for the typed API client and polling state machine, including accepted batches, independent terminal states, temporary poll failures, and cleanup; implement the client and polling hook until `npm test --prefix frontend` passes in run mode.
- [ ] 7.2 Write failing component tests for the default Fast text/PyPDF mode, intent-based parser labels and provider guidance, file validation, upload errors, status announcements, completed summaries/pages, independent failures, keyboard use, non-executable raw HTML, and the no-text OCR resubmission recommendation without automatic fallback; implement the single-page interface until the focused tests pass.
- [ ] 7.3 Finish responsive styling, loading/empty/focus/error states, and concise UI copy; verify the production build and a keyboard-only manual pass at narrow and desktop widths.

## 8. Containerize and document the runnable stack

- [ ] 8.1 Write health-aware backend and frontend Dockerfiles plus Nginx proxy configuration; verify both multi-stage images build without copying credentials, virtual environments, caches, or `node_modules`.
- [ ] 8.2 Add `compose.yaml` for Redis 7+, API, worker, and frontend with localhost binding, health checks, restart behavior, named Redis storage, and runtime-only provider variables; verify locally that each image runs against Colima and verify `docker compose config` in CI without embedding secret values.
- [ ] 8.3 Add a generated sample PDF and an end-to-end PyPDF smoke script with a test-only summarizer boundary; verify in CI that one Compose command starts the stack, the browser URL becomes healthy, upload returns HTTP 202, the worker consumes the stream, polling reaches a terminal result, and the stack shuts down cleanly.
- [ ] 8.4 Rewrite the README as the reviewer guide covering setup, credentials, Compose, Make targets, architecture, Redis data flow, curl demo, frontend use, tests, limitations, and optional live-provider checks; execute every non-credential command shown in the guide.

## 9. Add continuous integration and delivery

- [ ] 9.1 Add GitHub Actions CI for uv-locked Python checks, npm-locked frontend checks, and Compose image validation on pull requests to `main` and pushes to `dev` or `main`; verify workflow syntax and local commands match `make check`.
- [ ] 9.2 Add a least-privilege GHCR delivery workflow for successful `main` updates that publishes commit-tagged backend and frontend images but never publishes from pull requests; verify permissions, tags, cache configuration, and secret-free build inputs by inspection and workflow validation.

## 10. Run integrated quality gates

- [ ] 10.1 Run `make check` from a clean dependency state and fix all Ruff format/check, mypy, pytest, Oxfmt, Oxlint, TypeScript, Vitest, and frontend build failures.
- [ ] 10.2 Build and smoke-test the individual Redis, API, worker, and frontend containers locally with Colima; run the complete Compose health and PyPDF end-to-end smoke checks in CI, inspect logs for secret/content leakage, and verify clean shutdown.
- [ ] 10.3 If local Gemini and Mistral credentials are available, run one opt-in sample through each external parser without recording keys or provider payloads; otherwise record both checks as not run rather than weakening mocked coverage.
- [ ] 10.4 Create the source archive from tracked files, excluding `.git`, `.venv`, `node_modules`, caches, local environment files, and generated build output; inspect the archive listing and verify it expands into a runnable repository.

## Workflow follow-up

- Run the installed `openspec-verify-change` workflow and strict OpenSpec validation after all tracked tasks pass.
- Run the risk-tiered final implementation review and resolve any demonstrated critical or high finding within the allowed fix wave.
- Archive `build-async-document-processor`, open a conventional-commit pull request from `dev` to `main`, squash merge after required checks, and verify delivery workflow status.
