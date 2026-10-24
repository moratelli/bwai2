# Design

## Context

The parser boundary is a typed enum shared by submission validation, Redis records, worker dispatch, OpenAPI, generated frontend types, and the browser mode list. The worker registry already treats parsers as independent adapters, so removing one adapter does not change queueing, status transitions, summarization, or deployment topology. See `proposal.md` for motivation and the three delta specs for behavior.

This application has not been released. Redis data is temporary and expires after 24 hours, so there is no production data migration to preserve.

## Goals / non-goals

**Goals:**

- Remove the Mistral path at its API, dependency, runtime, and UI boundaries.
- Keep PyPDF and Gemini behavior unchanged.
- Leave generated OpenAPI and TypeScript contracts reproducible and in sync.
- Retain a live Gemini check that uses an actual PDF through the normal API, queue, worker, extraction, and summary path.

**Non-goals:**

- Add a replacement OCR provider or local OCR engine.
- Preserve compatibility for unreleased clients that submit `parser=mistral`.
- Change Redis Streams delivery, result retention, or Gemini summarization.

## Decisions

### Narrow the parser domain at the source

`ParserKind` will contain only `pypdf` and `gemini`. FastAPI will therefore reject `mistral` during request validation with the same HTTP 422 behavior used for any unsupported parser. OpenAPI and frontend types will be regenerated from that source.

Keeping a deprecated enum member or accepting a no-op parser was rejected because both would advertise behavior the application cannot execute.

The backend enum, generated OpenAPI and TypeScript declarations, frontend decoder, mode list, and their tests land in one task and commit. That task must pass `make check-api`, focused backend and frontend tests, Ruff, strict mypy, Oxfmt, Oxlint, and both frontend TypeScript checks before it is committed. Dependency and delivery cleanup follow without changing the parser contract again.

Two current requirements contain Mistral scenarios. Their deltas explicitly remove the old requirements and add differently named replacements with the surviving behavior, reasons, and migration guidance. This makes the intentional removal visible and satisfies strict validation without retaining a retired success path.

### Delete the integration rather than feature-flag it

The Mistral SDK, adapter, settings, environment forwarding, unit tests, and UI option will be deleted. The generic parser registry remains because it still isolates PyPDF and Gemini.

A hidden feature flag was rejected because it would retain an unverified dependency and dead operational surface in a small take-home project.

### Keep scan guidance specific to PyPDF

PyPDF continues to preserve empty pages. If every extracted page is empty, summarization fails with a safe no-text message. Conditional guidance tells Fast text users that PyPDF cannot read scanned-only pages and suggests selecting Rich Markdown (Gemini) before resubmitting, or using a text-based PDF. The browser does not switch providers automatically. The no-text failure can occur after either parser, so its message must not assume PyPDF was selected.

Gemini remains the existing visual PDF path; this change does not guarantee successful transcription of every scan. The [Gemini document-understanding documentation](https://ai.google.dev/gemini-api/docs/document-processing) describes native vision over PDF text and images, and `GeminiPdfParser.parse` already sends the original PDF bytes. Removing Mistral is not evidence that Gemini rejects scans.

Adding local OCR or another provider is outside this change.

### Keep live verification focused on Gemini

The live-provider target will generate a valid PDF, submit it with `parser=gemini`, wait for terminal completion, and require ordered non-empty page output plus a non-empty summary. It will keep provider content and credentials out of logs. Normal CI remains credential-free and uses the deterministic smoke summarizer.

Container checks use a disposable test stack; teardown must not remove an existing developer stack's data. The live check uses the existing Gemini configuration only after change approval. It does not inspect or edit private `.env` contents, and it sends only the generated test PDF.

## Risks / trade-offs

- [Scan results depend on the selected parser and scan quality] → Explain PyPDF's limitation and the explicit Gemini retry option without promising successful extraction.
- [Existing local Redis records may contain `parser=mistral`] → The app is unreleased and records expire after 24 hours. Document that old test data may need clearing, but do not delete an existing developer volume without confirming that its data is disposable.
- [A narrower parser enum is an API compatibility break] → Change the backend enum, generated contract, and frontend consumers together and test them in the same commit. Unsupported submissions receive HTTP 422.
- [Deleting an optional provider reduces feature breadth] → Prefer two executable, tested modes over a third mode that the available account cannot run.

## Migration plan

1. Add and run failing backend and frontend tests for the two-parser contract and accurate no-text guidance.
2. Remove the backend integration and frontend consumers, regenerate the contract, and pass their focused checks in one commit.
3. Remove the SDK dependency, public configuration examples, Compose variables, live-test branch, and obsolete documentation through the remaining tasks. Keep private `.env` files untouched.
4. Recreate local dependencies from lockfiles, run project checks, provider-free Compose smoke, Playwright, and the live Gemini check in a disposable test stack.
5. If rollback is required before merge, revert the change commit and restore the prior lockfiles and generated API artifacts. No data migration is required.

## Architecture impact

- **Affected boundary/invariant:** The parser adapter boundary and its API-visible `ParserKind` contract shrink from three implementations to two. Queue, persistence, summarization, and deployment boundaries do not change.
- **Proposed impact:** Remove the Mistral adapter and all consumers while preserving the generic registry for PyPDF and Gemini.
- **Spec authority:** `openspec/specs/document-submission/spec.md`, `openspec/specs/asynchronous-document-processing/spec.md`, and `openspec/specs/web-experience/spec.md` define current behavior. `openspec/changes/remove-mistral-ocr/specs/` contains the proposed deltas; these do not replace the current specs before approved synchronization/archive.
- **Context authority:** absent; bounded repository discovery found no `CONTEXT.md` or `CONTEXT-MAP.md`.
- **ADR authority:** absent; bounded repository discovery found no ADR directory or applicable ADR.
- **Architecture authority:** absent; bounded repository discovery found no designated architecture map, `docs/architecture/`, or root `ARCHITECTURE.md`.
- **Change status:** Pedro Moratelli approved implementation and verification of `remove-mistral-ocr` on 2026-10-24, with the delivery restriction: "don't squash and merge yet. i want to run a targeted review afterwards". This approval covers the seven tasks, live Gemini check, spec synchronization/archive, and updating PR #1 after gates pass. Leave the PR open for his targeted review; squash and merge require separate authorization. The Opus plan review passed on the same date after correction of two blockers and a clean targeted follow-up.
