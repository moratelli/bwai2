# Processing resilience delta

## ADDED Requirements

### Requirement: Coordinate Gemini request admission

The system SHALL pace production Gemini generation requests across workers sharing a coordination scope and model. The default budget SHALL be 12 admissions per minute, evenly spaced rather than released as a burst. Extraction, all summary stages, and retried calls SHALL use the same budget. This is an application admission limit, not a guarantee about other clients or Google's token quotas.

#### Scenario: Multiple workers request permission together

- **WHEN** workers sharing a scope and model request generation concurrently
- **THEN** only one receives immediate admission and later admissions respect the configured spacing
- **AND** the default spacing is at least five seconds regardless of worker process count

#### Scenario: A document requires several provider calls

- **WHEN** extraction, section summaries, reductions, or final synthesis invoke Gemini
- **THEN** every generation call obtains a separate admission
- **AND** SDK retries do not create unaccounted requests behind one admission

#### Scenario: A worker restarts

- **WHEN** a replacement worker starts while a request-admission deadline remains active
- **THEN** it observes that deadline rather than resetting the budget

#### Scenario: Admission cannot be confirmed

- **WHEN** coordination is unavailable, malformed, or a grant's commit outcome is uncertain
- **THEN** no corresponding Gemini call is sent
- **AND** the application treats the problem as infrastructure uncertainty rather than a confirmed Gemini outage

#### Scenario: A waiting operation is cancelled

- **WHEN** processing loses ownership or is cancelled while waiting for admission
- **THEN** it does not send a later provider request
- **AND** an already-consumed admission is not refunded in a way that permits a burst

### Requirement: Coordinate provider cooldowns

The system SHALL coordinate a shared cooldown after retryable Gemini failures while retaining the existing processing-attempt ceiling. Default local backoff SHALL start at 15 seconds, double up to 60 seconds, and add up to five seconds of jitter. Supported provider retry hints SHALL establish a minimum delay, not be shortened by that backoff cap. Infrastructure failures SHALL not be described as Gemini failures.

#### Scenario: Gemini requests a sixty-second delay

- **WHEN** a retryable response supplies a supported sixty-second retry hint
- **THEN** the shared cooldown prevents new admissions before that delay ends
- **AND** changing document, worker, or retry number does not bypass the cooldown

#### Scenario: Another failure extends a cooldown

- **WHEN** another worker records a later cooldown deadline
- **THEN** waiting workers recheck the shared state before admission
- **AND** an older or shorter update cannot shorten the active deadline

#### Scenario: The document exhausts its retry budget

- **WHEN** a retryable Gemini failure occurs on the final permitted attempt
- **THEN** the worker still records a shared cooldown before moving to another document
- **AND** the failed document is not automatically resubmitted

#### Scenario: Retry hints are absent or unsafe

- **WHEN** retry metadata is missing, malformed, negative, non-finite, or outside the documented supported range
- **THEN** the worker uses the bounded fallback policy without exposing the raw metadata

#### Scenario: A supported hint exceeds the exponential-backoff cap

- **WHEN** a supported hint is longer than sixty seconds
- **THEN** the sixty-second backoff cap does not permit an earlier admission

#### Scenario: A request is already in flight

- **WHEN** a different worker establishes a cooldown after a request has already been admitted and sent
- **THEN** the cooldown controls subsequent admissions without claiming to cancel or undo that request

### Requirement: Preserve confirmed retry context

The system SHALL persist a safe, attempt-tagged failure context and any scheduled retry deadline before waiting or publishing exhaustion. Retry-state writes SHALL follow the existing ownership, attempt, terminal-state, and retention fences. Recovery SHALL honor a persisted future deadline without admitting another attempt. A previous failure SHALL be identified as last-confirmed evidence rather than invented evidence about an interrupted later attempt.

#### Scenario: A provider failure is retried

- **WHEN** a retryable Gemini failure has remaining attempts
- **THEN** its allowlisted category, known HTTP status, processing stage, failed attempt, and retry deadline become durable before the worker waits
- **AND** status remains `processing` with no partial result exposed

#### Scenario: Recovery occurs before the deadline

- **WHEN** a replacement owner finds a future persisted retry deadline and has remaining processing attempts
- **THEN** it waits with lease renewal and without incrementing the attempt counter or invoking processing
- **AND** it rechecks eligibility and ownership before admitting work

#### Scenario: Final failure is followed by a worker crash

- **WHEN** final-attempt failure context is durable but the worker stops before terminal publication
- **THEN** recovery publishes `retry_exhausted` with that confirmed context without another provider call

#### Scenario: A later attempt is interrupted without a recorded failure

- **WHEN** recovery exhausts the budget but only an earlier attempt has confirmed provider failure context
- **THEN** any surfaced context is labelled as the earlier last-confirmed failure
- **AND** the application does not claim the interrupted attempt returned the same provider error

#### Scenario: A later confirmed failure is not a provider failure

- **WHEN** a confirmed infrastructure failure replaces an earlier provider failure
- **THEN** current failure context no longer attributes that newer failure to Gemini

#### Scenario: No trustworthy provider context exists

- **WHEN** an exhausted record is legacy, lacks failure context, or contains invalid optional context
- **THEN** the application retains a safe generic exhaustion explanation without fabricating a provider status

#### Scenario: Retry-state publication loses its fence

- **WHEN** the document expires, becomes terminal, changes owner, or advances beyond the writer's attempt before a retry-state update commits
- **THEN** the update cannot recreate or overwrite the record, delete source data, extend retention, or acknowledge unfinished work

#### Scenario: Processing resumes or finishes

- **WHEN** a due attempt is admitted or a terminal result is published
- **THEN** obsolete waiting metadata is removed atomically with that transition
- **AND** success clears failure context while a permanent failure does not inherit an unrelated earlier provider cause
