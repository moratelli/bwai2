# Proposal

## Why

The current Mistral OCR path cannot be exercised with the available provider account because Mistral grants it zero requests per minute. The take-home should ship with features that reviewers can run and verify, so the optional Mistral integration will be removed rather than left as an unproven branch.

## What changes

- **BREAKING**: Remove `mistral` from the document submission API and parser domain. Supported parser values become `pypdf` and `gemini`.
- Remove the Mistral SDK, OCR adapter, credential settings, example variables, model configuration, Compose wiring, tests, and documentation. Existing private `.env` files stay untouched.
- Remove the Scanned document / OCR option and Mistral retry recommendation from the web application. Empty-extraction guidance will explain PyPDF's scan limitation and suggest an explicit retry with Rich Markdown (Gemini), without promising that every scan will succeed.
- Keep the live-provider check for the verified Gemini PDF extraction and Gemini summarization flow.
- Regenerate the OpenAPI document and TypeScript declarations after narrowing the parser enum.

## Capabilities

### New capabilities

None.

### Modified capabilities

- `document-submission`: Restrict accepted parser values and availability checks to PyPDF and Gemini.
- `asynchronous-document-processing`: Remove Mistral OCR extraction and change empty-extraction guidance.
- `web-experience`: Remove the OCR mode and describe the remaining parser trade-offs accurately.

## Impact

This change touches the backend parser domain, runtime configuration, Python dependencies, generated OpenAPI contract, frontend parser choices, container environment, test suites, live-provider script, and reviewer documentation. Existing clients that submit `parser=mistral` will receive the normal unsupported-value HTTP 422 response after the change.
