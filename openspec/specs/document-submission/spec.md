# document-submission Specification

## Purpose
Defines how clients submit one or more PDF files, select one parser, and receive independently trackable asynchronous document jobs.

## Requirements

### Requirement: Submit a PDF batch
The system SHALL accept between one and ten PDF files in one multipart request, apply one parser choice to the batch, and create one document job per file.

#### Scenario: Valid multi-file submission
- **WHEN** a client submits valid PDF files with parser `pypdf` or `gemini`
- **THEN** the API returns HTTP 202 with one unique document identifier and initial `queued` status per file

#### Scenario: Empty submission
- **WHEN** a client submits no files
- **THEN** the API rejects the request with HTTP 422 and creates no document jobs

#### Scenario: Too many files
- **WHEN** a client submits more than ten files in one request
- **THEN** the API rejects the request with HTTP 413 and creates no document jobs

### Requirement: Validate uploaded PDFs
The system MUST reject files that exceed 20 MiB, lack a PDF signature, or use a non-PDF media type before creating any job in the batch.

#### Scenario: Oversized file
- **WHEN** any uploaded file exceeds 20 MiB
- **THEN** the API returns HTTP 413 with a safe validation message and creates no jobs for the batch

#### Scenario: Spoofed PDF upload
- **WHEN** any uploaded file claims to be a PDF but does not begin with a PDF signature
- **THEN** the API returns HTTP 422 and creates no jobs for the batch

#### Scenario: Invalid media type
- **WHEN** any uploaded file has a media type other than `application/pdf`
- **THEN** the API returns HTTP 422 and creates no jobs for the batch

### Requirement: Queue accepted documents
The system SHALL persist accepted document metadata and payloads before publishing document identifiers for asynchronous processing.

#### Scenario: Queue publication succeeds
- **WHEN** all files pass validation and queue publication succeeds
- **THEN** every returned document identifier resolves to persisted metadata and a queued work item

#### Scenario: Queue publication fails
- **WHEN** the queue or persistence layer is unavailable during submission
- **THEN** the API returns HTTP 503 and does not report the batch as accepted

### Requirement: Validate supported parser availability
The system SHALL accept only supported parser values and SHALL reject a parser whose required provider configuration is unavailable.

#### Scenario: Unknown parser
- **WHEN** a client submits a parser value outside `pypdf` and `gemini`
- **THEN** the API returns HTTP 422 with the supported values

#### Scenario: Missing Gemini configuration
- **WHEN** Gemini credentials required for summarization are not configured
- **THEN** the API returns HTTP 503 for every submission without creating document jobs
