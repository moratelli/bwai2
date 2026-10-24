# Tasks

Runtime implementation awaits approval of this complete change. The separately approved Mistral README clarification is already committed as `e156c0e`. Baseline: `make check` passed with 118 backend tests, one optional Redis skip, and 17 frontend tests.

Work sequentially in the existing `dev` worktree. For each behavioral task, add and run the failing test before production edits, run the focused tests and relevant static gates after the fix, self-review, and commit the code/tests with this task's evidence. Do not create another progress ledger. Pure refactoring in task 3.1 uses before/after characterization checks instead of an artificial RED test.

Task 1.1 must update the repository contract and all terminal callers together: leaving one caller unguarded would retain the stale-write defect. The cross-layer edits form one state-publication change. The fixture, checker, and live runner have separate green checkpoints in section 4.

## 1. Worker ownership and lifecycle

- [ ] 1.1 Guard processing and every terminal write against expired records, stale lease tokens, and already-terminal state; propagate typed outcomes through ports, processing, and worker acknowledgement decisions. Add failing repository/service/worker tests for both late success and late failure, missing-source and exhaustion handlers, retained TTL, and expiry/token replacement between guard read and commit. Add real Redis 7 integration cases and a dedicated CI Redis service; make a configured but unreachable test Redis fail rather than silently skip. Verify the repository, processing, worker, result API, and real-Redis suites plus backend static gates, actionlint, and `make check-api`; document restart/no-mixed-worker constraints with the change.

- [ ] 1.2 Renew leases during processing and retry backoff, stop on ownership loss or renewal failure/timeout, and clean up owned tasks and token-checked release on completion and cancellation. Write failing deterministic tests using independent renewal barriers; verify long-running work retains ownership, stale work cannot publish or acknowledge unfinished jobs, expired records are not renewed, and cancellation leaves no renewal task. Run focused worker/repository/runtime tests and backend static gates; document renewal behavior and the remaining possibility of repeated provider calls.

- [ ] 1.3 Move attempt admission into an ownership-guarded persisted transaction, continue the count across worker replacement, and remove stream delivery count from retry-budget decisions. Default the existing batch setting to one without removing explicit overrides. Write failing tests for repeated claims/lock contention, pre-admission infrastructure failure, recovery after admission, exhausted recovery without a fourth provider attempt, and default one-message fetching. Verify processing/worker/repository/runtime suites, relevant real-Redis cases, backend static gates, and unchanged API generation; update README attempt and default-consumer semantics.

## 2. Summary and browser correctness

- [ ] 2.1 Enforce the content-character bound for oversized page blocks and every reduction/synthesis request, preserving source order and stored pages. Add the three-pass/non-contraction failure policy from the design. First reproduce the single-page bypass and oversized synthesis with fake provider calls; cover tiny/exact limits, mixed pages, normal one-call summaries, successful bounded reduction, empty extraction, and safe `summary_too_large` failure. Verify `test_gemini_summarizer.py`, processing error propagation, backend static gates, and the README's exact scope of the chunk bound.

- [ ] 2.2 Add the local unavailable view state for HTTP 404 without changing server statuses or generated contracts. Write failing polling/card tests proving permanent polling stops, temporary failures still retry, other batch results survive, new submissions work, and timers are cleaned up. Update status announcements and fixed resubmission guidance. Verify all frontend tests, Oxfmt/Oxlint, both TypeScript checks, production build, and `make check-api`; document the not-found/expired result behavior.

## 3. Diagnostics and a single submission writer

- [ ] 3.1 Migrate repository/stream fixture setup from `save_queued` and `publish_batch` to `RedisSubmissionGateway.create_batch`, then remove the helpers and any now-unused typing members. Recheck callers immediately before removal and preserve the existing metadata, source, stream, and atomic-submission assertions. Verify the integration suites before and after, remaining references, and backend static gates; production submission behavior stays unchanged.

- [ ] 3.2 Configure emitted worker logs with safe correlation fields and defaults for context-free records, using admitted attempt numbers. First add formatter-output tests for normal events, retry/lease-loss events, a library record, and excluded secret/content extras. Verify worker/runtime/logging tests and backend static gates; add a concise README example that contains only synthetic identifiers and no lease tokens or provider contents.

## 4. Representative quality evidence

- [ ] 4.1 Add the deterministic two-page synthetic report, expected-facts manifest, and generator using the existing ReportLab development dependency. First add failing tests for deterministic bytes, the two-page structure, and the page-specific source facts. Verify generation, fixture content, and backend/script static gates; keep the generator, generated fixture, manifest, and their tests together as one reproducible input bundle.

- [ ] 4.2 Add the standalone labelled-fact checker against that bundle. Write failing tests for missing/swapped pages, wrong values or labels, empty or merely non-empty summaries, and accepted numeric equivalents. Verify positive and negative checker cases and safe failure output without credentials, then run backend/script static gates. Document the checker's supported formatting and limits without claiming it can establish the origin of arbitrary results.

- [ ] 4.3 Build the `make live-quality` runner around the fixture and checker, using its own disposable stack, verified real-summarizer mode, one admitted attempt, bounded requests/deadline, and owned cleanup. First add fake-HTTP/runner tests for successful checks, provider/time-limit failures, failed facts, forbidden fake mode, and cleanup. Run those tests, backend/script static gates, and shell/Make syntax checks; document how to run the command and what one successful result does not prove.

## 5. Integrated verification

- [ ] 5.1 After the final implementation edit, run fresh `make check`, the real Redis 7 integration suite on a disposable instance with no unexpected skips, the existing Compose HTTP/concurrency checks, and the browser flow. Run `make live-quality` with the existing Gemini credential under its controlled stack, allowing at most one rerun for a classified transient provider failure. Record exact outcomes and limits, confirm all owned test containers/volumes were removed without touching the owner's stack or private environment files, and verify the source ZIP manifest and contents. A failed quality assertion or unavailable required gate remains a delivery blocker rather than a claimed pass.

## Workflow follow-up

- Run the installed OpenSpec apply/verification instructions as appropriate; after all tasks, perform report-only semantic verification once, then the risk-tiered final implementation review. Keep the normal single-review/fix budget; do not rerun the entire hiring assessment.
- Run `openspec validate --all --strict --no-interactive` while this change is active. After the complete-plan approval and successful gates/review, synchronize and archive with `openspec archive harden-document-processing -y`.
- Commit and push the completed work, inspect exact-head CI, refresh the source ZIP, and update existing PR #1. Preserve the owner's explicit no-merge instruction: do not squash, merge, enable auto-merge, or update main.
