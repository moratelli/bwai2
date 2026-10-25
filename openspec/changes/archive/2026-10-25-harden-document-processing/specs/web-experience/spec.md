# Spec Delta

## MODIFIED Requirements

### Requirement: Show asynchronous progress

The web application SHALL display each accepted document independently and poll until it reaches a terminal state or its resource returns HTTP 404. An unavailable resource SHALL be a terminal local retrieval state, distinct from a server-reported processing failure.

#### Scenario: Batch is accepted

- **WHEN** the upload API accepts a batch
- **THEN** the page creates one status card per returned document and begins polling

#### Scenario: Document remains in progress

- **WHEN** a poll returns `queued` or `processing`
- **THEN** the card displays the current state without showing stale result content

#### Scenario: Polling request fails temporarily

- **WHEN** a status poll encounters a temporary network or server failure
- **THEN** the page shows a non-destructive connection message and retries with a bounded delay

#### Scenario: Polling returns HTTP 404

- **WHEN** a tracked document's resource returns HTTP 404
- **THEN** its card explains that the document was not found or has expired, stops claiming it is processing, and schedules no further polls for that identifier

#### Scenario: One unavailable document shares a batch with active or completed documents

- **WHEN** one document becomes unavailable while other documents remain active or completed
- **THEN** the page preserves completed results and continues polling only the active documents

#### Scenario: The user submits another document after a 404

- **WHEN** a new submission is accepted after an earlier resource became unavailable
- **THEN** polling starts for the new identifier without reviving the unavailable resource

#### Scenario: Transport response contains a client-only status

- **WHEN** the API response contains the local unavailable status instead of a supported server status
- **THEN** the transport validator rejects it rather than extending the server contract
