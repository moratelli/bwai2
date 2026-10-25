# Result metadata delta

## ADDED Requirements

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
