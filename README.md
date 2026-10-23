# Async document processor

A small full-stack service for uploading PDFs and processing them outside the HTTP request. Each file becomes an independent Redis-backed job with ordered page content, one Markdown summary, and a terminal success or failure state.

The implementation favors a clear local review path over production infrastructure: React and Nginx in front, FastAPI for HTTP, Redis Streams for work distribution, and a separate Python worker process.

## Run it

You need Docker with Compose v2. Copy `.env.example` to an untracked `.env`, then set `GEMINI_API_KEY`. Add `MISTRAL_API_KEY` only if you want to try OCR.

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
| Rich Markdown / Gemini | `gemini` | Tables and layouts that benefit from generative extraction | The PDF goes to Google Gemini. |
| Scanned document / OCR / Mistral | `mistral` | Scans and image-heavy PDFs | The PDF goes to Mistral; extracted text then goes to Gemini for the summary. |

The app does not switch providers behind the user's back. If PyPDF finds no text, the result recommends resubmitting with OCR.

Gemini uses `gemini-3.1-flash-lite` by default. Mistral OCR uses `mistral-ocr-latest`. Both model names can be changed in `.env`.

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
                                       |      +── PyPDF / Gemini / Mistral
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
| `MISTRAL_API_KEY` | empty | Needed only for OCR mode |
| `MISTRAL_OCR_MODEL` | `mistral-ocr-latest` | Mistral OCR model |
| `DOCUMENT_TTL_SECONDS` | `86400` | Redis retention for document records |
| `MAX_FILES_PER_BATCH` | `10` | Atomic batch limit |
| `MAX_FILE_SIZE_BYTES` | `20971520` | Per-file limit, 20 MiB by default |
| `WORKER_CONCURRENCY` | `2` | Consumers in the worker process |
| `WORKER_MAX_ATTEMPTS` | `3` | Retry ceiling for temporary failures |

Provider keys are runtime variables. They are not copied into images, exposed to Vite, or included in tracked files.

`TEST_SUMMARIZER_ENABLED` is different: it exists only so the container smoke test can exercise the queue without spending provider quota. Leave it `false` for normal use. It does not enable Gemini parsing or OCR.

## Development checks

Python dependencies and commands run through `uv`; frontend dependencies use the committed npm lockfile.

```bash
make setup
make check
```

`make check` verifies generated API drift, Ruff formatting and linting, strict mypy, Oxfmt, Oxlint, strict TypeScript, backend and frontend tests, the Python package build, and the Vite production build.

If the FastAPI schema changes, regenerate and verify the frontend contract:

```bash
make generate-api
make check-api
```

Run the complete credential-free container path with:

```bash
make smoke
```

That target starts Compose with the explicit test summarizer, generates a PDF, uploads it through Nginx, waits for the worker result, checks page text and summary output, and removes the stack and Redis volume afterward.

## Live provider checks

The automated suite uses fakes at provider boundaries. To exercise Gemini, put a real `GEMINI_API_KEY` in `.env`, keep `TEST_SUMMARIZER_ENABLED=false`, start the stack, and submit a PDF in the UI. Add `MISTRAL_API_KEY` before testing OCR.

Provider content and credentials are omitted from worker logs. Logs contain document/message IDs, attempts, outcomes, and safe error codes.

## Deliberate limits

This is a take-home service, not a multi-tenant document platform.

- Redis is both the queue and temporary record store; data expires rather than moving to durable object storage.
- The API holds each bounded upload in memory before the atomic Redis transaction.
- Result delivery uses polling, not WebSockets or server-sent events.
- There is no authentication, rate limiting, malware scanning, or per-user isolation.
- PyPDF does not OCR image-only pages. The app asks the user to choose Mistral OCR explicitly because it changes privacy, latency, and cost.
- Provider-backed results depend on external availability and credentials. The smoke summarizer proves orchestration only; it is not a production fallback.
