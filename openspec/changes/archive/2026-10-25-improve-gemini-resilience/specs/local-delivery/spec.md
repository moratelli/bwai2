# Provider operation delta

## ADDED Requirements

### Requirement: Configure the application's Gemini budget

The delivery configuration SHALL expose the application's Gemini request-admission budget and retry timing without exposing credentials. Documentation SHALL identify the shared coordination scope, slower processing under that budget, and the distinction between request pacing and provider token quotas. Existing readiness semantics SHALL remain configuration/dependency checks, not proof of successful Gemini inference.

#### Scenario: Default configuration is used

- **WHEN** a configured production worker starts with default provider settings
- **THEN** it uses twelve generation admissions per minute shared with workers in the same scope and model
- **AND** its default retry policy uses a fifteen-second base, sixty-second exponential cap, and up to five seconds of additive jitter

#### Scenario: Provider budget configuration is invalid

- **WHEN** a request budget is zero, negative, or otherwise outside supported configuration bounds
- **THEN** startup rejects the configuration instead of silently disabling pacing

#### Scenario: Other applications use the Google project

- **WHEN** a reviewer reads the setup and operations guidance
- **THEN** it explains that separately coordinated applications can consume the same provider quota
- **AND** it does not advertise the local budget as a project-wide quota guarantee

#### Scenario: Token limits are discussed

- **WHEN** documentation describes the reported 250k input-token-per-minute allowance
- **THEN** it distinguishes tokens from characters and explains that proactive token admission is outside this change
- **AND** it describes token-quota rejections as provider evidence rather than proof of a PDF defect

#### Scenario: Credential-free verification runs

- **WHEN** normal tests or disposable smoke checks run
- **THEN** they use fake provider responses or the existing test summarizer without calling Gemini
- **AND** provider-budget behavior is tested with controlled time instead of minute-long sleeps

### Requirement: Observe provider requests safely

Provider-request diagnostics SHALL record allowlisted outcomes, processing stage, known HTTP status, duration, and returned token counts when available. Retry diagnostics SHALL retain existing document/attempt correlation. Diagnostics SHALL NOT include prompts, generated text, PDFs, credentials, raw provider errors, arbitrary headers, quota dimensions, or credential-derived coordination keys.

#### Scenario: A provider request succeeds

- **WHEN** Gemini returns a response with valid usage metadata
- **THEN** logs can report its request outcome and numeric prompt/output token counts without logging generated or submitted content

#### Scenario: A provider request fails

- **WHEN** Gemini returns HTTP 429 or 503
- **THEN** safe diagnostics distinguish those statuses and the processing stage
- **AND** missing token usage remains unknown rather than being reported as zero

#### Scenario: A transport error has no status

- **WHEN** the request fails without a provider HTTP response
- **THEN** diagnostics record a safe timeout or connection category without an invented status

#### Scenario: Metadata contains unsafe or unexpected values

- **WHEN** a response or logging extra contains arbitrary strings, malformed numbers, or sensitive canaries
- **THEN** only validated allowlisted metadata reaches the log record

#### Scenario: A request has no document logging context

- **WHEN** a provider-request event has no document or stream identifier
- **THEN** it remains printable without inventing correlation fields
