# document-results Specification

## Purpose
Defines the polling contract for document progress, completed page content and summaries, and safe terminal failure information.

## Requirements

### Requirement: Retrieve document state
The system SHALL expose a document resource by opaque identifier with filename, parser, status, timestamps, attempt count, and status-appropriate fields.

#### Scenario: Queued document
- **WHEN** a client retrieves a queued document identifier
- **THEN** the API returns HTTP 200 with `queued` status and no pages, summary, or error

#### Scenario: Processing document
- **WHEN** a client retrieves a document currently being processed
- **THEN** the API returns HTTP 200 with `processing` status and no partial result

#### Scenario: Unknown document
- **WHEN** a client retrieves an identifier that is unknown or has expired
- **THEN** the API returns HTTP 404 without revealing other document identifiers

### Requirement: Return complete page results
The system SHALL return completed pages in source order with a zero-based index and Markdown-compatible content for each source page.

#### Scenario: Completed document
- **WHEN** a client retrieves a completed document
- **THEN** the API returns HTTP 200 with every page, the final Markdown summary, and `completed` status

#### Scenario: Empty page among populated pages
- **WHEN** a completed document contains a page with no extracted text
- **THEN** the API returns that page at its source index with empty content

### Requirement: Return safe failure details
The system SHALL expose a stable machine-readable error code and user-safe message for failed documents without provider payloads, stack traces, credentials, or internal connection details.

#### Scenario: Failed document
- **WHEN** a client retrieves a failed document
- **THEN** the API returns HTTP 200 with `failed` status, error code, safe message, and no partial pages or summary

#### Scenario: Provider error contains sensitive details
- **WHEN** a provider failure includes request metadata or credential-bearing content
- **THEN** the persisted and returned failure omits those details

### Requirement: Provide health status
The system SHALL expose liveness and readiness endpoints that distinguish a running API process from its ability to accept work.

#### Scenario: API is alive but Redis is unavailable
- **WHEN** the API process is running and cannot reach Redis
- **THEN** liveness succeeds while readiness returns an unhealthy status

#### Scenario: Required Gemini configuration is absent
- **WHEN** the API process is running without Gemini credentials
- **THEN** liveness succeeds while readiness reports that submissions are unavailable without exposing configuration values

#### Scenario: Required dependencies are available
- **WHEN** Redis is reachable and required submission configuration is present
- **THEN** both liveness and readiness succeed

### Requirement: Expose scheduled provider retries

A processing result SHALL optionally include a safe `retry_wait` object when a provider-related retry has been durably scheduled. It SHALL describe the not-before time and the confirmed failure that caused the wait without publishing extracted pages or a partial summary. The server statuses SHALL remain `queued`, `processing`, `completed`, and `failed`.

#### Scenario: A provider retry is scheduled

- **WHEN** lookup finds a live processing document with a persisted provider retry deadline
- **THEN** it returns HTTP 200 with `status: processing`, the existing admitted attempt count, and `retry_wait.until` plus its typed safe cause

#### Scenario: An attempt has resumed

- **WHEN** the next attempt has been admitted
- **THEN** lookup no longer returns the previous retry-wait object

#### Scenario: No retry is scheduled

- **WHEN** a document is queued, terminal, or processing without a confirmed scheduled provider retry
- **THEN** lookup omits `retry_wait`
- **AND** it does not invent a provider wait or a future retry time

#### Scenario: Legacy processing record

- **WHEN** a stored processing document has no new metadata fields
- **THEN** its existing response remains valid without optional fields being required

#### Scenario: Result cannot be retrieved

- **WHEN** a polling request fails or the document has expired
- **THEN** the existing transport-error or HTTP 404 behavior applies rather than a fabricated provider retry state

### Requirement: Expose confirmed provider failure causes

A failed result SHALL retain its existing error code and provide a safe explanatory message. It SHALL optionally include `error.cause` with an allowlisted provider category, processing stage, failed attempt, and known HTTP status. Exhaustion SHALL retain `retry_exhausted`. Metadata from an earlier attempt SHALL be identified as last-confirmed evidence. No raw provider response or credential-derived identifier SHALL be exposed.

#### Scenario: Gemini returns a quota rejection

- **WHEN** retry exhaustion follows a recorded Gemini HTTP 429
- **THEN** the result identifies an API quota or rate-limit rejection
- **AND** it does not claim that a specific request or token quota was exceeded without supporting evidence

#### Scenario: Gemini is unavailable

- **WHEN** retry exhaustion follows a recorded Gemini HTTP 503
- **THEN** the result identifies temporary provider unavailability at the recorded extraction or summarization stage
- **AND** it does not describe the PDF as invalid or claim a global outage

#### Scenario: The provider request has no HTTP response

- **WHEN** the recorded cause is a timeout or transport failure
- **THEN** the response identifies that category without inventing an HTTP status or overload diagnosis

#### Scenario: Only an earlier failure was recorded

- **WHEN** the exposed cause's attempt precedes the document's final admitted attempt
- **THEN** the response message identifies it as the last confirmed failure on that earlier attempt

#### Scenario: Failure details are absent or invalid

- **WHEN** a legacy or interrupted job has no trustworthy optional cause
- **THEN** lookup omits the cause and returns the safe generic error
- **AND** it does not infer a provider status from `retry_exhausted` alone

#### Scenario: A document or response fails permanently

- **WHEN** an invalid PDF, empty extraction, or rejected Gemini output causes terminal failure
- **THEN** the existing permanent error code and message remain authoritative
- **AND** an unrelated earlier retry cause is not attached to that failure

#### Scenario: Provider metadata contains sensitive content

- **WHEN** an exception contains raw response text, headers, document content, request identifiers, quota dimensions, or credentials
- **THEN** only validated allowlisted fields enter stored and returned context
- **AND** arbitrary strings and dictionaries do not cross the result boundary

#### Scenario: An ordinary API error is returned

- **WHEN** submission validation, readiness, or another non-document-result endpoint returns an error
- **THEN** its existing error envelope is unchanged and gains no provider-cause field
