# Tasks

## 1. Explain rejected responses

- [x] 1.1 Add evidence-based Gemini response messages without changing the error code, requests, retries or completeness policy; land the regression tests and README explanation together. Write and run the failing adapter tests first, implement the smallest passing change, then verify the full boundary and project gates below before committing code, tests, docs and this task's evidence together.
  - Cover `MAX_TOKENS` precedence (including empty, malformed and apparently complete text), missing/empty/whitespace text, malformed JSON, invalid schema including an empty page array, too few/many pages, duplicate/one-based/gapped/reordered indexes, missing/unrecognized finish metadata and unused candidate metadata. Include actual SDK response objects and a synthetic valid 240-page response. Keep existing transport-error and successful-extraction tests.
  - Exercise the actual adapter through processing/result tests: preserve `invalid_provider_response`, persist the specific safe message, skip summarization and publish no partial pages. Include synthetic sensitive strings in rejected payloads and metadata; verify that errors, result JSON and emitted logs do not contain them. Reuse the existing App failure-message rendering test; no frontend behavior change is expected.
  - Document single-response extraction, the upload-size/output-capacity distinction and the limits of the small quality fixture. Do not claim a verified cause for historical failures or add an arbitrary page limit. Use no customer PDF, text, filename or local path in artifacts.
  - Focused GREEN: `uv run --project backend pytest backend/tests/unit/test_gemini_parser.py backend/tests/unit/test_processing.py backend/tests/api/test_document_results.py backend/tests/unit/test_worker_logging.py backend/tests/integration/test_gemini_response_failures.py`; `uv run --project backend ruff check backend scripts`; `uv run --project backend ruff format --check backend scripts`; `uv run --project backend mypy backend/src backend/tests`; `uv run --project backend mypy --strict scripts`.
  - After the last implementation edit, run `TEST_REDIS_URL=<disposable Redis 7 URL> make check` with no real-Redis skips. Run `make smoke` and `make browser-e2e` with an empty `GEMINI_API_KEY`, test summarization, a new `COMPOSE_PROJECT_NAME` and an unused `APP_PORT` with matching `APP_URL` and `PLAYWRIGHT_BASE_URL`. Remove only those test resources and confirm the owner stack is unchanged. Probe changed source files with LSP and inspect session diagnostics; record inconclusive/advisory outcomes separately from project gates. Run `git diff --check`.

## Verification evidence

- 2026-10-25: the old adapter produced 23 expected failures and 18 passing preservation checks across the new unit/integration matrix. After implementation, all 64 focused tests passed. The additional unknown-metadata success case is a preservation check.
- Fresh `make check` passed with a disposable Redis 7 instance: 330 backend tests, no skips; 23 frontend tests; API drift, Ruff format/lint, both mypy targets, frontend format/lint, both TypeScript checks and both builds passed.
- HTTP and ten-client concurrency smoke passed. The first browser invocation used its default port because `PLAYWRIGHT_BASE_URL` was missing; it failed before reaching the app. Setting that variable to the disposable URL fixed the invocation, and Chromium passed. No application change was needed for that setup error.
- All disposable test containers and volumes were removed. Existing container IDs and private `.env` size/mtime/inode metadata remained unchanged. No provider calls or customer-document copies were made.
- Targeted diagnostics found no language/type errors. Nine auxiliary `no-ellipsis-body` warnings describe intentional Protocol declarations; no source suppression or unrelated cleanup was added. The session error view was empty apart from its existing disposition caveat. `git diff --check` passed.

## Final review evidence

At reviewed head `82b48d881a3c33a064aa4fa62aaf626457023bb1`, report-only OpenSpec verification confirmed the completed task, the added requirement, all 10 scenarios and both design decisions. No semantic checks were skipped and no gaps were found. Fresh-context GPT-5.6 Sol high and Claude Opus 4.8 high reviews both returned `[]`. This was a normal, scoped runtime change; Pedro explicitly requested both reviewers. Final verdict: PASS, with no findings to fix or defer. Strict active validation passed all 6 items. The approved spec synchronization is the remaining archive action; runtime verification above remains valid because only this evidence record changes after review.

## Workflow follow-up

- Run the installed report-only `openspec-verify-change` workflow against this active change.
- At Pedro's request, run one fresh-context Sol high and one Opus high final review in parallel over the immutable implementation diff and relevant callers. Apply the normal single fix-wave policy for demonstrated critical/high findings; do not add an unrelated whole-platform review.
- Run `openspec validate --all --strict --no-interactive` while active, then `openspec archive clarify-gemini-response-errors -y` under the completed-change approval. Verify exact delta synchronization and strict validation afterward.
- Rebuild and inspect the source ZIP, push `dev`, verify exact-head CI and update existing PR #1. Leave it open without auto-merge; do not update `main` or rebuild the existing owner stack. Do not change the default PR title/body.
- Do not run live provider checks or resubmit the internal document. This change fixes diagnostic ambiguity, not the historical extraction failure or large-document support.
