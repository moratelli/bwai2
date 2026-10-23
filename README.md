# Async document processor

A take-home application for uploading PDF files, processing them outside the HTTP request, and reading page-level content with a generated summary.

Development happens on `dev`. The `main` branch remains the delivery target.

## Submission API

`POST /api/v1/documents` accepts a multipart batch and responds with one queued document resource per file. Every file in a batch uses the same parser.

The supported parser values are:

- `pypdf`: fast local text extraction for ordinary digital PDFs. This is the default mode in the web application.
- `gemini`: AI-assisted extraction into Markdown.
- `mistral`: OCR-oriented extraction for scanned or image-heavy PDFs.

Summaries always use Gemini, including documents parsed with PyPDF or Mistral. `GEMINI_API_KEY` is therefore required for all submissions. `MISTRAL_API_KEY` is needed only when `mistral` is selected.

Once the API is running locally, submit one file with:

```bash
curl --fail-with-body \
  -X POST http://localhost:8000/api/v1/documents \
  -F 'parser=pypdf' \
  -F 'files=@./sample.pdf;type=application/pdf'
```

Submit several files by repeating the `files` field:

```bash
curl --fail-with-body \
  -X POST http://localhost:8000/api/v1/documents \
  -F 'parser=mistral' \
  -F 'files=@./scan-one.pdf;type=application/pdf' \
  -F 'files=@./scan-two.pdf;type=application/pdf'
```

The default limits are ten files per batch and 20 MiB per file. The API rejects non-PDF media types, files without a PDF signature, unsupported parser values, and submissions whose required provider is not configured. Validation happens before the batch is published.

FastAPI serves the interactive OpenAPI documentation at `http://localhost:8000/docs` during backend development.

## Configuration

Copy `.env.example` to `.env` for local container use, then add provider keys to the untracked file. Do not put credentials in source code, frontend variables, image build arguments, or commits.

The complete setup, Compose workflow, architecture, tests, parser trade-offs, and result polling examples will be documented as their implementations land.
