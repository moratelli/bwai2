# asynchronous-document-processing Specification

## Purpose
Defines reliable background delivery, parser-specific page extraction, Gemini summarization, retry behavior, and document state transitions.

## Requirements

### Requirement: Process outside the request lifecycle

The system SHALL consume accepted document jobs through a Redis Streams consumer group without holding the upload request open for extraction or summarization. By default, each consumer SHALL fetch one outstanding job at a time.

#### Scenario: Worker consumes queued work

- **WHEN** a worker receives an accepted stream entry
- **THEN** the document moves from `queued` to `processing` before parser work begins

#### Scenario: Worker restarts with pending work

- **WHEN** a worker becomes unavailable after claiming an unacknowledged entry
- **THEN** another worker can reclaim and continue the stale entry

#### Scenario: Default consumer is busy

- **WHEN** a consumer uses the default configuration and is processing a document
- **THEN** it does not prefetch other jobs into a local waiting batch

### Requirement: Make processing idempotent

The system MUST prevent at-least-once stream delivery from producing conflicting duplicate results for the same document. Stored terminal results MUST remain immutable, and only the current processing lease holder SHALL publish a result for a live, non-terminal document. This guarantee does not require exactly-once external provider calls.

#### Scenario: Completed entry is delivered again

- **WHEN** a worker receives an entry for a document already in `completed`
- **THEN** the worker performs no provider calls and acknowledges the entry

#### Scenario: Failed entry is delivered again

- **WHEN** a worker receives an entry for a document already in terminal `failed` state
- **THEN** the worker performs no provider calls and acknowledges the entry

#### Scenario: Original worker finishes after ownership changes

- **WHEN** a worker loses its lease and later finishes extraction, summarization, or error handling
- **THEN** its stale success or failure cannot change the document, remove another worker's source data, or replace a terminal result

#### Scenario: Processing outlasts the original lease duration

- **WHEN** a worker remains healthy and its document remains live during a long operation
- **THEN** it renews its lease while processing and during retry backoff so another worker cannot acquire concurrent ownership

#### Scenario: Lease renewal fails or ownership is lost

- **WHEN** a worker cannot confirm renewal of its current lease
- **THEN** it stops its local processing attempt and does not publish a stale result or acknowledge an unfinished document

### Requirement: Generate one document summary

The system SHALL use the configured Gemini model to create one Markdown summary from the extracted page content for every successfully processed document. The configured summary chunk size SHALL bound document-derived content in every summarization request, including synthesis requests; fixed instruction text is outside that content budget.

#### Scenario: Normal-size document

- **WHEN** the extracted content fits within the configured summary chunk size
- **THEN** Gemini produces one final Markdown summary from the full extracted content

#### Scenario: Large extracted document

- **WHEN** extracted content exceeds the configured summary chunk size
- **THEN** the worker summarizes bounded page groups and synthesizes them into one final Markdown summary

#### Scenario: Empty extracted document

- **WHEN** all extracted pages are empty
- **THEN** the document fails without invoking another parser and explains that no readable text was extracted, with conditional guidance for Fast text users to select Rich Markdown (Gemini) or use a text-based PDF

#### Scenario: One page exceeds the content budget

- **WHEN** a single rendered page block exceeds the configured chunk size
- **THEN** the summarizer splits it in source order without silently truncating its content, and every request's document-derived content stays within the bound

#### Scenario: Partial summaries exceed the synthesis budget

- **WHEN** the collected section summaries cannot fit in one bounded synthesis request
- **THEN** the summarizer reduces them through bounded requests before attempting final synthesis

#### Scenario: Summaries do not converge within the bounded reduction policy

- **WHEN** reduction fails to shrink the content or reaches its bounded reduction limit
- **THEN** the document fails with a safe `summary_too_large` error without sending an oversized request or silently discarding content

### Requirement: Handle transient processing failures

The system SHALL retry retryable provider and infrastructure failures within a configurable ceiling of three total admitted processing attempts by default. It SHALL persist attempt admission before work begins and SHALL not count stream delivery or lock contention as processing attempts. Permanent validation or document errors SHALL not be retried.

#### Scenario: Temporary provider failure recovers

- **WHEN** a provider returns a retryable rate-limit or server error and a later attempt succeeds
- **THEN** the document reaches `completed` with one stored result

#### Scenario: Retry budget is exhausted

- **WHEN** a retryable operation still fails on the third total attempt with the default ceiling
- **THEN** the document moves to `failed` and the stream entry is acknowledged

#### Scenario: Permanent parser failure

- **WHEN** the PDF is corrupt, encrypted, or rejected as unsupported by the selected parser
- **THEN** the document moves directly to `failed` without consuming the remaining retry budget

#### Scenario: Messages are reclaimed without processing

- **WHEN** an entry is delivered or reclaimed repeatedly without a worker admitting a processing attempt
- **THEN** the document's persisted attempt count and remaining processing budget are unchanged

#### Scenario: Worker stops after attempt admission

- **WHEN** a worker stops after admission has been persisted and another worker takes ownership
- **THEN** the replacement continues from the persisted count rather than resetting the budget or using the stream delivery count

#### Scenario: Recovery finds an already exhausted processing budget

- **WHEN** a replacement worker owns a live, non-terminal document whose admitted attempts have reached the configured ceiling
- **THEN** it publishes `retry_exhausted` without starting another parser or summary call

#### Scenario: Infrastructure fails before admission

- **WHEN** an infrastructure failure prevents attempt admission from being persisted
- **THEN** no processing attempt is consumed and the unfinished stream entry remains unacknowledged

### Requirement: Publish terminal state atomically

The system MUST publish terminal fields and source deletion together, only while the document is live, non-terminal, and owned by the writer's lease. It SHALL acknowledge an entry only after successful terminal publication or observation that the document is already terminal or absent. Losing ownership alone SHALL NOT authorize acknowledgement.

#### Scenario: Processing succeeds

- **WHEN** extraction and summarization both complete
- **THEN** pages, summary, completion metadata, and `completed` state become observable together before acknowledgement

#### Scenario: Processing fails terminally

- **WHEN** processing cannot continue
- **THEN** a stable error code, safe error message, attempt count, and `failed` state become observable before acknowledgement

#### Scenario: Document expires between validation and publication

- **WHEN** the document expires before a state update commits
- **THEN** the update does not recreate any metadata or result record, and lookup continues to return HTTP 404

#### Scenario: Ownership changes between validation and publication

- **WHEN** another worker acquires the lease before a state update commits
- **THEN** the stale update does not commit and does not acknowledge unfinished work

#### Scenario: An expired document is delivered again

- **WHEN** the worker observes that a delivered document is absent
- **THEN** it acknowledges the stale entry without recreating the document or invoking a provider

### Requirement: Remove transient source data

The system SHALL delete the raw uploaded PDF after terminal processing while retaining metadata and results until the original submission retention deadline, which is 24 hours by default. Processing, retries, lease renewal, and terminal publication SHALL NOT extend that deadline.

#### Scenario: Document completes

- **WHEN** a document reaches `completed`
- **THEN** its raw PDF is deleted and its result remains available until the retention period ends

#### Scenario: Document fails

- **WHEN** a document reaches `failed`
- **THEN** its raw PDF is deleted and its safe failure metadata remains available until the retention period ends

#### Scenario: Document expires while processing

- **WHEN** the retention deadline passes during extraction, summarization, or retry backoff
- **THEN** late work cannot recreate the document or give it a new retention period

### Requirement: Extract ordered content with supported parsers
The system SHALL store one page result per PDF page in ascending zero-based page order and format each page as Markdown-compatible text.

#### Scenario: PyPDF parser succeeds
- **WHEN** a text-based PDF is processed with `pypdf`
- **THEN** each PDF page produces a corresponding page result whose extracted text is safe to render as Markdown text

#### Scenario: Gemini parser succeeds
- **WHEN** a PDF is processed with `gemini`
- **THEN** Gemini returns structured page-level Markdown that is validated before storage

#### Scenario: Page has no extractable PyPDF text
- **WHEN** PyPDF returns no text for a page
- **THEN** the page remains present with empty content rather than being dropped or renumbered
