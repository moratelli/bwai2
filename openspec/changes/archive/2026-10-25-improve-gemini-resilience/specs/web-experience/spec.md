# Provider feedback delta

## ADDED Requirements

### Requirement: Explain provider-dependent progress

The browser SHALL distinguish confirmed provider quota rejection, provider unavailability, timeout/connection failure, and document errors using validated result metadata. A scheduled retry SHALL remain an active `processing` document. The browser SHALL neither infer a Gemini outage from a generic error nor promise an automatic retry that the backend has not scheduled.

#### Scenario: A provider retry is waiting

- **WHEN** polling returns a valid `retry_wait` for a processing document
- **THEN** that card explains the confirmed reason and displays the retry's not-before time
- **AND** polling continues without marking the document failed, completed, or unavailable

#### Scenario: The displayed retry time has passed

- **WHEN** the not-before time passes but the backend still reports a retry wait
- **THEN** the card continues to describe waiting without a negative countdown or a claim that a request has started
- **AND** it does not submit or retry the document from the browser

#### Scenario: The provider rejects a request for quota reasons

- **WHEN** a failed result contains a confirmed Gemini rate-limit cause
- **THEN** its card identifies the API quota or rate-limit rejection and offers manual recovery guidance
- **AND** it does not claim that Gemini is overloaded or the PDF is corrupt

#### Scenario: Gemini is temporarily unavailable

- **WHEN** a failed result contains a confirmed Gemini unavailability cause
- **THEN** its card identifies the provider failure and recorded processing stage
- **AND** it describes the job's recorded failure rather than claiming a current global outage

#### Scenario: An earlier attempt is the last confirmed evidence

- **WHEN** a failed result's cause refers to an earlier admitted attempt
- **THEN** the card labels it as the last confirmed failure, not the outcome of the interrupted later attempt

#### Scenario: Failure context is absent

- **WHEN** a result has only a legacy safe error code and message
- **THEN** the browser displays that message without inventing a provider, status, stage, or scheduled retry

#### Scenario: New metadata is malformed

- **WHEN** a response contains an invalid optional cause, category, attempt, or retry timestamp
- **THEN** the transport validator rejects the response rather than displaying untrusted metadata as provider evidence

#### Scenario: A retry resumes or completes

- **WHEN** polling returns a newer result without retry-wait metadata
- **THEN** the old waiting explanation is removed and the current processing or terminal result is rendered

#### Scenario: Existing failure and accessibility behavior applies

- **WHEN** a document fails permanently, expires, or has its status updated
- **THEN** existing document-specific guidance, safe Markdown rendering, keyboard behavior, and status announcements remain intact
- **AND** successful sibling results remain visible

### Requirement: Summarize tracked document outcomes

The browser SHALL report counts of completed, failed, active, and unavailable documents across its currently tracked cards. It SHALL keep failed processing distinct from missing or expired results and from temporary polling errors. These counts SHALL not introduce a separate server-side batch identity or imply that terminal failure is successful completion.

#### Scenario: All tracked documents are terminal

- **WHEN** two documents completed and eight failed
- **THEN** the summary identifies two completions and eight failures instead of only saying that all processing finished

#### Scenario: Some documents are still processing

- **WHEN** completed, failed, and active cards coexist
- **THEN** the summary retains completed and failed counts and reports the active count
- **AND** a processing card waiting to retry is counted as active

#### Scenario: A result is unavailable

- **WHEN** a tracked result returns HTTP 404
- **THEN** its unavailable count is separate from server-reported processing failures

#### Scenario: A polling request fails temporarily

- **WHEN** polling has a temporary transport error
- **THEN** the connection warning does not convert active cards into document failures or remove successful results

#### Scenario: Additional documents are submitted

- **WHEN** another submission adds new document identifiers
- **THEN** counts reflect all currently tracked cards without resetting earlier outcomes or reviving terminal identifiers
