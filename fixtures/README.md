# Synthetic quality report

`quality-report.pdf` contains fictional figures only. Page one has a ruled revenue table: North USD 120,000, South USD 80,000, and total USD 200,000. Page two records operating costs of USD 140,000 and operating profit of USD 60,000. `quality-report.expected.json` records the acceptance facts independently of the generator.

From the repository root, regenerate the PDF with the locked development environment:

```bash
uv run --project backend python scripts/generate_quality_pdf.py
uv run --project backend pytest backend/tests/test_quality_fixture.py
```

The tests compare two generator runs with the committed bytes and check the facts on each page.

## Run the live check

With the existing Gemini credential in your environment or untracked `.env`, run from the repository root:

```bash
make live-quality
```

This command contacts Gemini and uses provider quota. It creates a unique `bwai2-quality-*` Compose project and lets Docker choose a localhost port. It ignores an existing `APP_URL` and does not target your running stack. The runner forces the real summarizer, one worker, and one admitted attempt, then verifies the worker configuration before uploading the fixture. It does not print or modify the credential.

The run has a five-minute budget, with thirty seconds reserved for cleanup, and bounded individual HTTP calls. It submits once, checks the resulting job identity, attempt count and facts, and removes only its own containers and volume. The JSON report includes an explicit cleanup check; a failed cleanup or fact assertion makes the command fail. SDK-level retries remain unchanged.

The runner does not retry a failed job automatically. Each manual rerun makes new provider calls. Investigate a failed factual check rather than rerunning until it passes. One successful run establishes this fixture/model result only, not general document quality or exactly-once provider execution.

`make live-quality` selects Gemini extraction. To check PyPDF extraction with a real Gemini summary, submit this fixture with `parser=pypdf` to a normally configured stack, then run the exported-result checker below. There is no dedicated PyPDF live-quality Make target. CI runs the fake-HTTP runner tests without credentials or Docker.

## Check an exported result

Save the completed API result as JSON, then run:

```bash
uv run --project backend python scripts/check_quality_result.py .tmp/result.json
```

Use `-` instead of a filename to read standard input. The command returns zero only when all checks pass. It prints check names and booleans, never the supplied page text or summary. No credential is needed.

The checker requires two non-empty pages in index order, their page-specific labelled facts, and both **Total revenue** and **Operating profit** in the summary. A bare number under another label does not count. Repeated matches for the same label must agree.

Supported formatting includes whitespace and Markdown decoration, optional `USD` or `$` prefixes, comma-separated or plain integers, decimal equivalents such as `200000.00`, and `k` notation such as `200k`. Labels must precede their amounts; a colon, equals sign, or a short connector (`was`, `is`, `of`, `amounts to`) is allowed. The checker does not recognize arbitrary paraphrases or reconstruct separated table columns.

This is a small factual spot check. It does not grade layout, scans, handwriting, other languages, or every possible contradiction. A valid paraphrase can fail its conservative matching rules. A passing result does not establish which provider produced it or authenticate an arbitrary JSON file. Review a failed assertion rather than loosening the checker to make one model response pass.
