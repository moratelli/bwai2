# Proposal

## Why

The application can send a burst of Gemini calls while retrying, despite the owner's reported allowance of 15 requests per minute. It also replaces the useful failure cause with `retry_exhausted`, leaving the browser unable to distinguish provider unavailability, quota rejection, and a document problem. An isolated request reproduced Gemini HTTP 503 without Redis or retries; request pacing must therefore accompany honest failure reporting, not a promise that throttling will eliminate provider outages.

## What Changes

- Pace every production Gemini generation request through one Redis-backed budget shared by worker instances, extraction, section summaries, reductions, final synthesis, and retries. Default to 12 requests per minute, leaving headroom below the reported 15 RPM allowance.
- Coordinate a shared provider cooldown, including after a document exhausts its retries. Keep three processing attempts by default, use slower bounded exponential backoff with additive jitter, and respect validated provider retry hints.
- Preserve an allowlisted provider category, HTTP status when known, and extraction/summarization stage. Persist confirmed failure context and scheduled retry information without weakening ownership fencing, terminal immutability, or retention.
- Add optional result metadata for scheduled retries and terminal provider causes while preserving existing document statuses and `retry_exhausted`. Older records remain readable and are not retroactively attributed to Gemini.
- Show accurate provider-specific failure messages, a waiting-to-retry state when the backend confirms it, and batch outcome counts. Never announce success for a missing summary or promise an unscheduled automatic retry.
- Record safe request outcomes and returned token-usage counts. Document the separate 250k input-TPM allowance without claiming that request pacing enforces it.
- Verify the flow using fake provider transports, controlled clocks, disposable Redis, and browser tests. No further live Gemini calls, private-document access, or running-stack changes are part of implementation approval.

## Capabilities

### New Capabilities

None. Extend the existing processing, result, delivery, and browser capabilities.

### Modified Capabilities

- `asynchronous-document-processing`: shared request admission/cooldowns and recoverable, cause-preserving retries.
- `document-results`: optional retry-wait and provider-cause metadata with safe legacy behavior.
- `web-experience`: evidence-based provider feedback, scheduled-wait presentation, and batch outcome counts.
- `local-delivery`: documented provider-budget configuration and safe provider-request diagnostics.

## Impact

The backend provider boundary, worker, Redis repository, result DTOs, runtime settings, and their tests change. The frontend transport validator, generated OpenAPI/TypeScript artifacts, document cards, batch status text, and tests change together. Configuration examples and README guidance are updated; private environment files remain untouched.

The FastAPI/Redis Streams architecture, parser selection, full-document retry unit, strict Gemini response validation, source cleanup, and result retention remain. There is no automatic model fallback, new external service, new production dependency, token-counting API call, proactive TPM limiter, extraction checkpoint, HTTP idempotency redesign, or Redis-history purge. The budget covers workers sharing the configured Redis namespace and model, not other applications using the same Google project.
