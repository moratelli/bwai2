"""Behavior tests for bounded Gemini summarization."""

from dataclasses import dataclass

import httpx
import pytest
from google.genai import types

from document_processor.domain.errors import PermanentProcessingError, RetryableProcessingError
from document_processor.domain.models import PageContent
from document_processor.infrastructure.summarizers.gemini_summarizer import GeminiSummarizer


@dataclass
class FakeResponse:
    text: str | None


@dataclass
class CapturedRequest:
    model: str
    contents: str
    config: types.GenerateContentConfig


class FakeModels:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[CapturedRequest] = []

    async def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: types.GenerateContentConfig,
    ) -> FakeResponse:
        self.requests.append(CapturedRequest(model, contents, config))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@dataclass
class FakeAsyncClient:
    models: FakeModels


@dataclass
class FakeClient:
    aio: FakeAsyncClient


class FakeProviderError(Exception):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"sensitive provider error {code}")


def summarizer_with(
    responses: list[str | Exception | None],
    *,
    chunk_size: int = 1_000,
    model: str = "gemini-3.1-flash-lite",
) -> tuple[GeminiSummarizer, FakeModels]:
    models = FakeModels(
        [
            FakeResponse(response) if not isinstance(response, Exception) else response
            for response in responses
        ]
    )
    summarizer = GeminiSummarizer(
        client=FakeClient(FakeAsyncClient(models)),
        model=model,
        chunk_size=chunk_size,
    )
    return summarizer, models


@pytest.mark.asyncio
async def test_summarizes_small_document_in_one_request() -> None:
    summarizer, models = summarizer_with(["## Summary\n\nConcise result."])

    summary = await summarizer.summarize(
        [PageContent(index=0, markdown="Alpha"), PageContent(index=1, markdown="Beta")]
    )

    assert summary == "## Summary\n\nConcise result."
    assert len(models.requests) == 1
    request = models.requests[0]
    assert request.model == "gemini-3.1-flash-lite"
    assert "## Page 1\n\nAlpha" in request.contents
    assert "## Page 2\n\nBeta" in request.contents
    assert request.config.temperature == 0


@pytest.mark.asyncio
async def test_chunks_only_at_page_boundaries_then_synthesizes_once() -> None:
    summarizer, models = summarizer_with(
        ["Alpha summary", "Beta summary", "Final summary"],
        chunk_size=24,
        model="configured-model",
    )

    summary = await summarizer.summarize(
        [PageContent(index=0, markdown="Alpha text"), PageContent(index=1, markdown="Beta text")]
    )

    assert summary == "Final summary"
    assert len(models.requests) == 3
    assert all(request.model == "configured-model" for request in models.requests)
    assert "Alpha text" in models.requests[0].contents
    assert "Beta text" not in models.requests[0].contents
    assert "Beta text" in models.requests[1].contents
    assert "Alpha text" not in models.requests[1].contents
    assert "Alpha summary" in models.requests[2].contents
    assert "Beta summary" in models.requests[2].contents


@pytest.mark.asyncio
async def test_empty_extraction_fails_without_provider_request() -> None:
    summarizer, models = summarizer_with([])

    with pytest.raises(PermanentProcessingError) as caught:
        await summarizer.summarize(
            [PageContent(index=0, markdown=""), PageContent(index=1, markdown="  ")]
        )

    assert caught.value.code == "empty_extraction"
    assert str(caught.value) == (
        "No readable text was extracted. "
        "If you used Fast text, it cannot read scanned pages; "
        "try Rich Markdown (Gemini) or a text-based PDF."
    )
    assert models.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, "", "   "])
async def test_rejects_empty_provider_summary(response: str | None) -> None:
    summarizer, _models = summarizer_with([response])

    with pytest.raises(PermanentProcessingError) as caught:
        await summarizer.summarize([PageContent(index=0, markdown="Text")])

    assert caught.value.code == "invalid_provider_response"
    assert str(caught.value) == "Gemini returned an invalid summary."


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [408, 409, 429, 500, 503])
async def test_maps_temporary_provider_errors_to_safe_retryable_error(code: int) -> None:
    summarizer, _models = summarizer_with([FakeProviderError(code)])

    with pytest.raises(RetryableProcessingError) as caught:
        await summarizer.summarize([PageContent(index=0, markdown="Text")])

    assert caught.value.code == "gemini_temporarily_unavailable"
    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_maps_transport_timeout_to_safe_retryable_error() -> None:
    summarizer, _models = summarizer_with([httpx.ReadTimeout("sensitive timeout")])

    with pytest.raises(RetryableProcessingError) as caught:
        await summarizer.summarize([PageContent(index=0, markdown="Text")])

    assert caught.value.code == "gemini_temporarily_unavailable"
    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_maps_permanent_provider_error_without_leaking_payload() -> None:
    summarizer, _models = summarizer_with([FakeProviderError(400)])

    with pytest.raises(PermanentProcessingError) as caught:
        await summarizer.summarize([PageContent(index=0, markdown="Text")])

    assert caught.value.code == "gemini_rejected_document"
    assert str(caught.value) == "Gemini could not summarize this document."
