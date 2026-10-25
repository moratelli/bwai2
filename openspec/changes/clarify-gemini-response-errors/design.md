# Design

## Context

See `proposal.md` for the problem and scope. At base `7e262b33eb98dff1aaa1ae6bcee006a7d1eb1b41`, the Gemini adapter catches all text/schema/index validation errors behind one message. The running worker has the same parser source as this base. No captured response establishes why the reported large-document job failed.

The installed `google-genai` version is 1.75.0. A local construction of its `GenerateContentResponse` confirmed optional `candidates`, optional candidate `finish_reason`, and the `MAX_TOKENS` enum. Its text accessor remains available for a response carrying that reason. This evidence required no API request.

## Goals / Non-Goals

Explain what can be observed, not why the provider behaved that way. Keep the existing error code, result shape, completeness policy and failure pipeline. Do not build a new diagnostics subsystem, change prompts or generation budgets, or attempt to make large documents succeed. The original failed records will not be rewritten.

## Decisions

### 1. Classify at the existing adapter boundary

Inspect only the finish reason of the candidate whose text is consumed (the first candidate for the current SDK response). A confirmed `MAX_TOKENS` takes precedence over content validation. Missing or unrecognized metadata does not prove truncation; validate the text as before. Do not change provider exception mapping, requests, model selection or retry rules.

Use the following order and messages. All cases remain `PermanentProcessingError` with code `invalid_provider_response`.

| Evidence | Message |
| --- | --- |
| Consumed candidate reports `MAX_TOKENS` | Gemini reached its response limit before a complete extraction could be confirmed. Try a smaller PDF. |
| Text is absent, empty or whitespace | Gemini returned no extraction output. |
| JSON syntax is invalid | Gemini returned malformed JSON for the extracted pages. |
| JSON does not satisfy the existing page schema | Gemini returned an invalid page structure. |
| Schema-valid page count differs | Gemini page count mismatch: expected {expected}, received {returned}. The result was rejected. |
| Count matches but indexes are not exactly zero-based source order | Gemini returned invalid page numbering or order. The result was rejected. |

Retain the schema minimum of one page: an empty page array is a schema error, not absent response text. Distinguish JSON syntax from other Pydantic errors using error types, never by echoing exception text or inputs. The only interpolated values are integer counts. A response reporting an output limit is rejected even if its JSON appears complete, since structural validity cannot confirm complete transcription.

New error codes or metadata fields would expand the public contract without helping this small fix. Keep the existing code and use the human-readable message for diagnosis. Logging remains unchanged and does not retain raw finish-reason strings, payloads or validation traces.

### 2. Prove the boundary without another provider request

Use synthetic responses, including actual SDK response objects, to cover classification and precedence. A synthetic 240-page valid response proves no new validation cutoff; it does not prove that a real model can extract a document of that size. Exercise the real adapter through processing/result tests to prove safe message propagation, no summarization and no partial publication. Check the existing UI message path without adding frontend behavior.

README will explain single-response extraction, the distinction between upload bytes and response capacity, and the limited scope of the two-page quality check. It will not state an unverified model token limit or invent a supported maximum page count. Keep customer files, filenames, text and local paths out of the repository and test artifacts.

## Architecture impact

This changes user-visible failure wording in `asynchronous-document-processing`. It does not alter module boundaries, domain terms, persistence, lease ownership, retry admission, summarization or deployment. The adapter and existing error pipeline remain responsible for their current concerns. No context document, ADR or architecture map is required. The existing canonical specs remain unchanged until the approved archive step.

## Risks / Trade-offs

- Coarse machine-readable error code remains → intentional compatibility trade-off; the safe message distinguishes observable cases.
- Metadata cannot explain every failure → classify only confirmed evidence; do not promise retrospective diagnosis or successful large-document extraction.
- Validation exceptions contain provider input → never expose their messages/inputs; test errors, persisted results and emitted logs with synthetic canaries.
- Existing workers retain old behavior → verification uses disposable resources; do not rebuild the owner's stack or modify private configuration.

## Migration Plan

No schema or data migration. New messages apply to new processing after deploying the updated worker; old failures retain their stored message. Reverting the adapter restores prior wording. Deployment to the existing owner stack and any live provider run require separate authorization.

## Approval

Pedro approved the completed change on 2026-10-25 through the `approve_diagnostics_change` confirmation after the Opus plan review of `21f62a64172112216e526b8e8fd93f2c647ca0d9` returned `[]`. This approval covers implementation, verification, requested Sol/Opus final reviews, archive and PR #1 update, but not merge, a main update, live provider calls or a rebuild of the existing stack.
