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

### Requirement: Explain rejected Gemini extraction responses

When rejecting a Gemini extraction response, the system SHALL report an evidence-based, safe reason through the existing `invalid_provider_response` error. It SHALL retain permanent-failure behavior and complete-page validation and SHALL not expose provider content in errors or logs.

#### Scenario: Provider reports an output limit

- **WHEN** the consumed response candidate reports `MAX_TOKENS`
- **THEN** the failure message identifies the response limit, even if the returned text is empty, malformed or appears structurally complete
- **AND** no partial result is accepted

#### Scenario: Provider returns no extraction text

- **WHEN** the response text is absent, empty or whitespace and there is no confirmed output-limit signal
- **THEN** the failure message identifies empty extraction output without claiming that a size or token limit was reached

#### Scenario: Provider returns malformed JSON

- **WHEN** non-empty response text is not valid JSON and there is no confirmed output-limit signal
- **THEN** the failure message identifies malformed JSON without quoting the response or guessing its cause

#### Scenario: Provider returns an invalid schema

- **WHEN** valid JSON fails the required page schema, including an empty page array
- **THEN** the failure message identifies an invalid page structure

#### Scenario: Provider returns a different page count

- **WHEN** schema-valid output contains fewer or more pages than the source PDF
- **THEN** the failure message states the expected and returned page counts and rejects the result

#### Scenario: Provider returns invalid page indexes

- **WHEN** schema-valid output has the expected page count but its indexes are duplicated, missing, non-zero-based or out of order
- **THEN** the failure message identifies invalid page numbering or order and rejects the result

#### Scenario: Finish metadata is absent or unrecognized

- **WHEN** there is no recognized output-limit signal for the consumed candidate
- **THEN** normal text, schema and page-completeness validation determines the outcome
- **AND** metadata from an unused alternative does not misclassify that outcome

#### Scenario: Complete many-page response passes validation

- **WHEN** a response satisfies the existing schema and contains every source page in zero-based order without a confirmed output-limit signal
- **THEN** validation succeeds without an added page-count cutoff

#### Scenario: Rejection reaches the result boundary

- **WHEN** the current processing owner receives a classified response rejection
- **THEN** the document fails permanently with `invalid_provider_response` and its safe message in the existing result schema
- **AND** the UI displays that message, summarization is not invoked, and no partial pages are published

#### Scenario: Provider text contains sensitive data

- **WHEN** rejected text, schema fields or provider metadata contain sensitive strings
- **THEN** diagnostic errors and emitted logs exclude those strings, raw validation details and document content
- **AND** messages contain only fixed wording and, where relevant, numeric page counts

### Requirement: Reject confirmed truncated Gemini summaries

The system SHALL reject any Gemini summarization response whose consumed candidate reports `MAX_TOKENS`, using the existing permanent `invalid_provider_response` error and a safe message. It SHALL neither publish that response as a completed summary nor use it in a later synthesis step.

#### Scenario: Final summary reaches the output limit

- **WHEN** the consumed candidate for a direct document summary or final synthesis reports `MAX_TOKENS`
- **THEN** the document fails permanently rather than publishing a completed result
- **AND** the rejection applies whether its text is absent, partial or apparently complete

#### Scenario: Intermediate summary reaches the output limit

- **WHEN** a section summary or reduction response reports `MAX_TOKENS` for the consumed candidate
- **THEN** processing stops without sending that response to a later summarization request
- **AND** no partial pages or summary are published as a completed document

#### Scenario: Finish metadata is absent or unrecognized

- **WHEN** the consumed candidate has no recognized output-limit signal
- **THEN** normal summary validation determines the outcome without inventing truncation

#### Scenario: An unused candidate reports an output limit

- **WHEN** only an unused alternative candidate reports `MAX_TOKENS`
- **THEN** that alternative does not change validation of the consumed candidate

#### Scenario: Ordinary summary output remains valid

- **WHEN** a response passes existing summary validation without a confirmed output-limit signal
- **THEN** the system retains the existing successful summary flow, content budgets and retry policy

#### Scenario: Rejection reaches the stored result

- **WHEN** the current processing owner receives a confirmed summary-limit rejection
- **THEN** the existing result interface exposes a failed document with `invalid_provider_response` and its safe message
- **AND** the failure is persisted before acknowledgement without another provider attempt

#### Scenario: Rejected output contains sensitive data

- **WHEN** rejected summary text or metadata contains sensitive strings
- **THEN** the diagnostic message and emitted logs exclude those strings and raw provider details

### Requirement: Reclaim acknowledged processing entries

For a processing stream with one logical consumer group shared by its workers, the system SHALL acknowledge and remove an eligible entry atomically under the existing terminal-state rules. It SHALL preserve unrelated unread and unfinished pending entries and retain the existing document-result expiry policy.

#### Scenario: Successful processing is acknowledged

- **WHEN** a completed result has been safely persisted and its queue entry is acknowledged
- **THEN** that entry is absent from both the group's pending list and the stream
- **AND** the document result remains available until its existing retention deadline

#### Scenario: Failed processing is acknowledged

- **WHEN** a permanent or exhausted failure has been safely persisted and its queue entry is acknowledged
- **THEN** that entry is removed from both the pending list and the stream without removing the stored failure result

#### Scenario: A terminal or absent document is redelivered

- **WHEN** a delivered entry refers to an already-terminal or absent document
- **THEN** safe acknowledgement also removes that entry without invoking a provider or recreating document data

#### Scenario: Another job is still waiting or processing

- **WHEN** one eligible entry is reclaimed while other entries are unread or pending
- **THEN** those other entries remain available for normal delivery or stale-message recovery

#### Scenario: Ownership is lost before publication

- **WHEN** a worker loses ownership and cannot publish its unfinished result
- **THEN** it neither acknowledges nor removes that job's entry

#### Scenario: Cleanup is repeated

- **WHEN** acknowledgement is repeated for an entry already acknowledged and removed
- **THEN** it is harmless and does not alter another entry or the stored document result

#### Scenario: Cleanup is interrupted before execution

- **WHEN** the acknowledgement-and-removal operation has not executed
- **THEN** neither part takes effect and the pending entry remains recoverable
- **AND** another client cannot observe an interleaving between the operation's acknowledgement and removal

#### Scenario: Updated workers start with retained history

- **WHEN** updated workers start on a stream containing previously acknowledged history
- **THEN** startup does not scan or purge that history
- **AND** cleanup is limited to entries subsequently selected for safe acknowledgement
