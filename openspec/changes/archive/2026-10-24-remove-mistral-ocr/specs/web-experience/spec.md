# web-experience delta

## MODIFIED Requirements

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
