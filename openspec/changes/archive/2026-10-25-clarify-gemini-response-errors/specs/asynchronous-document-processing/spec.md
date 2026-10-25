# Spec Delta

## ADDED Requirements

### Requirement: Explain rejected Gemini extraction responses

When rejecting a Gemini extraction response, the system SHALL report an evidence-based, safe reason through the existing `invalid_provider_response` error. It SHALL retain permanent-failure behavior and complete-page validation and SHALL not expose provider content in errors or logs.

#### Scenario: Provider reports an output limit

- **WHEN** the consumed response candidate reports `MAX_TOKENS`
- **THEN** the failure message identifies the response limit, even if the returned text is empty, malformed or appears structurally complete
- **AND** no partial result is accepted

#### Scenario: Provider returns no extraction text

- **WHEN** the response text is absent, empty or whitespace and there is no confirmed output-limit signal
- **THEN** the failure message identifies empty extraction output without claiming that a size or token limit was reached

#### Scenario: Provider returns malformed JSON

- **WHEN** non-empty response text is not valid JSON and there is no confirmed output-limit signal
- **THEN** the failure message identifies malformed JSON without quoting the response or guessing its cause

#### Scenario: Provider returns an invalid schema

- **WHEN** valid JSON fails the required page schema, including an empty page array
- **THEN** the failure message identifies an invalid page structure

#### Scenario: Provider returns a different page count

- **WHEN** schema-valid output contains fewer or more pages than the source PDF
- **THEN** the failure message states the expected and returned page counts and rejects the result

#### Scenario: Provider returns invalid page indexes

- **WHEN** schema-valid output has the expected page count but its indexes are duplicated, missing, non-zero-based or out of order
- **THEN** the failure message identifies invalid page numbering or order and rejects the result

#### Scenario: Finish metadata is absent or unrecognized

- **WHEN** there is no recognized output-limit signal for the consumed candidate
- **THEN** normal text, schema and page-completeness validation determines the outcome
- **AND** metadata from an unused alternative does not misclassify that outcome

#### Scenario: Complete many-page response passes validation

- **WHEN** a response satisfies the existing schema and contains every source page in zero-based order without a confirmed output-limit signal
- **THEN** validation succeeds without an added page-count cutoff

#### Scenario: Rejection reaches the result boundary

- **WHEN** the current processing owner receives a classified response rejection
- **THEN** the document fails permanently with `invalid_provider_response` and its safe message in the existing result schema
- **AND** the UI displays that message, summarization is not invoked, and no partial pages are published

#### Scenario: Provider text contains sensitive data

- **WHEN** rejected text, schema fields or provider metadata contain sensitive strings
- **THEN** diagnostic errors and emitted logs exclude those strings, raw validation details and document content
- **AND** messages contain only fixed wording and, where relevant, numeric page counts
