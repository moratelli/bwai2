# Design

## Context

See `proposal.md` for motivation and scope. The base is `f87c1ad66d2413b690f0c6486cdff0f5de96a070` in the existing linked `dev` worktree. The review reproduced both processing failures in memory; parent inspection confirmed the relevant code paths and the installed exception hierarchy. No runtime changes have been made for this proposal.

The locked PyPDF 6.21.2 defines `LimitReachedError` under `PyPdfError`, outside the currently caught `PdfReadError`. The installed google-genai 1.75.0 exposes optional candidate finish reasons, while its text accessor can return content after `MAX_TOKENS`. The summarizer currently checks text only. The queue currently calls `XACK` without deleting the entry; all three acknowledgement call sites belong to the worker's existing terminal/absent-document paths.

## Goals / Non-Goals

Keep each correction at its current adapter boundary. Preserve existing error codes, HTTP/result shapes, publication ordering, lease ownership and retry accounting. The canonical permanent-parser-failure requirement already covers the intended PDF behavior; only summary-limit rejection and queue retention need delta requirements.

Do not add a general exception handler to the worker, a shared provider-response framework, PDF batching, new provider budgets, automatic retries for truncation, telemetry fields, a historical queue sweeper or support for multiple consumer groups on one processing stream. Do not edit the live-quality runner or its guarded fixture-test file; they are outside this change.

## Decisions

### 1. Translate PDF-library failures in both existing adapters

Catch `PyPdfError` in `pypdf_parser.py` and `pdf_validation.py`, retaining the existing non-library validation exception handling. Map those library errors to the current permanent `invalid_pdf` message. Preserve the explicit `encrypted_pdf` path and re-raise domain processing errors unchanged. Do not catch every `Exception` or change the worker's retry loop.

This contains the reported library failure before it escapes into the worker pool, without disguising unrelated programming errors. Keeping the correction in both current adapters is smaller than introducing a new validation abstraction.

Write RED tests using a small synthetic malformed-cross-reference PDF and the actual installed exception type. Cover direct PyPDF extraction and Gemini's local validation before any provider request. The worker/result regression must show one permanent failed job, a deleted source, acknowledgement and a valid sibling completing through the same worker. Retain normal, corrupt and encrypted PDF cases. Check fixed error text rather than raw exception details.

### 2. Reject confirmed summary truncation at the shared request boundary

Add optional candidate metadata to the summarizer's response contract and check the first, consumed candidate in `_request`, before reading/accepting its text. This one boundary serves direct summaries, section summaries, reduction passes and final synthesis.

For confirmed `MAX_TOKENS`, raise `PermanentProcessingError` with code `invalid_provider_response` and fixed message:

> Gemini reached its response limit before a complete summary could be confirmed.

Reject even apparently complete text: non-empty content cannot override explicit output-limit metadata. Missing candidates, a missing finish reason and unrecognized values follow existing text validation. An unused candidate's finish reason does not change the outcome. Existing empty-summary messages, transport/provider error mapping, prompts, content budgets, reduction count and model configuration remain unchanged.

Use actual SDK-shaped synthetic responses in the regression matrix, including section/reduction/final stages, metadata fallback, unused alternatives and sensitive-string canaries. Verify that rejected intermediate output causes no further synthesis request, and that the worker persists a safe failed result without partial pages/summary or another attempt. No live provider request is necessary.

### 3. Pair acknowledgement with deletion in one Redis 7 transaction

Keep the existing `RedisStreamQueue.acknowledge(message_id)` boundary and integer acknowledgement result. Buffer `XACK` and `XDEL` for that exact entry in a `redis.asyncio` pipeline with `transaction=True`, execute once, and return the `XACK` count. Repeated cleanup returns zero acknowledgements and remains harmless. Do not use `XTRIM`, delete the stream key, scan historical entries or require a newer Redis command.

Keep this transaction separate from terminal publication. The repository first persists a fenced terminal result and deletes its source. The worker then calls the queue only after that publication or observation that the document is terminal/absent. If the process stops between these stages, the existing recovery path observes the terminal/absent document and completes queue cleanup without repeating provider work. Lost ownership alone never authorizes acknowledgement or deletion.

The processing stream has one logical consumer group, shared by all worker instances. `XDEL` removes a stream entry, not merely one group's reference; additional groups could lose payloads still pending for them. README must state this topology and that the stream is not retained event history. This change does not inspect or migrate an existing deployment. Any later deployment must confirm that all workers share the group before enabling the new cleanup behavior.

Test both fake Redis and a disposable real Redis 7 instance. Assert removal from both PEL and stream after successful, failed, already-terminal and absent-document handling; preserve stored results, TTLs, unread siblings, pending siblings and ownership-lost work. Verify harmless repeat calls. A focused transaction-contract/interruption test must show one transactional execution rather than two independent commands, and no acknowledgement or deletion if execution has not occurred. These checks establish the intended operation; they do not claim rollback for arbitrary Redis command errors or exactly-once provider calls.

Alternatives: separate `XACK`/`XDEL` calls leave an avoidable interruption window; blind size/time trimming can discard unfinished work; a periodic sweeper adds policy and deployment machinery outside this correction. Multiple-group coordination is deliberately not added.

## Verification and compatibility evidence

- PyPDF hierarchy: local inspection at the locked version, plus the review's bounded malformed-input reproduction.
- Gemini metadata: local SDK response construction at 1.75.0; the existing extraction adapter already uses the same evidence-based distinction.
- Redis acknowledgement and deletion semantics: [XACK](https://github.com/redis/docs/blob/main/content/commands/xack.md), [XDEL](https://github.com/redis/redis-doc/blob/master/commands/xdel.md), and [deleted pending entries](https://github.com/redis/redis-doc/blob/master/commands/xreadgroup.md), retrieved through Context7. Deletion does not protect other groups' pending payloads.
- Transactional async client use and ordered results: [redis-py 6.4.0 asyncio examples](https://github.com/redis/redis-py/blob/v6.4.0/docs/examples/asyncio_examples.ipynb) and [pipeline documentation](https://github.com/redis/redis-py/blob/v6.4.0/README.md), retrieved through Context7. Verification uses the project's Redis 7.4 test image and locked client.
- Reuse the existing project's verification commands. Run focused checks per task, then one fresh full gate with no real-Redis skips and disposable HTTP/concurrency/browser checks after the final runtime edit. Do not repeat live Gemini validation or claim broader PDF quality.

## Architecture impact

The parser and summary corrections remain inside their current adapter boundaries. Queue cleanup extends the acknowledgement invariant: an eligible entry is acknowledged and removed together, under the documented single-group topology. No new module, service, persistence schema or public HTTP contract is introduced.

- **Spec:** `openspec/specs/asynchronous-document-processing/spec.md` is the current authority. The two added requirements are proposed in this change's delta; the existing permanent-parser-failure and fenced publication rules remain authoritative.
- **Context:** absent. Bounded checks at the repository, backend package, document-processor package and affected infrastructure/adapter roots found no `CONTEXT.md` or `CONTEXT-MAP.md`.
- **ADR:** absent. The same bounded roots contain no `docs/adr` authority, and OpenSpec configuration designates none.
- **Architecture map:** absent. Those roots contain no `ARCHITECTURE.md` or `docs/architecture` authority; OpenSpec configuration designates no alternative. README is explanatory documentation, not a separately designated architecture authority.
- **Change/status:** `fix-processing-edge-cases` is proposed. Pedro requested all three fixes on 2026-10-25; approval of the completed, reviewed implementation plan is still pending. Runtime edits and canonical-spec synchronization are not yet authorized by this plan.

## Risks / Trade-offs

- Stream deletion is irreversible and applies across groups → limit it to existing safe acknowledgement paths, document one group per stream, and verify pending/unread preservation against real Redis.
- Previously acknowledged history remains → explicitly document that this change prevents future accumulation on the normal acknowledgement path but performs no retrospective purge or hard memory-bound enforcement.
- A response limit can occur despite the character budget → fail honestly; do not infer that this change improves provider capacity or fixes the historical large-document failure.
- PDF parsing can fail in ways beyond this reported library exception → preserve targeted translation and existing tests; this is not a fuzzing campaign or resource-isolation project.
- Validation errors and provider text may contain sensitive input → use fixed messages and existing safe logging, with synthetic leakage regressions.

## Migration Plan

No dependency, data-schema or API migration. Tests use new disposable Redis/Compose resources only. Do not deploy to the existing stack, inspect private credentials, alter existing Redis data or update `main` as part of this change. PR #1 remains unmerged.

After separate deployment authorization, follow the existing whole-worker upgrade guidance and confirm the single-group topology. Pending entries remain recoverable; historical acknowledged entries are left in place. Reverting the queue adapter restores acknowledgement-only behavior for future work but cannot restore entries already removed. Reverting the other adapters restores their previous failure handling.
