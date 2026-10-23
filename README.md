# Async document processor

A take-home application for uploading PDF files, processing them outside the HTTP request, and reading page-level content with a generated summary.

Development happens on `dev`. The `main` branch remains the delivery target.

## Submission API

`POST /api/v1/documents` accepts a multipart batch and responds with one queued document resource per file. Every file in a batch uses the same parser.

The processing modes are:

| Mode | API value | Use it for | Trade-offs |
| --- | --- | --- | --- |
| **Fast text / PyPDF** (default) | `pypdf` | Normal digital PDFs with selectable text | Fast and local, with no parser usage cost. It does not OCR scanned pages. The PDF stays local, but its extracted text is sent to Gemini for the final summary. |
| **Rich Markdown / Gemini** | `gemini` | Complex layouts, tables, and richer Markdown | Sends the PDF to Google. Results can vary because extraction is generative, and the request adds provider latency and usage cost. Requires `GEMINI_API_KEY`. |
| **Scanned document / OCR / Mistral** | `mistral` | Scans and image-heavy PDFs | Sends the PDF to Mistral for OCR, then sends the extracted text to Gemini for summarization. This path needs both provider keys and can add the most latency and cost. |

The application honors the selected mode and never switches providers silently. If Fast text finds no text, resubmit the document with Scanned document / OCR rather than expecting an automatic fallback.

Every successful document receives one Gemini summary, regardless of the parser. `GEMINI_API_KEY` is therefore required for all submissions. `MISTRAL_API_KEY` is required only for the OCR mode. The configurable defaults are `gemini-3.1-flash-lite` and `mistral-ocr-latest`.

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
