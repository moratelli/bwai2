# Design

## Context

See [proposal.md](proposal.md) for the motivation and scope. The baseline at `f468995` passed `make check`: 118 backend tests passed, one optional real-Redis test skipped, and 17 frontend tests passed. Docker is available. Work stays in the existing linked `dev` worktree at `/Users/C5412523/dev/bwai2-dev`.

The repository already uses owner-token locks, Redis optimistic transactions for lock release, persisted document attempt fields, and separate worker/processing-service boundaries. The failures come from incomplete use of those mechanisms, not a missing queue architecture. ReportLab is already a development dependency, so the quality fixture needs no new package.

The separately approved Mistral README wording has been updated before runtime implementation. Its evidence is a successful authenticated catalogue request followed by OCR and text-generation responses with HTTP 429 and a zero-request allowance. No Mistral reintroduction or billing change is part of this change.

## Goals / Non-Goals

Preserve the HTTP schema, source-page completeness validation, parser selection, submission transaction, and existing deployment topology. Correct the state transitions and add focused evidence for them. Retention remains measured from submission, not from completion.

Do not promise exactly-once provider execution. A crash after a provider response can still cause a later admitted attempt to repeat extraction or summarization. Do not redesign stream retention, add durable extraction checkpoints, replace Jotai, repair historical corrupt records, or add infrastructure beyond an isolated Redis instance used for tests.

## Decisions

### 1. Guard state changes with Redis optimistic transactions

Use the existing `WATCH` / `MULTI` / `EXEC` pattern rather than introducing Lua and a second test execution dependency. Watch both the document hash and its lease key, read their current values, then commit only when the record is live, its status permits the transition, and the lease token matches. Terminal publication also checks the admitted attempt number. Retry a watch conflict by rereading the guards; never reuse a stale snapshot.

Redis 7 aborts a watched transaction when a watched key expires. This is documented in [Redis transactions](https://github.com/redis/docs/blob/main/content/develop/using-commands/transactions.md), including the pre-6.0.9 exception. Real-Redis tests must verify expiry between the guard read and `EXEC`; simulated deletion alone is not the final evidence.

Protect the processing transition as well as `complete` and `fail`. A separate existence check followed by an unguarded write is insufficient. On successful terminal publication, status, result/error fields, timestamps, and source deletion stay in the same transaction. Do not apply a new TTL to the document hash. Lease renewal changes only the lease expiry.

Return typed outcomes that distinguish an applied transition, an absent record, an already-terminal record, and lost ownership. Propagate these through the processing service and worker. The worker acknowledges only successful terminal publication, an already-terminal record, or an absent record. A lost lease with unfinished work leaves the stream entry pending. Source deletion must not occur on a rejected transition.

Every terminal path uses the same guards: success, missing source, permanent parser failure, and retry exhaustion. This avoids fixing the normal path while retaining an unsafe exception handler. Keep token checks internal; tokens never enter API responses or logs.

### 2. Renew the lease throughout the owned processing interval

Run one owned renewal task alongside processing and retry backoff. Derive the renewal interval from one third of the configured lease TTL; bound each renewal operation by that interval. Renewal checks token ownership and that the document remains live and non-terminal. An expired lease is not reacquired by renewal.

If renewal returns a rejection or cannot be confirmed within its timeout, cancel the local processing task and leave unfinished work pending. Cancellation is best effort for provider calls and threaded PDF parsing; the atomic publication guard is the authority that prevents stale writes.

On every exit, cancel and await the renewal task, drain the owned processing task, and attempt token-checked lease release with a bounded timeout of at most five seconds. A release failure is logged safely and allowed to expire; it must not mask the original cancellation or publish a failure. A completion racing renewal shutdown can conservatively leave a terminal entry pending for harmless redelivery.

Use an independently injectable renewal wait in tests, separate from the retry-delay function, so deterministic retry tests cannot accidentally create a tight heartbeat loop. Verify normal completion, loss of ownership, renewal errors, and worker cancellation without long sleeps.

### 3. Admit attempts atomically and persist their count

The worker owns attempt admission. After acquiring a lease, it asks the repository to atomically validate ownership, check the record state and attempt ceiling, increment the stored attempt count, and mark the document processing. The processing service receives the admitted number and token and performs one attempt; it no longer independently marks processing or invents attempt numbers.

Admission returns either the admitted number or a typed reason why no attempt starts. Lock contention, stream claims, and a failed transaction consume no attempt. A crash after committed admission still consumes that attempt, even if it occurs before the external request: the counter measures durably admitted processing, not a provable count of provider invocations. A lost acknowledgement of the admission transaction does not justify resetting the counter.

Use the stored count across retries and worker replacement. If it already reaches the ceiling, the current owner publishes a guarded `retry_exhausted` result without invoking the processor. Infrastructure failures before admission leave the entry pending and do not fabricate a terminal failure or consume the processing budget. Existing retry delays remain bounded exponential backoff, now based on the admitted count. Stream delivery count remains transport metadata only.

Change `Settings.worker_batch_size` from 10 to 1. There is no corresponding Compose or example-environment override today. Keep explicit batch configuration supported; the new correctness guards also apply to non-default batches. Do not remove the queue's delivery metadata as part of this change.

Implement ownership guards first, renewal second, and admission accounting third. The first commit may retain explicit attempt assignment behind a guarded processing transition; the third replaces that seam with atomic admission. Each intermediate commit must preserve passing focused tests.

### 4. Bound both extraction-derived chunks and synthesis inputs

Treat `SUMMARY_CHUNK_CHARACTERS` as a character budget for the rendered content portion of each request, including generated page/section labels and separators. Fixed instructions are outside that budget. Stored page results are not split or changed.

Pack content in source order, splitting a single oversized block when necessary. Preserve content rather than truncating it; handle tiny positive budgets and boundary-sized blocks without empty-chunk loops. Normal documents still use one summary call.

For multiple chunks, summarize each and pack the resulting section summaries through the same bounded-content path. If they fit, perform final synthesis. Otherwise allow at most three additional reduction passes. Each pass must strictly reduce the total rendered content length; fail with a safe `summary_too_large` permanent error on non-contraction or exhaustion. Do not add a configurable workflow engine or silently discard sections to make them fit.

Tests inspect the content segment passed to every fake provider call, including reduction and final synthesis. Cover a single oversized page, mixed pages, exact boundaries, tiny budgets, empty extraction, normal synthesis, and oversized/non-contracting provider output. This bounds our requests; it is not a token-budget or provider-capacity guarantee.

### 5. Represent a 404 as local unavailability

Add a client-only unavailable variant to the tracked-document view model, retaining the accepted document's identifier, filename, and parser. Do not extend generated `DocumentStatus`, the server response union, or the transport parser. The API must still reject an unrecognized server status.

Associate each settled polling result with the requested identifier. On `ApiError` with status 404, replace only that local card with an unavailable state and a fixed message explaining that the document was not found or has expired and can be uploaded again. Treat this state as terminal for scheduling and status announcements. Do not manufacture a server-side failed document or claim the processor completed.

Keep temporary network/server failures on the existing bounded retry path. Preserve other cards, clear transient error messages when appropriate, and make `track`/`clear` handle the local variant without reviving old IDs. Test mixed batches, later submissions, timer cleanup, and continued retries for temporary failures.

### 6. Emit existing safe correlation data

Keep standard logging. Configure a formatter with defaults for records lacking document context, and emit an explicit safe field set: event, document ID, message ID, admitted attempt, outcome/error code, and retry delay when present. Do not serialize arbitrary `LogRecord` extras, exception payloads, lease tokens, filenames, source bytes, or provider bodies.

Move the attempt-start event to successful admission and distinguish lease loss from processing failure. Tests must capture formatted output, not only inspect `caplog` extras. Include a context-free library record and secret/content canaries in unused extras to prove they are omitted.

### 7. Use one submission writer in production and tests

`RedisDocumentRepository.save_queued` and `RedisStreamQueue.publish_batch` have only test callers. Migrate those fixture setups to `RedisSubmissionGateway.create_batch`, use the gateway's existing atomicity tests, and obtain message identifiers from the stream where needed. Then remove the unused methods and typing members that no remaining caller needs.

This is a refactor: preserve repository and stream assertions and use before/after checks rather than inventing an artificial failing behavioral test. Verify callers again immediately before removal. Leave production submission behavior and batch validation unchanged.

### 8. Add one small quality example, not an evaluation platform

Commit a synthetic two-page report and an expected-facts manifest under `fixtures/`, plus a deterministic generator under `scripts/` using the existing ReportLab development dependency. Use an actual rendered table: North revenue USD 120,000, South revenue USD 80,000, total revenue USD 200,000 on page one; operating costs USD 140,000 and operating profit USD 60,000 on page two. Label the document synthetic and include no personal or customer data.

A standalone checker validates two ordered pages and the labelled page-specific facts. For the summary, require correctly labelled total revenue and operating profit. Normalize whitespace, Markdown decoration, and explicitly supported numeric equivalents such as `200,000`, `200000`, and `200k`. Do not accept a bare number occurring under the wrong label. Reject missing/swapped pages, changed values, swapped labels, empty output, and an otherwise non-empty summary missing a required fact. Keep assertions conservative and documented; a checker failure is a review signal, not permission to weaken checks until a provider passes.

Add a `make live-quality` path that runs one synthetic Gemini job in a uniquely named disposable Compose project. Its runner controls the target URL and port, enforces `TEST_SUMMARIZER_ENABLED=false` and `WORKER_MAX_ATTEMPTS=1`, and verifies the effective worker mode without printing credentials before uploading. Existing readiness output alone is insufficient when both a key and the test flag are set; the controlled worker configuration is the mode authority. The pure result checker does not claim to authenticate which provider produced arbitrary input.

Use bounded HTTP operations and a five-minute overall run deadline. Clean up only the runner-owned stack and volume. Report safe check names/statuses rather than provider bodies. The check uses one logical extraction/summary sequence; existing SDK-level retry behavior is unchanged. Normal CI runs fixture validation and fake-HTTP positive/negative harness tests without contacting Gemini. During delivery, allow one rerun only for a classified transient provider failure, never an automatic retry of a factual assertion failure.

## Verification strategy

- Add and run failing behavioral tests before each production change, then focused tests and relevant static checks. Record evidence only in `tasks.md` and commit each cohesive task there with its code/tests.
- Use fakeredis and async barriers for fast interleaving checks. Add real Redis 7 integration cases for watched expiry, token replacement, retained TTL, and conflicting terminal publication. Run them against a newly created disposable Redis instance, never an existing application database.
- CI supplies a dedicated Redis 7 service to the quality job and sets `TEST_REDIS_URL` for the existing and new integration tests. The existing fixture flushes its database, so that URL must identify the CI-owned disposable service. A missing local URL can retain the existing explicit skip; CI must execute these tests successfully rather than treating a skip as proof.
- After the last implementation edit, run fresh `make check`, the real-Redis tests, the existing Compose HTTP/concurrency and browser flows, and the optional live quality check. Inspect source ZIP contents after final commits.
- Run report-only OpenSpec semantic verification, the risk-tiered final implementation review, and strict validation while the change is active. Archive only under the subsequent complete-plan approval. Update PR #1 and the source ZIP, but do not merge or update main.

## Risks / Trade-offs

- Lease renewal cannot revoke a provider call already sent. Token-checked publication protects stored state; repeated provider cost remains possible.
- A scheduler pause or Redis outage can cause a safe lease loss and later recovery. Fail closed on uncertain ownership instead of extending ownership locally.
- Mixed old/new worker versions are unsafe because old code can still publish unguarded writes. Stop old workers before starting the updated worker process.
- Existing records may contain attempt counts produced by the old logic. Preserve their stored history; do not guess corrected counts or mutate existing data.
- Bounding synthesis can produce `summary_too_large` when a provider will not compress output. Prefer an explicit failure to unbounded calls or silent truncation.
- The quality fixture checks a few important facts, not general OCR accuracy or freedom from hallucination. No new claim about all scanned/complex PDFs follows from one success.

## Migration Plan

No Redis schema migration or HTTP contract regeneration is expected. Restart all worker processes together after stopping the old version, preserve Redis data and existing retention deadlines, and let pending entries recover normally. Do not delete locks or existing records to accelerate recovery.

Rollback requires stopping the new workers before restoring the old application revision; it also restores the old correctness limitations. Verification uses disposable stacks and does not deploy into or reset the owner's existing stack.

## Architecture impact

- Boundary/invariant: the repository enforces ownership and retention at commit time; the worker owns lease renewal and attempt admission; the service performs one admitted parse/summarize attempt. The browser adds only a local retrieval state.
- Proposed impact: strengthen existing stored-result consistency and retry semantics without changing deployment boundaries, parser vocabulary, HTTP schemas, or external provider contracts.
- `spec`: current authorities are `openspec/specs/asynchronous-document-processing/spec.md`, `document-results/spec.md`, `web-experience/spec.md`, and `local-delivery/spec.md`. Proposed deltas belong to `harden-document-processing/specs/`; current specs remain authoritative until approved synchronization/archive.
- `context`: absent. Bounded inventory of the repository, backend, frontend, and scripts found no `CONTEXT.md` or `CONTEXT-MAP.md`.
- `adr`: absent. The same bounded inventory found no applicable ADR directory or durable-decision record.
- `architecture`: no designated canonical map. README architecture text is explanatory; `openspec/config.yaml` designates no alternate authority and requires no new map.
- Change/status: `harden-document-processing` is proposed for Pedro Moratelli's approval after one independent plan review. The request authorizes preparation; runtime application and spec synchronization/archive await approval of this complete change. The existing no-merge instruction remains in force.
