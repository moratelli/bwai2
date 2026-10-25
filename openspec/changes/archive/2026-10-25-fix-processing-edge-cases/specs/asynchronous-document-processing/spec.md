# Spec Delta

## ADDED Requirements

### Requirement: Reject confirmed truncated Gemini summaries

The system SHALL reject any Gemini summarization response whose consumed candidate reports `MAX_TOKENS`, using the existing permanent `invalid_provider_response` error and a safe message. It SHALL neither publish that response as a completed summary nor use it in a later synthesis step.

#### Scenario: Final summary reaches the output limit

- **WHEN** the consumed candidate for a direct document summary or final synthesis reports `MAX_TOKENS`
- **THEN** the document fails permanently rather than publishing a completed result
- **AND** the rejection applies whether its text is absent, partial or apparently complete

#### Scenario: Intermediate summary reaches the output limit

- **WHEN** a section summary or reduction response reports `MAX_TOKENS` for the consumed candidate
- **THEN** processing stops without sending that response to a later summarization request
- **AND** no partial pages or summary are published as a completed document

#### Scenario: Finish metadata is absent or unrecognized

- **WHEN** the consumed candidate has no recognized output-limit signal
- **THEN** normal summary validation determines the outcome without inventing truncation

#### Scenario: An unused candidate reports an output limit

- **WHEN** only an unused alternative candidate reports `MAX_TOKENS`
- **THEN** that alternative does not change validation of the consumed candidate

#### Scenario: Ordinary summary output remains valid

- **WHEN** a response passes existing summary validation without a confirmed output-limit signal
- **THEN** the system retains the existing successful summary flow, content budgets and retry policy

#### Scenario: Rejection reaches the stored result

- **WHEN** the current processing owner receives a confirmed summary-limit rejection
- **THEN** the existing result interface exposes a failed document with `invalid_provider_response` and its safe message
- **AND** the failure is persisted before acknowledgement without another provider attempt

#### Scenario: Rejected output contains sensitive data

- **WHEN** rejected summary text or metadata contains sensitive strings
- **THEN** the diagnostic message and emitted logs exclude those strings and raw provider details

### Requirement: Reclaim acknowledged processing entries

For a processing stream with one logical consumer group shared by its workers, the system SHALL acknowledge and remove an eligible entry atomically under the existing terminal-state rules. It SHALL preserve unrelated unread and unfinished pending entries and retain the existing document-result expiry policy.

#### Scenario: Successful processing is acknowledged

- **WHEN** a completed result has been safely persisted and its queue entry is acknowledged
- **THEN** that entry is absent from both the group's pending list and the stream
- **AND** the document result remains available until its existing retention deadline

#### Scenario: Failed processing is acknowledged

- **WHEN** a permanent or exhausted failure has been safely persisted and its queue entry is acknowledged
- **THEN** that entry is removed from both the pending list and the stream without removing the stored failure result

#### Scenario: A terminal or absent document is redelivered

- **WHEN** a delivered entry refers to an already-terminal or absent document
- **THEN** safe acknowledgement also removes that entry without invoking a provider or recreating document data

#### Scenario: Another job is still waiting or processing

- **WHEN** one eligible entry is reclaimed while other entries are unread or pending
- **THEN** those other entries remain available for normal delivery or stale-message recovery

#### Scenario: Ownership is lost before publication

- **WHEN** a worker loses ownership and cannot publish its unfinished result
- **THEN** it neither acknowledges nor removes that job's entry

#### Scenario: Cleanup is repeated

- **WHEN** acknowledgement is repeated for an entry already acknowledged and removed
- **THEN** it is harmless and does not alter another entry or the stored document result

#### Scenario: Cleanup is interrupted before execution

- **WHEN** the acknowledgement-and-removal operation has not executed
- **THEN** neither part takes effect and the pending entry remains recoverable
- **AND** another client cannot observe an interleaving between the operation's acknowledgement and removal

#### Scenario: Updated workers start with retained history

- **WHEN** updated workers start on a stream containing previously acknowledged history
- **THEN** startup does not scan or purge that history
- **AND** cleanup is limited to entries subsequently selected for safe acknowledgement
