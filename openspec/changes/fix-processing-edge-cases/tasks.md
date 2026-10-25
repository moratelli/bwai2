# Tasks

## Execution rules

Use the existing linked `dev` worktree, starting from `f87c1ad66d2413b690f0c6486cdff0f5de96a070`. Runtime implementation begins only after the complete plan passes review and Pedro approves it. Keep all progress and evidence in this file; do not create a separate ledger.

For each behavioral task, write and run its failing regression before editing production code. Implement the smallest correction, run the focused tests and static checks, self-review, then commit code, tests, any required README change and this task's evidence together. Continue through the approved tasks without routine approval pauses.

Focused Python gates for tasks 1-3: `uv run --project backend ruff check backend scripts`; `uv run --project backend ruff format --check backend scripts`; `uv run --project backend mypy backend/src backend/tests`; `uv run --project backend mypy --strict scripts`; `git diff --check`. Use targeted LSP feedback without replacing these gates. Do not repeat the full project gate inside each task.

## 1. Contain PDF-library failures

- [ ] 1.1 Add a bounded malformed-cross-reference regression and map relevant `PyPdfError` failures in both PDF adapters to the existing permanent `invalid_pdf` error. Verify both parsers fail safely, Gemini makes no provider request for rejected local input, source cleanup and acknowledgement occur, and a valid sibling completes through the same worker without an escaping exception. Preserve the explicit encrypted-PDF path and existing non-library exception handling. Run `uv run --project backend pytest backend/tests/unit/test_pypdf_parser.py backend/tests/unit/test_gemini_parser.py backend/tests/unit/test_processing.py backend/tests/unit/test_worker.py backend/tests/integration/test_processing_edge_cases.py` and the focused Python gates before completing the task.
  - Add `backend/tests/integration/test_processing_edge_cases.py` for actual adapter/service/repository/queue/worker/result-boundary regressions using synthetic input and fake Redis. Do not assert stream-entry deletion yet; task 3 adds that policy.
  - Cover safe fixed messages and retained normal, corrupt and encrypted behavior. Do not add a generic worker exception handler or new error code. This restores the canonical permanent-parser-failure behavior; no new user-facing parser policy is needed.

## 2. Reject truncated summaries

- [ ] 2.1 Add SDK-shaped summary-limit regressions, then reject confirmed `MAX_TOKENS` at the summarizer's shared request boundary with the exact safe message in `design.md`. Verify direct/final summaries and section/reduction responses, stopping before further synthesis when an intermediate response is rejected. Preserve absent/unrecognized metadata, unused alternatives, normal successful summaries, empty-output handling, provider errors and existing request budgets. Run `uv run --project backend pytest backend/tests/unit/test_gemini_summarizer.py backend/tests/unit/test_processing.py backend/tests/integration/test_processing_edge_cases.py backend/tests/api/test_document_results.py backend/tests/unit/test_worker_logging.py` and the focused Python gates before completing the task.
  - Extend the pipeline regression to assert a persisted permanent `invalid_provider_response`, no completed pages/summary, no further attempt, safe emitted logs and unchanged HTTP result shape. Include sensitive-string canaries without credentials or customer data.
  - Update README's summary/diagnostics guidance in this task. Explain confirmed output-limit rejection without promising larger-document success or changing existing stored failures. Do not refactor the extraction adapter or create a shared provider-response framework.

## 3. Reclaim eligible queue entries

- [ ] 3.1 Add failing queue-retention regressions, then execute `XACK` and `XDEL` for the selected entry in one Redis 7 transaction at the existing acknowledgement boundary. Preserve the integer `XACK` result and harmless repeat behavior. Verify successful, failed, already-terminal and absent-document paths; stored result/TTL preservation; recoverable unread/pending siblings; and no acknowledgement/deletion for ownership-lost work. Run `TEST_REDIS_URL=<disposable Redis 7 URL> uv run --project backend pytest backend/tests/integration/test_redis_streams.py backend/tests/integration/test_redis_transitions.py backend/tests/integration/test_processing_edge_cases.py backend/tests/unit/test_worker.py` with no real-Redis skips, plus the focused Python gates, before completing the task.
  - Extend the stream test setup to cover fake Redis and a disposable real Redis 7 instance using the existing `TEST_REDIS_URL` convention. Missing configuration may explicitly skip locally; configured but unreachable Redis must fail. Use test-owned keys and clean up only those keys/resources.
  - Include a transaction-contract/interruption test proving one `transaction=True` execution, no independent acknowledgement/deletion window, and unchanged pending state when execution has not occurred. Preserve the group and stream usability after the last entry is removed.
  - Extend the worker pipeline tests from tasks 1-2 to check stream removal as well as pending-list removal. Cover retry-exhausted cleanup and redelivery after terminal publication; provider work must not repeat for a stored terminal result.
  - Update README in this task: one logical consumer group per stream, all workers share it, queue entries are not audit history, cleanup does not extend/remove document results, and startup does not purge previously acknowledged history. State that a later deployment must confirm that topology. Do not add a sweep, `XTRIM`, multi-group coordination or changes to the worker's acknowledgement eligibility.

## 4. Verify the combined change

- [ ] 4.1 After the final runtime edit, run a fresh `TEST_REDIS_URL=<disposable Redis 7 URL> make check` with no real-Redis skips. Run `make smoke` and `make browser-e2e` using new disposable Compose resources, an empty `GEMINI_API_KEY`, test summarization, an unused `APP_PORT` and matching `APP_URL`/`PLAYWRIGHT_BASE_URL`. Verify resource cleanup, unchanged owner-stack identifiers and unchanged private `.env` metadata without reading its contents. Probe changed files with LSP, inspect session diagnostics, run `git diff --check` and record any advisory/inconclusive results separately from passing project gates. Commit the combined verification evidence in this file.
  - Reuse the existing isolated test procedure; never point integration tests at an application Redis database. Do not execute the live-provider or live-quality targets, and do not access customer PDFs.
  - Reconcile only the three requested fixes. Do not expand into queue admission, batching, broad fault campaigns, UI redesign, infrastructure or new evaluation tooling.

## Workflow follow-up

- Run the installed `openspec-verify-change` workflow once, report-only, against the completed active change. Verify all proposed scenarios and the restored canonical permanent-parser-failure contract.
- Run the risk-tiered final implementation review once after fresh gates. Apply the normal one-fix-wave limit for demonstrated critical/high findings; do not rerun the whole hiring assessment or add unrelated cleanup.
- Reuse the bounded architecture evidence unless its inputs change. Run `openspec validate --all --strict --no-interactive` while active, then `openspec archive fix-processing-edge-cases -y` under the completed-plan approval. Verify exact delta synchronization, preservation of unrelated requirements and strict validation afterward.
- Refresh and inspect the source ZIP, push `dev`, verify exact-head CI and update PR #1 with the result. Preserve its title/body, leave it open without auto-merge, and do not update `main` or rebuild the existing stack.
- No historical Redis purge or live provider run is authorized by this change. Any later deployment must receive separate authorization and confirm the single-group topology.
