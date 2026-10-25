# Async document processor

A small full-stack service for uploading PDFs and processing them outside the HTTP request. Each file becomes an independent Redis-backed job with ordered page content, one Markdown summary, and a terminal success or failure state.

The implementation favors a clear local review path over production infrastructure: React and Nginx in front, FastAPI for HTTP, Redis Streams for work distribution, and a separate Python worker process.

## Run it

You need Docker with Compose v2 and `make`. Run the commands below from the repository root. On a fresh checkout, copy `.env.example` to an untracked `.env`, then set `GEMINI_API_KEY`. Keep an existing `.env` rather than overwriting it.

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

Gemini extraction sends the whole PDF in one request and expects every page in one response. Passing the upload-size limit does not guarantee that the full extraction fits in a response. The bundled two-page quality check covers that fixture; it does not establish large-document reliability.

Rejected extraction responses keep the `invalid_provider_response` code, with a specific message for a confirmed response limit, empty output, malformed JSON, an invalid page schema, or mismatched page counts or numbering. The app rejects incomplete results rather than silently dropping pages. Missing pages or malformed JSON alone do not prove that a response limit was reached.

Summarization also rejects a consumed response marked `MAX_TOKENS`, even if it contains text. The document fails with `invalid_provider_response`: "Gemini reached its response limit before a complete summary could be confirmed." This applies to section summaries, reductions and final synthesis. Rejected text never feeds another synthesis step. Absent or unrecognized finish metadata follows normal summary validation; this check does not increase document capacity.

These diagnostics apply to new processing after the worker is updated. Existing failed records keep their stored message.

### Gemini request budget

Every production extraction, summary chunk, reduction, and synthesis request passes through a Redis admission gate. The default is 12 requests per minute, spaced at least five seconds apart at admission. SDK automatic retries are disabled so they cannot bypass that accounting. Local PyPDF extraction and the test summarizer do not consume permits.

Cooperating workers must share Redis, model, `GEMINI_RATE_LIMIT_SCOPE`, and the configured RPM. A conflicting budget or uncertain Redis operation fails closed. Waiting clients recheck the gate instead of reserving future slots, and restarts do not reset it. When changing a scope's budget, stop its workers and allow its admission/cooldown state to expire before starting the matching configuration. Never change scopes to evade an active cooldown.

A confirmed retryable provider failure extends a shared cooldown, even when that document has exhausted its attempts. Backoff starts at 15 seconds, doubles by admitted attempt, and caps its exponential component at 60 seconds. The worker adds up to five seconds of jitter. Numeric or HTTP-date `Retry-After` and `google.rpc.RetryInfo` delays up to one day can extend the wait; the exponential cap never shortens a supported hint. Malformed, negative, non-finite, or longer hints use fallback backoff. Already-sent requests can finish; later admissions wait and recheck.

This is application admission pacing, not a guarantee about request arrival times or provider availability. Other clients and separate Redis deployments can still consume the project's quota. The app does not proactively enforce input-token or daily quotas. It records returned token usage when available; missing usage is unknown, not zero.

### Why Mistral OCR was removed

Mistral OCR was removed because the available free Mistral Studio account had zero execution quota. The API key authenticated, but OCR requests returned HTTP 429 with an allowed rate of zero requests per minute. We therefore could not run and verify the integration and retained PyPDF and Gemini.

## Try the API with curl

The repository includes a synthetic one-page PDF, so this example needs no local Python setup. With the stack running and Gemini configured, submit it through the same Nginx endpoint used by the browser:

```bash
curl --fail-with-body \
  -X POST http://localhost:8080/api/v1/documents \
  -F 'parser=pypdf' \
  -F 'files=@bruno/fixtures/sample.pdf;type=application/pdf'
```

The response contains a document ID and HTTP status `202`. Replace `<document-id>` below with that ID and poll until `status` becomes `completed` or `failed`:

```bash
curl --fail-with-body \
  "http://localhost:8080/api/v1/documents/<document-id>"
```

Repeat the `files` form field to submit several PDFs in one batch. The API validates the whole batch before it writes any job.

A result lookup returning HTTP `404` means the document was not found or has expired. The browser stops polling that document and offers resubmission guidance, while other jobs continue. Temporary network and server failures still retry. The unavailable label is local to the browser; it is not a new API status.

## Test the API with Bruno

Open the checked-in `bruno/` folder as a collection. It includes local environments, synthetic PDF fixtures, response assertions, and automatic document-ID capture for PyPDF, batch, worker-failure, validation, and live Gemini requests.

Follow the [Bruno runbook](bruno/README.md). Select **Local** for port `8080` or **Manual** for an isolated stack on port `18080`. Keep Gemini credentials in the server's `.env`, not in Bruno.

## API surface

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/documents` | Validate PDFs and enqueue one job per file |
| `GET` | `/api/v1/documents/{document_id}` | Read queued, processing, completed, or failed state |
| `GET` | `/health/live` | Confirm that the API process is alive |
| `GET` | `/health/ready` | Check Redis and summary-provider configuration |

Readiness does not contact Gemini. A `configured` result means a key was supplied, not that Google accepted it or that quota is available. Use a live processing check to verify extraction and summarization.

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

A submission transaction stores metadata under `document:<uuid>`, raw PDF bytes under `document:<uuid>:source`, and an entry in the `documents:processing` stream. Document and source keys expire after 24 hours by default.

Workers use the `document-workers` consumer group. They reclaim stale pending messages before reading new ones and acquire `document:<uuid>:lock`. They acknowledge and delete each eligible stream entry in one transaction, only after terminal publication or after observing an already-terminal or absent document. Terminal writes also delete the raw PDF. Temporary provider failures get bounded exponential retries; permanent failures are stored with safe error codes and messages.

The processing stream supports one logical consumer group shared by all worker instances. Confirm that topology before deploying this cleanup behavior: deleting an entry removes its payload for every group, so separate groups on the same stream are unsupported. Pending and unread jobs remain recoverable. Completed and failed document results keep their existing expiry deadlines.

Queue entries are not an audit log. Startup does not scan or purge previously acknowledged history; cleanup applies only to entries subsequently selected for safe acknowledgement.

`WORKER_CONCURRENCY` controls the number of consumers inside the worker container. The default is two. Each consumer fetches one job at a time by default; larger batches remain an explicit worker-setting override.

The retry budget counts attempts admitted in a Redis transaction, not stream deliveries or lock contention. A worker restart continues the stored count. Failure before admission commits spends no attempt; a crash or lost response after that commit still spends it, even if no provider request was sent. Existing counters from the earlier implementation are preserved rather than rewritten.

State changes check the current lease owner and the live document record in the same Redis transaction. Terminal writes also check the processing attempt. A stale worker cannot overwrite a result, delete another worker's source, or recreate an expired record. Processing does not extend the original retention deadline.

Retry storage uses those same owner and attempt checks. A future retry deadline defers admission without incrementing the counter; Redis server time decides when it is due. Admission clears the deadline but retains the attempt-tagged last failure. A later infrastructure failure replaces earlier Gemini attribution. Completion clears all retry evidence, permanent failure clears unrelated evidence, and exhaustion retains the last confirmed context. Missing or malformed optional metadata does not manufacture a cause or break legacy records.

The worker renews its lease every third of its configured lifetime, including while waiting between retries. If renewal fails or cannot be confirmed within that interval, it cancels local processing and leaves unfinished work pending. Cleanup cancels and awaits the owned tasks and bounds lease release to at most five seconds. A failed release is logged without replacing the original processing or cancellation outcome.

Cancellation cannot revoke a provider request already sent. A crash or lost lease can still lead to repeated provider calls; the ownership checks protect stored results, not exactly-once execution.

When upgrading from the earlier worker implementation, stop all old workers before starting the new version. Mixed worker versions are unsafe because the old code can still write without these guards. Keep Redis data and let pending jobs recover; do not delete locks to speed up a restart.

Worker logs contain one JSON record per line. Correlation uses the last confirmed admitted attempt, not the stream delivery count. Missing fields are `null`, including for library records. The formatter excludes free-text messages, exception traces, tokens, filenames, document/provider payloads, and arbitrary extras. A synthetic example:

```json
{
  "timestamp": "2026-10-24T12:00:00+00:00",
  "level": "WARNING",
  "logger": "document_processor.worker",
  "event": "document_retry_scheduled",
  "document_id": "00000000-0000-4000-8000-000000000001",
  "message_id": "1-0",
  "attempt": 1,
  "outcome": null,
  "error_code": "gemini_temporarily_unavailable",
  "delay_seconds": 15.0,
  "provider": null,
  "provider_category": null,
  "provider_stage": null,
  "http_status": null,
  "duration_ms": null,
  "prompt_token_count": null,
  "candidates_token_count": null
}
```

`gemini_request_completed` and `gemini_request_failed` events report the stage, duration, known HTTP status, and safe provider category when available. Completed requests can include returned prompt/output token counts. These events can lack document context; they never include prompts, generated text, raw headers, or provider response bodies.

## Configuration

`.env.example` contains every setting used by Compose. The main ones are:

| Variable | Default | Notes |
| --- | --- | --- |
| `APP_PORT` | `8080` | Localhost port for Nginx |
| `GEMINI_API_KEY` | empty | Required for normal submissions and all normal summaries |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Gemini extraction and summary model |
| `GEMINI_REQUESTS_PER_MINUTE` | `12` | Shared generation admission budget, 1–1000 |
| `GEMINI_RATE_LIMIT_SCOPE` | `bwai2` | Nonsecret shared label: 1–64 ASCII letters, digits, `_` or `-` |
| `DOCUMENT_TTL_SECONDS` | `86400` | Redis retention for document records |
| `MAX_FILES_PER_BATCH` | `10` | Atomic batch limit |
| `MAX_FILE_SIZE_BYTES` | `20971520` | Per-file limit, 20 MiB by default |
| `WORKER_CONCURRENCY` | `2` | Consumers in the worker process |
| `WORKER_MAX_ATTEMPTS` | `3` | Retry ceiling for temporary failures |
| `WORKER_RETRY_BASE_SECONDS` | `15` | Initial exponential backoff |
| `WORKER_RETRY_MAX_SECONDS` | `60` | Exponential component cap; does not cap supported provider hints |
| `WORKER_RETRY_JITTER_SECONDS` | `5` | Maximum additive jitter |

The Gemini key is a runtime variable. It is not copied into images, exposed to Vite, or included in tracked files.

`TEST_SUMMARIZER_ENABLED` is different: it exists only so the container smoke test can exercise the queue without spending provider quota. Leave it `false` for normal use. It does not enable Gemini parsing or OCR.

Summary requests enforce the configured character budget on page and section content, including labels and separators. Fixed instructions are outside that budget; this is not a token-limit guarantee. Splitting does not change the extracted pages in a successful result. Oversized section summaries get at most three reduction passes; failure to shrink or fit ends the job with `summary_too_large` rather than sending oversized content or dropping sections.

## Development checks

Host checks need Python 3.12, `uv`, Node.js 24 with npm, and `make`. CI uses `uv` 0.11.7. Python dependencies and commands run through `uv`; frontend dependencies use the committed npm lockfile. Container checks also need Docker with Compose v2.

```bash
make setup
make check
```

Choose the check for the evidence you need:

| Command | Coverage | Live Gemini calls |
| --- | --- | --- |
| `make check` | Static checks, API/unit/integration tests and builds; real Redis cases require `TEST_REDIS_URL` | No |
| `make smoke` | Real Compose HTTP/worker flow and ten concurrent uploads, with a deterministic test summary | No |
| `make browser-e2e` | One Chromium upload-to-result flow, with a deterministic test summary | No |
| `make live-providers` | One-page Gemini extraction and a non-empty real summary | Yes |
| `make live-quality` | Two-page Gemini extraction and summary, checked against selected labelled facts | Yes |

`make check` does not run the Compose, browser or live-provider checks. The test summarizer verifies orchestration; it cannot establish whether Gemini produces a correct summary.

`make check` verifies generated API drift, Ruff formatting and linting, strict mypy, Oxfmt, Oxlint, strict application and Playwright TypeScript, backend and frontend tests, the Python package build, and the Vite production build.

CI provides a disposable Redis 7 service for the integration tests. Locally, set `TEST_REDIS_URL` to a disposable Redis 7 instance before running the checks to include real transaction, expiry, and ownership tests. The submission test clears the selected database: never use an application database. Without that variable, real-Redis cases explicitly skip; a configured but unreachable instance fails the run. Run these tests serially against their own database; their fixtures are not designed for parallel runs sharing it.

For an optional backend coverage report, keep that disposable `TEST_REDIS_URL` configured and run:

```bash
mkdir -p .tmp
COVERAGE_FILE="$PWD/.tmp/backend.coverage" \
  uv run --project backend pytest backend/tests \
  --cov=document_processor --cov-branch \
  --cov-config=backend/pyproject.toml --cov-report=term-missing
```

This measures the `document_processor` package, not frontend code or standalone scripts. There is no enforced percentage threshold. Inspect missing branches and behavior assertions rather than treating the percentage as a quality verdict.

If the FastAPI schema changes, regenerate and verify the frontend contract:

```bash
make generate-api
make check-api
```

In a dedicated test shell, select a new disposable Compose project and an unused port before running the container checks below. The example uses `18081` to stay separate from the Bruno manual stack on `18080`:

```bash
export COMPOSE_PROJECT_NAME="bwai2-check-$(date +%s)"
export APP_PORT=18081
export APP_URL=http://127.0.0.1:18081
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

After `make setup`, run the same workflow through Chromium with an explicitly empty provider key:

```bash
GEMINI_API_KEY='' make browser-e2e
```

This target installs the pinned Playwright Chromium build, opens the real React UI, selects the generated PDF, submits it with PyPDF, waits for the Redis worker result, and checks the rendered summary and page content. CI runs both the HTTP smoke test and this browser test against the same Compose stack, then uploads the Playwright HTML report.

## Quality fixture

`fixtures/quality-report.pdf` is a deterministic two-page synthetic report with a revenue table. The [fixture guide](fixtures/README.md) explains regeneration and offline labelled-fact checks. These checks do not use provider credentials.

For one live extraction/summary check, run `make live-quality`. It uses the existing Gemini credential in its own disposable stack, verifies real-summarizer mode, and allows one admitted attempt. It has a five-minute budget and checks the labelled facts before removing its own containers and volume. It does not reuse your running stack or retry a failed job automatically.

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
- There is no authentication, incoming API rate limiting, malware scanning, or per-user isolation.
- PyPDF does not OCR image-only pages. Rich Markdown uses Gemini's visual PDF processing, but scan quality can limit extraction.
- Local records from an older parser configuration are not migrated. Start a new test stack, or remove old Redis data only when it is disposable.
- Provider-backed results depend on external availability and credentials. The smoke summarizer proves orchestration only; it is not a production fallback.
