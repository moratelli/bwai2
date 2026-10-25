# Spec Delta

## ADDED Requirements

### Requirement: Correlate worker processing events safely

Worker processing logs SHALL include stable event names, document identifiers, stream message identifiers, and admitted attempt numbers when available. Logs SHALL NOT include lease tokens, credentials, PDF contents, or provider response bodies. Records without processing context SHALL remain printable.

#### Scenario: A document is retried

- **WHEN** a worker admits an attempt or schedules a retry
- **THEN** the emitted log includes the document identifier and its actual admitted attempt number, rather than treating delivery count as attempts

#### Scenario: A worker loses its lease

- **WHEN** processing stops because lease ownership or renewal cannot be confirmed
- **THEN** the worker emits a safe event identifying the document and message without exposing the lease token or publishing a false completion event

#### Scenario: A non-document record is logged

- **WHEN** a library or startup event has no document identifier or attempt number
- **THEN** the configured logging format emits it without a missing-field error

### Requirement: Provide a representative document quality check

The repository SHALL provide one synthetic two-page PDF containing a table and known numeric facts, along with a repeatable checker for page coverage and factual preservation in extraction and summarization. The live check SHALL use Gemini and SHALL NOT accept test-only summarization as evidence of AI quality.

#### Scenario: A reviewer runs the optional live quality check

- **WHEN** a reviewer supplies Gemini credentials and runs the documented quality command against its disposable stack
- **THEN** the check processes the synthetic report and verifies both ordered source pages, the required page-specific facts, and the required labelled summary facts

#### Scenario: Non-empty output omits or changes a required fact

- **WHEN** output is non-empty but drops a page, moves a page-specific fact to the wrong page, or changes a required value or its meaning
- **THEN** the checker fails and identifies the failed check without printing provider response bodies or credentials

#### Scenario: Equivalent numeric formatting is used

- **WHEN** a result preserves a required labelled value using a supported equivalent format such as `200,000` or `200k`
- **THEN** the checker accepts that formatting rather than requiring an exact summary string

#### Scenario: Credential-free verification runs

- **WHEN** normal project checks or CI run
- **THEN** fixture generation and positive and negative checker tests run without calling Gemini or requiring provider credentials

#### Scenario: A live quality run cannot complete

- **WHEN** the provider fails, the processing deadline expires, or the quality assertions fail
- **THEN** the check fails explicitly, retains only safe diagnostic evidence, and does not silently substitute fake output or claim success
