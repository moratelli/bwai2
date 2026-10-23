# Design

## Context

This is a greenfield repository with only a README and OpenSpec scaffolding. The requested system spans an HTTP API, a queue-backed worker, two external AI providers, a browser client, and local container orchestration. The implementation must remain credible within a two-hour take-home timebox, so the design favors explicit boundaries and small modules over deployment abstractions.

The external interfaces used by the design were checked against current vendor documentation:

- Google Gen AI's Python SDK accepts inline byte parts, exposes async model methods, and supports schema-constrained JSON responses. The requested `gemini-3.1-flash-lite` identifier appears in Google's model documentation.
- Mistral's OCR API accepts base64 PDF data URLs and returns page-level Markdown with `mistral-ocr-latest`.
- redis-py exposes async Redis Streams consumer-group commands.
- PyPDF extracts text per page but does not OCR image-only pages.

## Goals / non-goals

**Goals:**

- Keep HTTP request handling independent from provider latency.
- Make parser and provider code replaceable and testable without live API calls.
- Preserve page boundaries and make all state transitions observable.
- Run the same pinned checks from the Makefile and GitHub Actions.
- Provide a polished, focused interface rather than a broad application shell.

**Non-goals:**

- Authentication, multi-tenancy, permanent document storage, and public internet deployment.
- AWS infrastructure, horizontal autoscaling, or production observability services.
- Perfect visual reconstruction of PDF layout.
- Guaranteed extraction from corrupt, encrypted, or oversized PDFs.
- Live-provider calls in automated tests.

## Decisions

### 1. Use a small monorepo with one backend image

The repository will contain `backend/`, `frontend/`, `compose.yaml`, and a root `Makefile`. The backend package will expose two entry points from one image: FastAPI for request handling and a worker process for stream consumption. The frontend will build to static assets served by Nginx, which will proxy `/api` and health requests to FastAPI. Reviewers reach the complete application through one localhost URL.

This avoids duplicated Python dependencies and cross-origin production configuration. Separate backend images were rejected because the API and worker share nearly all code. Serving the frontend from FastAPI was rejected because it couples Node builds to the Python runtime image and makes local frontend development less direct.

### 2. Keep domain and provider boundaries explicit

The backend will use a compact layered package:

- `api/` contains routers, request validation, response models, and dependency wiring.
- `domain/` contains document states, parser identifiers, page results, errors, and transition rules.
- `services/` contains submission and processing use cases.
- `infrastructure/` contains Redis persistence/queue code and parser/summarizer adapters.
- `worker.py` owns the consumer loop, retry policy, stale-entry recovery, and shutdown.

Routes remain thin. Services depend on typed protocols for the document store, queue, parsers, and summarizer. Tests can therefore use deterministic fakes without patching route internals.

A flatter single-module backend was considered, but it would mix provider handling, Redis commands, and HTTP behavior in the same code path. A larger clean-architecture template was also rejected as unnecessary for this scope.

### 3. Store job state and transient PDF bytes in Redis

Redis will hold:

- `document:{id}`: a hash containing status, parser, safe filename, timestamps, attempt count, terminal error fields, serialized pages, and summary.
- `document:{id}:source`: the raw PDF bytes, deleted at terminal state.
- `documents:processing`: a stream containing document identifiers only.
- `document-workers`: the stream consumer group.
- `document:{id}:lock`: a short-lived ownership token that prevents concurrent duplicate processing.

All document keys receive a 24-hour expiry when created. A transactional pipeline persists an entire accepted batch and appends its stream entries so the API never reports half-created jobs. Terminal updates write the complete result or safe error, delete source bytes, refresh the remaining expiry, and only then acknowledge the stream entry.

Putting raw bytes in Redis is acceptable for the 20 MiB per-file take-home limit and keeps Compose self-contained. Object storage would be the production choice, but adding S3 or MinIO would increase setup and failure modes without improving the requested demonstration.

### 4. Use consumer-group delivery with terminal idempotency and recovery

The worker creates the consumer group idempotently, reads new entries with `XREADGROUP`, and periodically reclaims stale pending entries with `XAUTOCLAIM`. Before provider work, it checks terminal state and acquires the per-document lock. Redelivery of a terminal document is acknowledged without another provider call. Lock ownership prevents two workers from publishing conflicting results.

Retryable provider rate limits, timeouts, and server failures use exponential backoff with jitter for up to three total attempts. Corrupt or encrypted PDFs, schema-invalid permanent responses, unsupported documents, and empty extraction are terminal. The worker acknowledges only after a terminal state is durable.

A simple Redis list was rejected because the task explicitly requires Streams and because lists do not provide a pending-entry list. A separate scheduler or dead-letter service was rejected for the timebox; failed state in the document record is sufficient for this demo.

### 5. Normalize every parser to ordered page results

Each parser implements an async protocol returning `list[PageContent]`:

- PyPDF runs blocking parsing in `asyncio.to_thread`, iterates every source page, and emits escaped Markdown-compatible plain text. Empty pages are preserved.
- Gemini sends inline PDF bytes with an extraction prompt and a Pydantic-derived response schema. The adapter validates page indexes, uniqueness, order, and Markdown strings before returning them.
- Mistral sends a base64 PDF data URL to `mistral-ocr-latest`. Because the current Python OCR call is synchronous, the adapter runs it in `asyncio.to_thread`, then normalizes returned page indexes and Markdown.

Provider adapters convert SDK exceptions into the project's retryable or permanent error types. They never expose provider response bodies to the API.

Sending provider-hosted file uploads was considered, but inline bytes avoid a second remote-file lifecycle for these bounded inputs. PyPDF remains the fastest and cheapest option for digital PDFs; Mistral is the dedicated OCR path; Gemini offers richer Markdown extraction but adds generative variability.

### 6. Summarize extracted content with bounded page groups

`gemini-3.1-flash-lite` will be the configurable default for both Gemini extraction and all summaries. The summarizer joins page content with explicit page markers. Small documents use one request. Large documents are split only at page boundaries into bounded character groups; Gemini summarizes each group and synthesizes those summaries into one final Markdown result.

Character bounds are an operational guard, not an estimate of exact model tokens. They make memory and request sizes predictable without adding a tokenizer tied to one provider model. Empty documents fail before a summary request.

A single unbounded summary request was rejected because extracted text can be much larger than the PDF byte size suggests. Truncation was rejected because it silently discards later pages.

### 7. Expose a status-oriented REST API

The initial endpoints will be:

- `POST /api/v1/documents` with multipart `files` and one `parser` field.
- `GET /api/v1/documents/{document_id}` for polling and terminal results.
- `GET /health/live` for process liveness.
- `GET /health/ready` for Redis and required configuration readiness.

The upload response returns one opaque UUID per file. Status responses use a discriminated shape for `queued`, `processing`, `completed`, and `failed` so clients cannot confuse partial content with a terminal result. Unknown and expired identifiers return 404.

Server-Sent Events and WebSockets were rejected because polling is enough for multi-second background jobs and is easier to recover after refresh or temporary network loss.

### 8. Validate files in bounded reads and render generated content defensively

The API accepts at most ten files and 20 MiB per file. It checks the media type and PDF signature, reads each upload in chunks with a hard byte limit, and never uses the client filename as a path. Nginx will enforce a compatible aggregate request limit as an outer guard. Corrupt and encrypted files fail safely during parsing.

The app is an unauthenticated local demonstration. Compose binds the browser entry point to localhost; the design does not present opaque UUIDs as authorization. A production deployment would require identity, object-level authorization, rate limiting, durable encrypted storage, and a retention policy review.

The browser uses a Markdown renderer with raw HTML disabled. Provider credentials exist only in backend runtime environment variables. Logs include document and request identifiers but not raw content, provider payloads, or credentials.

### 9. Poll from a focused React interface

The Vite/React/SWC frontend will provide a responsive upload panel, intent-based processing mode cards, selected-file review, and one result card per document. Fast text (`pypdf`) is selected by default for normal digital PDFs; Rich Markdown (`gemini`) and Scanned document / OCR (`mistral`) expose their provider, credential, privacy, quality, and cost implications as secondary guidance. The selected API parser is always honored: the application will not silently fall back to another provider.

It polls active documents with a bounded interval and backs off on temporary failures. Completed cards show the summary before collapsible page content; failed cards preserve other successful results. If PyPDF produces no text, the failure recommends resubmission with the OCR mode while keeping that choice with the user.

The interface will use semantic form controls, visible focus styles, an ARIA live status region, clear empty/loading/error states, and a restrained visual system. It will not add routing, accounts, automatic parser detection, or a component framework.

### 10. Make the repository toolchain the CI contract

The backend will use a Python 3.12 `pyproject.toml` and committed `uv.lock`. Every backend command runs through uv: Ruff format/check, mypy, pytest, and package execution. The frontend will commit `package-lock.json` and use strict TypeScript, Vite with the React SWC plugin, Oxlint, Oxfmt, Vitest in one-shot mode, and a production build.

The root Makefile will provide `setup`, `format`, `lint`, `typecheck`, `test`, `build`, `check`, `up`, `down`, and `clean` targets. `make check` is the local pre-push contract and maps directly to CI jobs.

GitHub Actions will have:

- a CI workflow for backend and frontend checks plus Compose image builds on pull requests to `main` and pushes to `dev` or `main`;
- a delivery workflow that, after successful checks on `main`, publishes commit- and branch-tagged backend and frontend images to GHCR using `GITHUB_TOKEN`.

Provider calls are mocked in tests, so CI needs no Gemini or Mistral secret. Published images contain no provider credentials.

## Risks / trade-offs

- **Redis memory pressure from PDFs** -> Enforce per-file and batch limits, set expiries immediately, delete source bytes at terminal state, and document object storage as the production replacement.
- **At-least-once delivery can repeat an external call after a crash** -> Use a processing lock and terminal-state checks. Results remain consistent, though a crash between a provider response and persistence can still consume a duplicate provider request.
- **Generative page extraction can omit or invent structure** -> Require schema-valid ordered pages, keep parser choice visible, and document that PyPDF/Mistral may be preferable when determinism or OCR matters.
- **PyPDF cannot read image-only pages** -> Preserve empty pages and explain the Mistral/Gemini alternatives in the UI and README.
- **External provider latency, quotas, and cost vary** -> Keep HTTP submission asynchronous, classify retryable failures, cap attempts, expose safe failure state, and let PyPDF handle extraction without an extraction-provider call.
- **No authentication** -> Bind the demo locally and state clearly that the design is not suitable for public deployment without identity and per-document authorization.
- **GHCR publication may be disabled by repository policy** -> Keep image build validation in CI; publication failure does not affect the local Compose path and can be disabled without code changes.

## Migration plan

1. Commit OpenSpec and repository tooling on `dev`.
2. Add backend, frontend, Compose, tests, and CI while keeping provider calls mocked by default.
3. Run static gates, unit tests, container builds, and an end-to-end PyPDF smoke test with Redis.
4. Run opt-in live Gemini and Mistral smoke tests only after credentials are supplied locally.
5. Merge `dev` to `main` through a pull request after review; the delivery workflow publishes images from the merged commit.

Rollback is a Git revert of the merge commit or redeployment of the previous GHCR commit tag. Redis contains only temporary data, so no schema migration or durable-data rollback is required.

## Architecture impact

- **Affected boundary/invariant:** Introduces the API/worker boundary, Redis as queue and temporary system of record, provider adapter boundaries, and a same-origin web delivery boundary.
- **Proposed impact:** All slow PDF and AI work runs in the worker; the API only validates, persists, enqueues, and retrieves. External SDKs stay behind typed adapters.
- **Spec authority:** Current specs are absent in the greenfield repository; proposed behavior is owned by `openspec/changes/build-async-document-processor/specs/`.
- **Context authority:** Absent; bounded discovery found no `CONTEXT.md` or `CONTEXT-MAP.md`.
- **ADR authority:** Absent; bounded discovery found no ADR directory, and the take-home does not need a separate durable decision record beyond this design.
- **Architecture authority:** Absent; bounded discovery found no architecture map. This design is the proposed change-scoped authority until implementation and archive.
- **Change status:** Approved by Pedro Moratelli on 2026-10-23 in this session. Approval covers continuous implementation, specification synchronization and archive, PR creation, and squash merge after all required gates pass.
