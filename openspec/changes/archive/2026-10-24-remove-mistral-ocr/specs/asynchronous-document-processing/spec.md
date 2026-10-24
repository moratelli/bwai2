# asynchronous-document-processing delta

## ADDED Requirements

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

## REMOVED Requirements

### Requirement: Extract ordered page content
**Reason**: The former requirement includes the retired Mistral success path. Its replacement, `Extract ordered content with supported parsers`, preserves PyPDF and Gemini behavior.
**Migration**: Submit documents with `pypdf` or `gemini`; page ordering and Markdown output remain unchanged.

## MODIFIED Requirements

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
- **THEN** the document fails without invoking another parser and explains that no readable text was extracted, with conditional guidance for Fast text users to select Rich Markdown (Gemini) or use a text-based PDF
