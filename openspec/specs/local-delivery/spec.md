# local-delivery Specification

## Purpose
Defines reproducible container startup, repository-owned developer commands, continuous integration checks, and publishable container artifacts.

## Requirements

### Requirement: Start the full application with Docker Compose
The repository SHALL provide one documented Docker Compose command that starts Redis, the API, a background worker, and the web application with health-aware dependencies.

#### Scenario: Configured local startup
- **WHEN** a developer supplies the documented environment values and runs the startup command
- **THEN** the complete stack becomes healthy and the application is reachable through one documented browser URL

#### Scenario: Redis is not ready
- **WHEN** the API or worker starts before Redis is healthy
- **THEN** the stack waits or retries without requiring a manual service restart

#### Scenario: Required configuration is missing
- **WHEN** the stack starts without a Gemini API key
- **THEN** containers remain diagnosable and readiness clearly reports that document submission is unavailable

### Requirement: Keep secrets outside images and source control
The repository MUST read provider credentials from runtime environment variables and MUST provide only placeholder names and setup guidance in tracked files.

#### Scenario: Container images are built
- **WHEN** backend and frontend images are built
- **THEN** no provider credential is present in an image layer, build argument, frontend bundle, or tracked configuration

#### Scenario: Developer configures local credentials
- **WHEN** a developer follows the setup guide
- **THEN** credentials remain in an ignored local environment file or shell environment

### Requirement: Provide a root developer interface
The repository SHALL provide a Makefile that delegates setup, formatting, linting, type checking, testing, building, and container lifecycle commands to the checked-in backend and frontend toolchains.

#### Scenario: Developer runs the aggregate check
- **WHEN** a developer runs `make check`
- **THEN** backend formatting, linting, typing, and tests plus frontend formatting, linting, typing, tests, and build checks run non-interactively

#### Scenario: Developer formats the repository
- **WHEN** a developer runs `make format`
- **THEN** the uv-managed Python formatter and repository-pinned frontend formatter update their owned files

#### Scenario: Developer installs dependencies
- **WHEN** a developer runs the documented setup target on a supported workstation
- **THEN** backend dependencies are synchronized from the uv lockfile and frontend dependencies are installed from the npm lockfile

### Requirement: Use reproducible project toolchains
The backend SHALL use Python 3.12 and uv for dependency locking and command execution, while the frontend SHALL use strict TypeScript, Vite with SWC, Oxc formatting and linting, and one-shot Vitest tests.

#### Scenario: Backend check runs
- **WHEN** backend quality commands run locally or in CI
- **THEN** they execute through uv against the committed lockfile

#### Scenario: Frontend check runs
- **WHEN** frontend quality commands run locally or in CI
- **THEN** pinned package scripts run Oxc, TypeScript checking, Vitest in run mode, and the production build

### Requirement: Enforce pull request quality gates
GitHub Actions SHALL run backend and frontend checks plus container build validation for pull requests to `main` and pushes to `dev` or `main`.

#### Scenario: A quality gate fails
- **WHEN** any required format, lint, type, test, or build command fails
- **THEN** the workflow fails and identifies the failing command

#### Scenario: All quality gates pass
- **WHEN** every required command and container build succeeds
- **THEN** the workflow reports success without requiring provider credentials or live provider calls

### Requirement: Publish container images after main updates
GitHub Actions SHALL build and publish versioned backend and frontend container images to GitHub Container Registry after a successful push to `main`.

#### Scenario: Main branch build succeeds
- **WHEN** a commit reaches `main` and all delivery checks pass
- **THEN** the workflow publishes traceable images using the repository's GitHub token and commit metadata

#### Scenario: Pull request validation runs
- **WHEN** a pull request targets `main`
- **THEN** images are built for validation but are not published

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
