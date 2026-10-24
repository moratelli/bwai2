# document-submission delta

## MODIFIED Requirements

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

## REMOVED Requirements

### Requirement: Validate parser availability
**Reason**: The former requirement includes a credential check for the retired Mistral parser. `Validate supported parser availability` replaces it with the two supported parser values.
**Migration**: Clients must submit `pypdf` or `gemini`. Submitting `mistral` returns HTTP 422 regardless of former Mistral credential settings.

## ADDED Requirements

### Requirement: Validate supported parser availability
The system SHALL accept only supported parser values and SHALL reject a parser whose required provider configuration is unavailable.

#### Scenario: Unknown parser
- **WHEN** a client submits a parser value outside `pypdf` and `gemini`
- **THEN** the API returns HTTP 422 with the supported values

#### Scenario: Missing Gemini configuration
- **WHEN** Gemini credentials required for summarization are not configured
- **THEN** the API returns HTTP 503 for every submission without creating document jobs
