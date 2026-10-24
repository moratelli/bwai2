# Spec Delta

## Purpose

Defines reliable background delivery, parser-specific page extraction, Gemini summarization, retry behavior, and document state transitions.

## ADDED Requirements

### Requirement: Process outside the request lifecycle
The system SHALL consume accepted document jobs through a Redis Streams consumer group without holding the upload request open for extraction or summarization.

#### Scenario: Worker consumes queued work
- **WHEN** a worker receives an accepted stream entry
- **THEN** the document moves from `queued` to `processing` before parser work begins

#### Scenario: Worker restarts with pending work
- **WHEN** a worker becomes unavailable after claiming an unacknowledged entry
- **THEN** another worker can reclaim and continue the stale entry

### Requirement: Make processing idempotent
The system MUST prevent at-least-once stream delivery from producing conflicting duplicate results for the same document.

#### Scenario: Completed entry is delivered again
- **WHEN** a worker receives an entry for a document already in `completed`
- **THEN** the worker performs no provider calls and acknowledges the entry

#### Scenario: Failed entry is delivered again
- **WHEN** a worker receives an entry for a document already in terminal `failed` state
- **THEN** the worker performs no provider calls and acknowledges the entry

### Requirement: Extract ordered page content
The system SHALL store one page result per PDF page in ascending zero-based page order and format each page as Markdown-compatible text.

#### Scenario: PyPDF parser succeeds
- **WHEN** a text-based PDF is processed with `pypdf`
- **THEN** each PDF page produces a corresponding page result whose extracted text is safe to render as Markdown text

#### Scenario: Gemini parser succeeds
- **WHEN** a PDF is processed with `gemini`
- **THEN** Gemini returns structured page-level Markdown that is validated before storage

#### Scenario: Mistral parser succeeds
- **WHEN** a PDF is processed with `mistral`
- **THEN** Mistral OCR page Markdown is normalized and stored in source-page order

#### Scenario: Page has no extractable PyPDF text
- **WHEN** PyPDF returns no text for a page
- **THEN** the page remains present with empty content rather than being dropped or renumbered

### Requirement: Generate one document summary
The system SHALL use the configured Gemini model to create one Markdown summary from the extracted page content for every successfully parsed document.

#### Scenario: Normal-size document
- **WHEN** the extracted content fits within the configured summary chunk size
- **THEN** Gemini produces one final Markdown summary from the full extracted content

#### Scenario: Large extracted document
- **WHEN** extracted content exceeds the configured summary chunk size
- **THEN** the worker summarizes bounded page groups and synthesizes them into one final Markdown summary

#### Scenario: Empty extracted document
- **WHEN** all extracted pages are empty
- **THEN** the document fails without invoking another parser and explains that a scanned PDF can be resubmitted with Mistral OCR

### Requirement: Handle transient processing failures
The system SHALL retry retryable provider and infrastructure failures up to three total processing attempts and SHALL not retry permanent validation or document errors.

#### Scenario: Temporary provider failure recovers
- **WHEN** a provider returns a retryable rate-limit or server error and a later attempt succeeds
- **THEN** the document reaches `completed` with one stored result

#### Scenario: Retry budget is exhausted
- **WHEN** a retryable operation still fails on the third total attempt
- **THEN** the document moves to `failed` and the stream entry is acknowledged

#### Scenario: Permanent parser failure
- **WHEN** the PDF is corrupt, encrypted, or rejected as unsupported by the selected parser
- **THEN** the document moves directly to `failed` without consuming the remaining retry budget

### Requirement: Publish terminal state atomically
The system MUST store either a complete result or a safe terminal failure before acknowledging the corresponding stream entry.

#### Scenario: Processing succeeds
- **WHEN** extraction and summarization both complete
- **THEN** pages, summary, completion metadata, and `completed` state become observable together before acknowledgement

#### Scenario: Processing fails terminally
- **WHEN** processing cannot continue
- **THEN** a stable error code, safe error message, attempt count, and `failed` state become observable before acknowledgement

### Requirement: Remove transient source data
The system SHALL delete the raw uploaded PDF after terminal processing while retaining metadata and results for 24 hours from submission.

#### Scenario: Document completes
- **WHEN** a document reaches `completed`
- **THEN** its raw PDF is deleted and its result remains available until the retention period ends

#### Scenario: Document fails
- **WHEN** a document reaches `failed`
- **THEN** its raw PDF is deleted and its safe failure metadata remains available until the retention period ends
