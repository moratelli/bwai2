# Proposal

## Why

The application needs a reliable way to accept PDFs without tying up HTTP requests while extraction and AI summarization run. It should be easy to review locally: one Docker Compose command starts the complete stack, and the repository carries the same formatting, testing, and build commands used in CI.

## What changes

- Add a multi-file upload API with a parser choice per batch: PyPDF, Google Gemini Flash, or Mistral OCR.
- Validate PDF uploads and create one independently tracked document for each accepted file.
- Process documents outside the API request through a Redis Streams consumer group.
- Extract page-ordered text or Markdown, then generate one Markdown summary per document with Gemini.
- Persist document state, page content, summaries, and safe failure details in Redis with a documented retention period.
- Add polling endpoints and a React interface for submitting files, following progress, and reading page content and summaries.
- Package the API, worker, frontend, and Redis as a Docker Compose application with health checks.
- Add a root Makefile, uv-managed Python checks, Oxc/TypeScript/Vitest frontend checks, and GitHub Actions CI.
- Document local setup, external API configuration, parser trade-offs, curl examples, and known extraction limitations.

## Capabilities

### New capabilities

- `document-submission`: Multi-file PDF validation, parser selection, document creation, and initial queueing behavior.
- `asynchronous-document-processing`: Redis Streams delivery, parser-specific page extraction, Gemini summarization, retries, and terminal state handling.
- `document-results`: Pollable document status and retrieval of page-ordered Markdown, summary, metadata, and safe errors.
- `web-experience`: Browser-based upload, progress tracking, failure display, and readable document results.
- `local-delivery`: Reproducible local container startup and repository-owned development and CI quality gates.

### Modified capabilities

None. This is a greenfield repository.

## Impact

- New FastAPI API and worker services under `backend/`.
- New React and TypeScript application under `frontend/`.
- Redis 7+ becomes the queue and result store.
- Google Gemini is required for summaries and optional Gemini parsing; Mistral credentials are required only when Mistral OCR is selected.
- Docker Compose, a root Makefile, lockfiles, tests, and GitHub Actions become part of the repository contract.
- Uploaded documents and extracted content are retained temporarily in Redis; production-grade durable object storage and AWS deployment are outside this take-home scope.
