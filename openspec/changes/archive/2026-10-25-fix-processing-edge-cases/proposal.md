# Proposal

## Why

The whole-submission review found three concrete gaps: a PyPDF limit exception can escape the per-document failure boundary, a confirmed truncated Gemini summary can be published as complete, and acknowledged queue entries remain in Redis indefinitely. Fix these without expanding document-processing capacity or rebuilding the delivery architecture.

## What Changes

- Translate relevant `PyPdfError` exceptions, including `LimitReachedError`, into the existing safe permanent PDF error in both local extraction and Gemini's local validation. Preserve encrypted-PDF handling and isolate valid sibling jobs.
- Reject a summary when the consumed Gemini candidate reports `MAX_TOKENS`, including section, reduction and final responses. Preserve the existing error code, normal metadata fallback and request/retry policy.
- Acknowledge and delete eligible stream entries together. Delete only the message being acknowledged after terminal publication or observation of a terminal/absent document; leave unfinished work recoverable.
- Document one logical consumer group per processing stream and the new retention behavior. Existing acknowledged history is not scanned or purged.
- Add synthetic regressions and disposable Redis 7 coverage. Use no customer documents or live provider calls.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `asynchronous-document-processing`: reject confirmed truncated summaries and reclaim acknowledged queue entries. The PDF exception fix restores the existing permanent-parser-failure requirement rather than introducing a new parser policy.

## Impact

Runtime changes are confined to the two PDF validation adapters, the Gemini summarizer and the Redis queue adapter. Tests and README will describe the corrected behavior. HTTP/generated contracts, dependencies, prompts, model settings, worker concurrency, lease fencing and document-result retention remain unchanged.

Queue entries cease to serve as retained history after acknowledgement; the processing stream is a work queue, not an audit log. All worker instances must share its single consumer group. No deployment, historical-data cleanup, multi-group support, batching, new page limits, extra telemetry or broader quality-evaluation work is included.

This change will receive one plan review and human approval before runtime edits. PR #1 stays open and unmerged; `main`, the existing stack and private environment remain untouched.
