"""Every Gemini generation crosses admission before it reaches the SDK."""

import asyncio
import json
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from google import genai
from google.genai import errors, types

from document_processor.domain.errors import InfrastructureUnavailableError, ProcessingError
from document_processor.domain.models import PageContent
from document_processor.domain.provider import RetrySchedule
from document_processor.infrastructure.parsers.gemini_parser import GeminiPdfParser
from document_processor.infrastructure.summarizers.gemini_summarizer import GeminiSummarizer
from document_processor.worker_logging import SafeWorkerFormatter
from tests.unit.test_gemini_parser import PDF_BYTES, client_with
from tests.unit.test_gemini_summarizer import FakeAsyncClient, FakeClient, FakeModels


@pytest.mark.parametrize("stage", ["extraction", "summarization"])
def test_sdk_retries_are_explicitly_disabled(stage: str, monkeypatch: pytest.MonkeyPatch) -> None:
    options: dict[str, object] = {}

    def client(**kwargs: object) -> object:
        options.update(kwargs)
        return object()

    monkeypatch.setattr(genai, "Client", client)
    if stage == "extraction":
        GeminiPdfParser(api_key="test-only", model="test")
    else:
        GeminiSummarizer(api_key="test-only", model="test", chunk_size=100)
    http_options = cast(types.HttpOptions, options["http_options"])
    assert http_options.retry_options is not None
    assert http_options.retry_options.attempts == 1


UNTRUSTED_USAGE_MARKER = "PRIVATE_CANARY"


class Gate:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def acquire(self) -> None:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error

    async def cool_down(self, seconds: float) -> RetrySchedule:
        raise AssertionError("The worker, not the request helper, coordinates retry cooldowns.")


async def test_extraction_is_gated_before_sdk_call() -> None:
    gate = Gate()
    gate.release.clear()
    client, models = client_with(
        '{"pages":[{"index":0,"markdown":"a"},{"index":1,"markdown":"b"}]}'
    )
    parser = GeminiPdfParser(client=client, model="test", request_gate=gate)
    task = asyncio.create_task(parser.parse(PDF_BYTES))
    try:
        await asyncio.wait_for(gate.entered.wait(), timeout=2)
        assert models.requests == []
        gate.release.set()
        assert len(await asyncio.wait_for(task, timeout=2)) == 2
        assert gate.calls == len(models.requests) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("reduction", [False, True])
async def test_every_summary_stage_is_individually_admitted(reduction: bool) -> None:
    gate = Gate()
    models = FakeModels([])

    def reply(prompt: str) -> str:
        assert gate.calls == len(models.requests)
        return "x" * 80 if prompt.startswith("Summarize this section") else "ok"

    models.reply = reply
    summarizer = GeminiSummarizer(
        client=FakeClient(FakeAsyncClient(models)), model="test", chunk_size=100, request_gate=gate
    )
    pages = (
        [PageContent(0, "a" * 80), PageContent(1, "b" * 80)] if reduction else [PageContent(0, "a")]
    )
    assert await summarizer.summarize(pages) == "ok"
    assert gate.calls == len(models.requests)
    if reduction:
        assert any(r.contents.startswith("Compress") for r in models.requests)
        assert models.requests[-1].contents.startswith("Synthesize")
    else:
        assert gate.calls == 1


@pytest.mark.parametrize("stage", ["extraction", "summarization"])
async def test_coordination_failure_never_calls_or_blames_gemini(
    stage: Literal["extraction", "summarization"], caplog: pytest.LogCaptureFixture
) -> None:
    error = InfrastructureUnavailableError("Request coordination")
    gate = Gate(error)
    pdf_client, pdf_models = client_with(None)
    summary_models = FakeModels([])
    with caplog.at_level(logging.INFO), pytest.raises(InfrastructureUnavailableError) as caught:
        if stage == "extraction":
            await GeminiPdfParser(client=pdf_client, model="test", request_gate=gate).parse(
                PDF_BYTES
            )
        else:
            await GeminiSummarizer(
                client=FakeClient(FakeAsyncClient(summary_models)),
                model="test",
                chunk_size=100,
                request_gate=gate,
            ).summarize([PageContent(0, "a")])
    assert caught.value is error
    assert pdf_models.requests == []
    assert summary_models.requests == []
    assert not any(getattr(r, "event", "").startswith("gemini_request") for r in caplog.records)


@pytest.mark.parametrize(
    "status,category", [(429, "rate_limited"), (503, "unavailable"), (403, None)]
)
async def test_failed_request_logs_safe_status_not_provider_body(
    status: int, category: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    models = FakeModels([errors.APIError(status, {"message": "PRIVATE_PROVIDER_CANARY"})])
    with caplog.at_level(logging.INFO), pytest.raises(ProcessingError):
        await GeminiSummarizer(
            client=FakeClient(FakeAsyncClient(models)),
            model="test",
            chunk_size=100,
            request_gate=Gate(),
        ).summarize([PageContent(0, "a")])
    records = [r for r in caplog.records if getattr(r, "event", None) == "gemini_request_failed"]
    assert len(records) == 1
    output = SafeWorkerFormatter().format(records[0])
    payload = cast(dict[str, object], json.loads(output))
    assert payload["http_status"] == status
    assert payload["provider_category"] == category
    assert payload["provider_stage"] == "summarization"
    assert payload["prompt_token_count"] is None
    assert payload["candidates_token_count"] is None
    assert "CANARY" not in output


async def test_cancelled_admission_never_calls_sdk() -> None:
    gate = Gate()
    gate.release.clear()
    models = FakeModels([])
    summarizer = GeminiSummarizer(
        client=FakeClient(FakeAsyncClient(models)), model="test", chunk_size=100, request_gate=gate
    )
    task = asyncio.create_task(summarizer.summarize([PageContent(0, "a")]))
    try:
        await asyncio.wait_for(gate.entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert models.requests == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@dataclass
class Response:
    usage_metadata: object
    text: str = "PRIVATE_RESULT_CANARY"
    candidates: list[types.Candidate] | None = None


@pytest.mark.parametrize(
    "usage,expected",
    [
        (SimpleNamespace(prompt_token_count=123, candidates_token_count=45), (123, 45)),
        (None, (None, None)),
        (SimpleNamespace(prompt_token_count=True, candidates_token_count=-1), (None, None)),
        (
            SimpleNamespace(
                prompt_token_count=UNTRUSTED_USAGE_MARKER, candidates_token_count=float("inf")
            ),
            (None, None),
        ),
    ],
)
async def test_logs_only_valid_returned_usage(
    usage: object, expected: tuple[int | None, int | None], caplog: pytest.LogCaptureFixture
) -> None:
    models = FakeModels([Response(usage)])
    with caplog.at_level(logging.INFO):
        await GeminiSummarizer(
            client=FakeClient(FakeAsyncClient(models)),
            model="test",
            chunk_size=100,
            request_gate=Gate(),
        ).summarize([PageContent(0, "PRIVATE_SOURCE_CANARY")])
    records = [r for r in caplog.records if getattr(r, "event", None) == "gemini_request_completed"]
    assert len(records) == 1
    output = SafeWorkerFormatter().format(records[0])
    payload = cast(dict[str, object], json.loads(output))
    assert payload["provider"] == "gemini"
    assert payload["provider_stage"] == "summarization"
    assert (payload["prompt_token_count"], payload["candidates_token_count"]) == expected
    assert isinstance(payload["duration_ms"], float)
    assert payload["duration_ms"] >= 0
    assert "CANARY" not in output
