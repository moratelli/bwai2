# Async document processor

A small full-stack service for uploading PDFs and processing them outside the HTTP request. Each file becomes an independent Redis-backed job with ordered page content, one Markdown summary, and a terminal success or failure state.

The implementation favors a clear local review path over production infrastructure: React and Nginx in front, FastAPI for HTTP, Redis Streams for work distribution, and a separate Python worker process.

## Run it

You need Docker with Compose v2. Copy `.env.example` to an untracked `.env`, then set `GEMINI_API_KEY`.

```bash
make up
```

Open [http://localhost:8080](http://localhost:8080). `make up` builds the images, starts the stack in the background, and waits for its health checks. To inspect processing logs or stop the stack:

```bash
make logs
make down
```

The API is intentionally reachable through Nginx rather than a second host port. This keeps browser and API requests on the same origin.

## Processing modes

The UI asks what kind of document it should read instead of presenting unexplained provider names.

| UI mode | API value | Best fit | What leaves the machine |
| --- | --- | --- | --- |
| Fast text / PyPDF | `pypdf` | Digital PDFs with selectable text | The PDF is parsed locally. Extracted text goes to Gemini for the summary. |
| Rich Markdown / Gemini | `gemini` | Tables, complex layouts, and scans; results depend on document quality | The PDF goes to Google Gemini. |

The app never switches parsers automatically. If Fast text finds no text, choose Rich Markdown / Gemini before resubmitting, or use a PDF with selectable text. Scan quality affects extraction.

Gemini uses `gemini-3.1-flash-lite` by default. Its model name can be changed in `.env`.

## Try the API with curl

Generate a one-page PDF that PyPDF can extract:

```bash
python3 scripts/generate_sample_pdf.py .tmp/sample.pdf
```

With the stack running and Gemini configured, submit it through the same Nginx endpoint used by the browser:

```bash
curl --fail-with-body \
  -X POST http://localhost:8080/api/v1/documents \
  -F 'parser=pypdf' \
  -F 'files=@.tmp/sample.pdf;type=application/pdf'
```

The response contains a document ID and HTTP status `202`. Poll that document until `status` becomes `completed` or `failed`:

```bash
curl --fail-with-body \
  http://localhost:8080/api/v1/documents/<document-id>
```

Repeat the `files` form field to submit several PDFs in one batch. The API validates the whole batch before it writes any job.

## Test the API with Bruno

Open the checked-in `bruno/` folder as a collection. It includes local environments, synthetic PDF fixtures, response assertions, and automatic document-ID capture for PyPDF, batch, worker-failure, validation, and live Gemini requests.

Follow the [Bruno runbook](bruno/README.md). Select **Local** for port `8080` or **Manual** for an isolated stack on port `18080`. Keep Gemini credentials in the server's `.env`, not in Bruno.

## API surface

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/documents` | Validate PDFs and enqueue one job per file |
| `GET` | `/api/v1/documents/{document_id}` | Read queued, processing, completed, or failed state |
| `GET` | `/health/live` | Confirm that the API process is alive |
| `GET` | `/health/ready` | Check Redis and summary-provider readiness |

FastAPI owns the OpenAPI contract. The generated schema lives at `frontend/openapi.json`; `openapi-typescript` turns it into `frontend/src/generated/api.ts`. Runtime parsing in the frontend still validates untrusted JSON before it enters Jotai state.

## Architecture and Redis flow

```text
Browser
  |
  v
Nginx :8080  ── static React app
  |
  v
FastAPI API  ── validate batch ──> Redis document/source keys
  |                                  |
  |                                  v
  |                             documents:processing
  |                                  |
  v                                  v
poll result <── Redis terminal state <── worker pool
                                       |      |
                                       |      +── PyPDF / Gemini
                                       +──────── Gemini summary
```

A submission transaction stores metadata under `document:<uuid>`, raw PDF bytes under `document:<uuid>:source`, and an entry in the `documents:processing` stream. Keys expire after 24 hours by default.

Workers use the `document-workers` consumer group. They reclaim stale pending messages before reading new ones, acquire `document:<uuid>:lock`, and acknowledge a stream entry only after Redis contains a terminal result. Terminal writes also delete the raw PDF. Temporary provider failures get bounded exponential retries; permanent failures are stored with safe error codes and messages.

`WORKER_CONCURRENCY` controls the number of consumers inside the worker container. The default is two.

## Configuration

`.env.example` contains every setting used by Compose. The main ones are:

| Variable | Default | Notes |
| --- | --- | --- |
| `APP_PORT` | `8080` | Localhost port for Nginx |
| `GEMINI_API_KEY` | empty | Required for normal submissions and all normal summaries |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Gemini extraction and summary model |
| `DOCUMENT_TTL_SECONDS` | `86400` | Redis retention for document records |
| `MAX_FILES_PER_BATCH` | `10` | Atomic batch limit |
| `MAX_FILE_SIZE_BYTES` | `20971520` | Per-file limit, 20 MiB by default |
| `WORKER_CONCURRENCY` | `2` | Consumers in the worker process |
| `WORKER_MAX_ATTEMPTS` | `3` | Retry ceiling for temporary failures |

The Gemini key is a runtime variable. It is not copied into images, exposed to Vite, or included in tracked files.

`TEST_SUMMARIZER_ENABLED` is different: it exists only so the container smoke test can exercise the queue without spending provider quota. Leave it `false` for normal use. It does not enable Gemini parsing or OCR.

## Development checks

Python dependencies and commands run through `uv`; frontend dependencies use the committed npm lockfile.

```bash
make setup
make check
```

`make check` verifies generated API drift, Ruff formatting and linting, strict mypy, Oxfmt, Oxlint, strict application and Playwright TypeScript, backend and frontend tests, the Python package build, and the Vite production build.

If the FastAPI schema changes, regenerate and verify the frontend contract:

```bash
make generate-api
make check-api
```

In a dedicated test shell, select a disposable Compose project and port before running the container checks below:

```bash
export COMPOSE_PROJECT_NAME=bwai2-check
export APP_PORT=18080
export APP_URL=http://127.0.0.1:18080
export PLAYWRIGHT_BASE_URL="$APP_URL"
```

The checks remove the selected stack and its Redis volume. Do not point them at a stack whose data you need.

Run the complete credential-free container path with:

```bash
make smoke
```

That target starts Compose with an empty Gemini key and the explicit test summarizer. It first uploads a generated PDF through Nginx and checks the worker result. It then runs `scripts/concurrency_smoke_test.py` against FastAPI directly inside the API container:

- Open ten upload connections together and hold them in flight while checking liveness, readiness, and a document lookup.
- Release the uploads and require ten HTTP `202` responses with distinct document IDs.
- Poll for nine completed documents and one expected corrupt-PDF failure. Check each successful document's filename, page text, and summary against its unique fixture.

The concurrency check requires test-only summarization before submitting any PDFs, uses five-second HTTP timeouts, and allows sixty seconds for the synthetic jobs to finish. Ten clients are a chosen acceptance test, not a stated assignment limit or a production capacity claim. The test does not add submission deduplication or exercise crashes and network outages. CI runs it as a required step.

`make smoke` removes the selected stack and Redis volume afterward; it does not edit your `.env`.

Run the same workflow through Chromium with:

```bash
make browser-e2e
```

This target installs the pinned Playwright Chromium build, opens the real React UI, selects the generated PDF, submits it with PyPDF, waits for the Redis worker result, and checks the rendered summary and page content. CI runs both the HTTP smoke test and this browser test against the same Compose stack, then uploads the Playwright HTML report.

## Published images

After CI succeeds for a push to `main`, the CD workflow publishes two images:

- `ghcr.io/moratelli/bwai2-backend`
- `ghcr.io/moratelli/bwai2-frontend`

Each image gets `sha-<full-commit-sha>` and `latest` tags. Pull requests and `dev` pushes never publish packages. The workflow checks out the exact revision that passed CI before building either image.

## Live Gemini check

Unit tests use fakes at provider boundaries so normal CI is deterministic and does not need secrets. To test real Gemini PDF extraction and summarization, configure `GEMINI_API_KEY` in the untracked `.env`, select a disposable test stack as above, then run:

```bash
make live-providers
```

The target disables the test summarizer and submits one generated PDF with `parser=gemini`. It checks terminal completion, an ordered page list containing extracted text, and a non-empty Gemini summary without printing credentials or provider content. The script also has credential-free tests that replace HTTP responses and verify these checks.

## Deliberate limits

This is a take-home service, not a multi-tenant document platform.

- Redis is both the queue and temporary record store; data expires rather than moving to durable object storage.
- The API holds each bounded upload in memory before the atomic Redis transaction.
- Result delivery uses polling, not WebSockets or server-sent events.
- There is no authentication, rate limiting, malware scanning, or per-user isolation.
- PyPDF does not OCR image-only pages. Rich Markdown uses Gemini's visual PDF processing, but scan quality can limit extraction.
- Local records from an older parser configuration are not migrated. Start a new test stack, or remove old Redis data only when it is disposable.
- Provider-backed results depend on external availability and credentials. The smoke summarizer proves orchestration only; it is not a production fallback.
