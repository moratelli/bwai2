# Design

## Context

See [proposal.md](proposal.md) for motivation and scope. The implementation base is `1b78b53` in the isolated `fix/gemini-provider-resilience` worktree. Its baseline passed 402 backend tests with disposable Redis, 23 frontend tests, formatting, lint, type checks, API drift checks, and builds.

Two worker loops currently retry inside one handler. Each adapter calls Gemini directly; a document can make several summary requests. The worker replaces the final safe exception with a generic exhaustion message. Redis already provides owner/attempt-fenced admission and publication, renewable leases, and fixed document retention. Those mechanisms remain authoritative.

The owner's reported provider allowance is 15 RPM and 250k input TPM. The earlier 27 processing starts were not an exact provider-request or token measurement. A separate, authorized one-request check returned HTTP 503 with an overload indication. Neither observation establishes the status of every historical failure.

## Goals / Non-Goals

Use the existing Redis dependency and existing document state machine. Add no infrastructure service, third-party dependency, provider call for token counting, or browser-driven retry loop. Keep full-document retries; successful extraction and intermediate summaries are not checkpointed by this change.

The application limits its own generation admissions. It does not claim to control separately coordinated clients, enforce the provider's TPM/RPD limits, guarantee provider availability, or execute external requests exactly once. No new live-provider or private-document test is authorized by approval of this plan.

## Decisions

### 1. Keep provider evidence typed and narrow

Extend the safe processing-error model with optional immutable provider information. It contains `provider: gemini`, a closed category (`rate_limited`, `unavailable`, `timeout`, `connection_error`, or `temporary_failure`), stage (`extraction` or `summarization`), optional HTTP status, and an internal optional retry-delay value. Add the admitted attempt when persisting or exposing the context.

Classify HTTP 429 as quota/rate rejection, HTTP 5xx as provider unavailability, recognized timeouts and transport errors separately, and other currently retryable statuses as temporary failures. A 503 is not sufficient to claim a global outage. Preserve existing permanent errors, MAX_TOKENS rejection, page validation, and the three-attempt unit. Keep existing retryable and exhaustion codes; new metadata supplies the distinction.

Parse supported retry hints from numeric `Retry-After`, HTTP-date `Retry-After`, and `google.rpc.RetryInfo.retryDelay`. Use the largest valid hint. Accept finite nonnegative delays up to 86,400 seconds; reject malformed, negative, non-finite, excessive, or unsupported values and use fallback backoff. The cap on exponential backoff never truncates a supported hint. Persist only the resulting deadline and safe enums/numbers, never raw headers, provider messages, quota dimensions, or exception chains.

**Alternative:** copying provider exception text into the error message would be smaller but could leak content or credentials. Inferring a quota category from `retry_exhausted` would be inaccurate.

### 2. Put one admission boundary around every generation call

Introduce a small shared Gemini request helper used by both adapters. It obtains permission, calls the injected SDK client once, translates SDK exceptions, and emits safe request diagnostics. Keep the adapters' existing client ownership and successful-response validation. Pass domain/infrastructure errors through rather than wrapping a limiter failure as a Gemini rejection. Explicitly configure SDK `attempts=1`; other model/request options remain unchanged.

Wire one Redis gate into each worker and its Gemini adapters. All gate instances for a scope and model use the same Redis key. Include direct summaries, all chunks/reductions/synthesis, and Gemini PDF extraction. Local PyPDF work and the test summarizer do not consume provider permits. Runtime tests must prove that neither production adapter can bypass the gate.

Configuration:

| Setting | Default | Meaning |
| --- | --- | --- |
| `GEMINI_REQUESTS_PER_MINUTE` | `12` | Positive integer admission budget, supported range 1–1000 |
| `GEMINI_RATE_LIMIT_SCOPE` | `bwai2` | Nonsecret coordination label, 1–64 ASCII letters/digits/underscores/hyphens |
| `WORKER_RETRY_BASE_SECONDS` | `15` | Initial retry backoff |
| `WORKER_RETRY_MAX_SECONDS` | `60` | Exponential component cap |
| `WORKER_RETRY_JITTER_SECONDS` | `5` | Additive random delay, never subtraction from a hint |

The gate uses Redis `TIME` and `WATCH`/`MULTI`/`EXEC`, consistent with the repository's transaction style. Store the configured interval, next admission time, and shared cooldown deadline. A grant is allowed only after both deadlines and advances the next admission by `ceil(60000 / RPM)` milliseconds. Waiters do not reserve future slots; they sleep and recheck, including after another worker extends a cooldown. Retry at most five optimistic conflicts per operation before reporting infrastructure uncertainty.

Use a nonsecret scope and model in the key, never an API key or key fingerprint. Workers in one scope must agree on the budget; a conflicting stored interval fails closed rather than allowing a faster worker to bypass it. State expires no earlier than sixty seconds after its latest admission/cooldown deadline. A corrupt state, unavailable Redis, or ambiguous commit authorizes no call. Do not refund a permit after cancellation or an uncertain outcome.

The worker computes `max(min(base * 2^(attempt-1), cap), supported_hint) + jitter`. For confirmed retryable provider failures, it extends the shared cooldown to at least that delay and receives the effective deadline, including any longer existing cooldown. It does this even on the final attempt, before returning to the queue. Infrastructure failures use backoff without being labelled a Gemini failure. Already-admitted requests can finish; the gate governs subsequent admissions, not network cancellation or requests from other applications.

**Alternatives:** an in-process semaphore only limits concurrency and cannot coordinate replicas or restarts. A token bucket allows an initial burst; evenly spaced admissions are easier to reason about for this small allowance. Proactive TPM enforcement would require separate token accounting for text and PDF requests and is not justified by the current evidence.

### 3. Persist retry evidence using the existing document fences

Add an immutable attempt-tagged failure context, a nullable retry deadline, and a `RetryWaiting` admission result. A last failure can have no provider information, so a later infrastructure failure explicitly replaces an earlier Gemini attribution.

Add one repository operation to record a caught retryable failure and an optional retry deadline. It watches the document and lock, requires the current token, `processing` status, and exact admitted attempt, and writes context before sleeping or publishing final exhaustion. Preserve the existing TTL; do not delete source data. A rejected or uncertain write leaves unfinished work pending and authorizes no further processing by that handler.

Admission first checks existence/ownership/status and the attempt ceiling. An exhausted record returns its last confirmed context. Otherwise a future stored retry deadline returns `RetryWaiting` without incrementing attempts. Use Redis server time for this comparison. A due admission clears waiting metadata atomically with the increment but retains attempt-tagged last failure evidence until it is replaced or processing finishes.

The worker handles `RetryWaiting` under its existing lease-renewal wrapper and re-enters admission after waiting. This covers normal retry and replacement-worker recovery. Rate-gate waiting happens inside an already-admitted processing attempt and does not add further attempts. Only recorded retry backoff is exposed as a scheduled retry; ordinary pacing is not labelled an upstream error.

Terminal success clears all retry/failure metadata. Permanent failure clears any unrelated previous retry cause. Exhaustion retains `retry_exhausted` and publishes a useful fixed-wording message with confirmed context. If recovery only has an earlier attempt's failure, say "last confirmed failure on attempt N" rather than claiming the interrupted final attempt returned that error. If no trustworthy context exists, retain generic exhaustion wording. The existing terminal transaction still publishes fields and source deletion together before acknowledgement.

Decode absent or invalid optional legacy metadata conservatively, without breaking existing document identity/status decoding or inventing a cause. A crash before a context write commits can still lose that in-memory evidence; the UI must not fill the gap with a guess.

**Alternative:** carrying the cause only in an exception or changing only the terminal message loses evidence on worker replacement and cannot support reliable waiting feedback.

### 4. Extend only the document-result contract

Keep all current statuses and fields. Add optional metadata to the processing and failed variants; omit it when absent. Use a dedicated failed-document error DTO rather than changing general submission/health error envelopes.

Example processing metadata (existing base fields omitted):

```json
{
  "status": "processing",
  "attempts": 1,
  "retry_wait": {
    "until": "2026-10-25T18:01:00Z",
    "cause": {
      "provider": "gemini",
      "category": "rate_limited",
      "stage": "summarization",
      "attempt": 1,
      "http_status": 429
    }
  }
}
```

A failed document can expose the same cause shape under `error.cause`, while `error.code` remains `retry_exhausted`. The safe `error.message` must explain the cause even to a client that ignores the optional object. Require valid closed enums, a positive cause attempt no greater than the document's attempt count, and category/status consistency; omit unknown HTTP status rather than using zero. A valid provider deadline without trustworthy provider cause is not surfaced as a provider retry wait.

Regenerate OpenAPI and TypeScript artifacts in the same task as DTOs and the frontend runtime transport consumer. Presentation follows in a separate task without changing that contract. Missing or null optional fields are treated as absent; present malformed metadata is rejected. Preserve existing payload shapes when no new fields exist and leave ordinary API error envelopes unchanged.

### 5. Explain recorded outcomes, not inferred live provider health

A processing card with `retry_wait` says it is waiting to retry, gives the recorded reason, and shows the not-before time. Do not add a new status, stop polling, create client retry timers, or promise a request starts at that time. When the time passes but metadata remains, continue to say it is waiting. Ordinary request pacing can retain the existing `processing` presentation.

Failed cards distinguish quota rejection, unavailability, timeout/connection failure, and existing document errors. Show the recorded stage; only say local extraction succeeded when a Fast text job's confirmed failure occurred during summarization. Earlier-attempt evidence is labelled "last confirmed". No global outage banner, automatic resubmission, model switching, or fabricated partial success is introduced.

Replace the generic terminal summary with counts over all tracked cards, including cards from earlier submissions. Keep unavailable separate from failed, count retry waits as active, and preserve polling warnings and successful siblings. No server-side batch identifier is added.

### 6. Keep evidence useful without logging content

Extend the allowlisted formatter with provider category/stage, known HTTP status, request duration, and valid numeric `prompt_token_count`/`candidates_token_count` when returned. Missing usage is unknown, not zero. The shared request helper need not introduce task-local logging state: events without document context remain valid, while worker retry/failure events retain document/message/attempt correlation.

Document the default request budget, coordination scope, supported retry hints, increased latency, remaining token-quota risk, and the fact that readiness checks configuration rather than live inference. Do not expose quota dashboards or claim reserved tokens. CI and normal verification remain credential-free.

## Risks / Trade-offs

- **Separate clients can consume provider quota** → scope guarantees to cooperating workers sharing Redis, scope, and model; leave default RPM headroom and retain accurate 429 handling.
- **TPM/RPD can still be exhausted** → record returned usage and quota failures; do not advertise proactive token enforcement.
- **Provider unavailability can outlast retries** → retain terminal failure and useful cause; pacing is not an availability guarantee.
- **Long hints increase waiting time** → bound accepted hints to one day, show the recorded not-before time, and preserve lease cancellation and original document expiry.
- **Shared cooldown delays unrelated documents** → intentional for the same quota/model scope; do not cancel already-sent requests or claim strict FIFO fairness.
- **A final request can be interrupted without a durable cause** → show only last-confirmed or generic evidence, never infer the final HTTP status.
- **Clock and network timing differ from admission time** → use Redis time for shared deadlines; describe the property as admission pacing, not exact wire-arrival spacing.

## Architecture impact

- **Boundary/invariant:** centralize Gemini request admission and safe error translation within infrastructure; extend existing fenced document state with optional retry evidence. Preserve API/worker separation, parser selection, full-document attempts, lease renewal, terminal immutability, source deletion, and retention.
- **Spec:** [processing](../../specs/asynchronous-document-processing/spec.md), [results](../../specs/document-results/spec.md), [web](../../specs/web-experience/spec.md), and [delivery](../../specs/local-delivery/spec.md) own current behavior. The four deltas in this change propose additions; canonical specs are not edited during planning.
- **Context:** absent in the bounded root/backend/frontend discovery of `CONTEXT.md` and `CONTEXT-MAP.md`.
- **ADR:** absent in the bounded root/backend/frontend `docs/adr` discovery. No new ADR is proposed.
- **Architecture:** no designated architecture authority, `ARCHITECTURE.md`, or `docs/architecture` was found in those roots. `openspec/config.yaml` specifies `spec-driven` and adds no authority/artifact rules.
- **Approval:** The owner approved the complete change on 2026-10-25 after an Opus review returned no findings at `369c5f2`. Approval covers all tasks, verification, final review, synchronization/archive, PR creation, and squash merge after the gates. The exclusions below still apply.

## Migration Plan

1. Implement and verify only in the isolated worktree, using fake providers and disposable Redis/Compose resources. Keep the Desktop stack, its configuration, and existing documents untouched.
2. Additive fields require no data migration. New readers tolerate old records; historical failures retain their stored messages. Do not purge stream history or reset admission/cooldown state at startup.
3. Deployment, when separately authorized, must update cooperating workers consistently. An old worker can bypass the new gate. Keep API and frontend generated contracts aligned; optional fields permit legacy responses.
4. If changing an existing coordination scope's budget, quiesce its workers and let admission/cooldown state expire before starting the new matching configuration; do not use a scope change to evade an active provider cooldown.
5. Rollback can restore the prior images without deleting data, but forfeits the new pacing and feedback. Do not perform deployment, rollback, key rotation, or billing changes as part of this plan's implementation.

## Verification evidence and references

The tests and acceptance flow are specified in `tasks.md`. Baseline logs are in the worktree's ignored `.tmp/baseline-check.log`; they are evidence, not a second task ledger.

- Redis server time and optimistic transactions: [TIME](https://redis.io/docs/latest/commands/time/) and [redis-py transaction guidance](https://redis.readthedocs.io/en/stable/advanced_features.html), verified through Context7 and the installed client's command surface.
- SDK behavior: installed `google-genai 1.75.0`; offline fake-transport checks showed one request with unset retry options, and explicit `HttpRetryOptions(attempts=1)` was exercised in the authorized diagnostic. Synthetic `APIError.details` retains the structured error payload containing `google.rpc.RetryInfo`.
- Gemini error categories and quota scope: [API errors](https://ai.google.dev/gemini-api/docs/generate-content/api-errors) and [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits), verified during the investigation. The numerical account allowances were supplied by the owner, not discovered automatically.
