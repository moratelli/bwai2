# Proposal

## Why

Gemini extraction currently reports the same page-structure error for empty output, invalid JSON, schema errors and page mismatches. A failed large-document submission exposed this diagnostic gap; its exact provider failure is unknown. We should explain the rejection without claiming that the upload was too large or expanding extraction scope.

## What Changes

- Keep `invalid_provider_response`, the existing HTTP/result schema, and permanent-failure behavior. Give each observable rejection a specific, safe message.
- Inspect the consumed candidate's `MAX_TOKENS` finish reason before validating its text. Do not infer an output-limit failure from malformed JSON or missing pages alone.
- Continue requiring every source page in zero-based order; never publish partial extraction or invoke summarization after a rejected response.
- Document that Gemini extraction uses one response and that the small quality fixture does not establish large-document reliability.
- Use synthetic SDK responses for regression tests. Do not add batching, retries, arbitrary page limits, new telemetry fields, provider requests or customer-document fixtures.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `asynchronous-document-processing`: explain rejected Gemini extraction responses while retaining strict completeness and existing failure contracts.

## Impact

The Gemini adapter, its tests, failure-propagation/display tests where needed, and README are affected. Worker logging, persistence, retries, summarization, generated contracts, dependencies and deployment remain unchanged. Error messages become more specific; clients can continue using the existing error code.

Pedro requested this narrow fix and another independent review. The complete change will receive one plan review and approval before runtime edits, then Sol and Opus final reviews. PR #1 must remain unmerged; the existing stack, private environment and `main` must not be updated.
