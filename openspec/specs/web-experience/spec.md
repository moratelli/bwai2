# web-experience Specification

## Purpose
Defines a focused browser workflow for uploading PDF batches, following asynchronous progress, and reading summaries and page content safely.

## Requirements

### Requirement: Configure a PDF batch
The web application SHALL present intent-based processing modes, default to Fast text (`pypdf`), and require an explicit user choice before submission without silently changing parsers.

#### Scenario: User selects multiple PDFs
- **WHEN** a user selects valid PDF files
- **THEN** the page lists the filenames and keeps the batch ready for submission

#### Scenario: Default processing mode
- **WHEN** the upload page first loads
- **THEN** Fast text is selected and identifies PyPDF as the provider suitable for normal digital PDFs

#### Scenario: User selects a parser
- **WHEN** a user chooses Fast text or Rich Markdown
- **THEN** the page identifies its PyPDF or Gemini provider and explains speed, quality, credential, privacy, and scanned-page trade-offs

#### Scenario: Browser rejects an obvious invalid selection
- **WHEN** a user selects a non-PDF file or exceeds the documented file count or size limit
- **THEN** the page explains the limit and does not submit the batch

### Requirement: Show asynchronous progress

The web application SHALL display each accepted document independently and poll until it reaches a terminal state or its resource returns HTTP 404. An unavailable resource SHALL be a terminal local retrieval state, distinct from a server-reported processing failure.

#### Scenario: Batch is accepted

- **WHEN** the upload API accepts a batch
- **THEN** the page creates one status card per returned document and begins polling

#### Scenario: Document remains in progress

- **WHEN** a poll returns `queued` or `processing`
- **THEN** the card displays the current state without showing stale result content

#### Scenario: Polling request fails temporarily

- **WHEN** a status poll encounters a temporary network or server failure
- **THEN** the page shows a non-destructive connection message and retries with a bounded delay

#### Scenario: Polling returns HTTP 404

- **WHEN** a tracked document's resource returns HTTP 404
- **THEN** its card explains that the document was not found or has expired, stops claiming it is processing, and schedules no further polls for that identifier

#### Scenario: One unavailable document shares a batch with active or completed documents

- **WHEN** one document becomes unavailable while other documents remain active or completed
- **THEN** the page preserves completed results and continues polling only the active documents

#### Scenario: The user submits another document after a 404

- **WHEN** a new submission is accepted after an earlier resource became unavailable
- **THEN** polling starts for the new identifier without reviving the unavailable resource

#### Scenario: Transport response contains a client-only status

- **WHEN** the API response contains the local unavailable status instead of a supported server status
- **THEN** the transport validator rejects it rather than extending the server contract

### Requirement: Render completed results safely
The web application MUST render the summary and page content as readable Markdown without executing embedded HTML or scripts.

#### Scenario: Document completes
- **WHEN** polling returns a completed document
- **THEN** the card displays its summary first and offers page-by-page content in source order

#### Scenario: Generated Markdown contains raw HTML
- **WHEN** a summary or page contains HTML or script-like content
- **THEN** the browser treats it as non-executable content

### Requirement: Explain document failures
The web application SHALL show a failed document's safe error message without discarding completed results from other files in the batch.

#### Scenario: One document in a batch fails
- **WHEN** one document reaches `failed` while another reaches `completed`
- **THEN** the page shows the failure and the successful document result independently

#### Scenario: PyPDF finds no text
- **WHEN** a Fast text document fails because every page is empty
- **THEN** the page explains that Fast text cannot read scanned-only pages, suggests selecting Rich Markdown (Gemini) before resubmitting without guaranteeing scan quality, and does not switch parsers automatically

#### Scenario: Upload request is rejected
- **WHEN** the upload API rejects the batch
- **THEN** the page displays the returned safe validation message and keeps the user's selection available for correction

### Requirement: Support baseline accessibility
The web application SHALL provide keyboard-operable controls, associated labels, visible focus states, status announcements, and sufficient semantic structure for the upload and results workflow.

#### Scenario: Status changes during polling
- **WHEN** a document status changes
- **THEN** assistive technology can detect the updated status without moving keyboard focus

#### Scenario: User navigates without a pointer
- **WHEN** a user operates the page by keyboard
- **THEN** file selection, parser choice, submission, and result expansion remain operable with visible focus
