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
