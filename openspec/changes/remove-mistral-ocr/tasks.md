# Tasks

## 1. Narrow the application parser contract

- [x] 1.1 Add and run failing backend and frontend tests for the two-value parser/OpenAPI enums, HTTP 422 rejection of `mistral` before job creation, only Fast text and Rich Markdown in the UI, rejection of `mistral` in decoded responses, and conditional PyPDF-specific no-text guidance. Remove Mistral from the domain, submission checks, runtime settings/assembly, adapter, frontend mode list, decoder, and obsolete tests; regenerate `frontend/openapi.json` and `frontend/src/generated/api.ts`. Commit the backend contract, generated files, frontend consumers, and tests together only after `make check-api`, focused backend/Vitest tests, Ruff, strict mypy, Oxfmt, Oxlint, and both frontend TypeScript checks pass.
  - Evidence: RED recorded 8 backend and 5 frontend assertion failures before production edits. GREEN passed 50 focused backend and 14 UI/decoder tests. `make format-check lint typecheck check-api`, self-review, and `git diff --check` passed. Validation errors now list only the supported enum values.
- [ ] 1.2 Remove the unused Mistral SDK and its mypy override through uv and project configuration; verify locked dependency installation, the absence of Mistral from the lockfile, and the relevant backend checks.

## 2. Remove obsolete delivery wiring

- [ ] 2.1 Record a failing absence check for Mistral in the tracked Compose configuration and environment example, remove those variables, and verify the check passes and Compose configuration renders without displaying credentials. Leave existing private `.env` files untouched.
- [ ] 2.2 Add and run a deterministic failing check that the live-provider harness submits only a generated PDF with `parser=gemini` and retains ordered non-empty page and summary assertions; reduce the harness to that flow and update the Makefile and README. Verify the focused check, shell syntax, and documented command/target consistency.

## 3. Verify the reduced delivery

- [ ] 3.1 Run `make check`, actionlint, targeted LSP diagnostics, and `git diff --check`; verify all static checks, tests, generated-contract drift checks, and builds pass. Confirm tracked runtime, dependency, configuration, and user-facing files have no active Mistral references; OpenSpec records and explicit rejection regression tests are allowed.
- [ ] 3.2 Use a disposable test stack for the provider-free Compose HTTP smoke and Playwright Chromium workflow, then run the live Gemini target with the generated PDF and existing Gemini configuration. Verify the browser result, ordered non-empty Gemini pages, and non-empty Gemini summary without inspecting credentials or logging provider content; remove only the test stack's disposable volumes.
- [ ] 3.3 Build and inspect the tracked source ZIP; verify integrity, required lockfiles and Playwright/live-check files, and exclusion of `.git`, `.pi`, `.env`, virtual environments, dependencies, caches, reports, and build output.

## Workflow follow-up

- Run report-only OpenSpec verification and the final implementation review.
- Strictly validate and archive `remove-mistral-ocr` after all tracked tasks pass.
- Push updates to PR #1, wait for required CI checks, and create the reviewed source ZIP from the `dev` revision. Leave PR #1 open for Pedro's requested targeted review. Do not squash, merge, or update `main` until he separately authorizes that delivery step.
