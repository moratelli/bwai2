# Proposal

## Why

The hiring review exposed reproducible state-consistency bugs: stale workers can overwrite results, processing can recreate expired records, and stream redeliveries can consume retries without doing work. The same review found unbounded summary inputs, endless browser polling after a 404, and limited evidence of extraction and summary quality.

## What Changes

- Guard processing and terminal writes with the current lease token and a live, non-terminal document record. Renew leases while work is active; stop stale work without publishing or acknowledging unfinished jobs.
- Persist the count of admitted processing attempts separately from stream deliveries. Default each consumer to fetching one message while retaining the existing configurable batch setting.
- Enforce the configured summary content bound for oversized pages and synthesis inputs, with a bounded failure path when summaries do not converge.
- Stop polling a document after HTTP 404 and show a local unavailable state, while retaining retries for temporary failures and preserving other results.
- Include safe document/message IDs and actual attempt numbers in worker logs.
- Remove the unused alternative persistence/publication helpers after moving their tests onto the production submission gateway.
- Add one synthetic two-page report with a table, known numeric facts, offline checker tests, and an optional live Gemini quality check.
- Record the directly verified Mistral free-account quota failure in the README.

No new product feature, provider, runtime dependency, or deployment service is planned. HTTP endpoints, parser choices, generated transport contracts, and exact source-page completeness validation remain unchanged.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `asynchronous-document-processing`: Ownership-protected state transitions, renewable leases, durable attempt accounting, and bounded summary requests.
- `web-experience`: A terminal local unavailable state for document polling that returns HTTP 404.
- `local-delivery`: Correlated worker diagnostics and a reproducible, optional AI-quality example with credential-free checker tests.

## Impact

The change touches the backend repository/ports, processing service, worker/runtime configuration, summarizer, their tests, frontend polling/card state, developer scripts, README, and CI verification. It keeps Redis Streams, Redis retention, FastAPI, React/Jotai, Docker Compose, and the existing generated contracts.

HTTP idempotency keys, exactly-once provider calls, extraction checkpoints, stream-retention redesign, authentication, cloud infrastructure, a frontend state rewrite, and broad crash/network testing remain out of scope. Existing data and private environment files stay untouched. PR #1 must remain open and unmerged until the owner separately authorizes merging.
