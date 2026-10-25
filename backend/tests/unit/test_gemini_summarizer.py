"""Behavior tests for bounded Gemini summarization."""

from collections.abc import Callable
from dataclasses import dataclass

import httpx
import pytest
from google.genai import types

from document_processor.domain.errors import PermanentProcessingError, RetryableProcessingError
from document_processor.domain.models import PageContent
from document_processor.infrastructure.summarizers.gemini_summarizer import (
    GeminiSummarizer,
    GeminiSummaryResponse,
)


@dataclass
class FakeResponse:
    text: str | None
    candidates: list[types.Candidate] | None = None


@dataclass
class CapturedRequest:
    model: str
    contents: str
    config: types.GenerateContentConfig


class FakeModels:
    def __init__(
        self,
        responses: list[GeminiSummaryResponse | Exception],
        *,
        reply: Callable[[str], str] | None = None,
    ) -> None:
        self.responses = responses
        self.reply = reply
        self.requests: list[CapturedRequest] = []

    async def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: types.GenerateContentConfig,
    ) -> GeminiSummaryResponse:
        self.requests.append(CapturedRequest(model, contents, config))
        response = FakeResponse(self.reply(contents)) if self.reply else self.responses.pop(0)
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


def sdk_summary(text: str | None, reason: str | None) -> types.GenerateContentResponse:
    content = None if text is None else types.Content(parts=[types.Part.from_text(text=text)])
    candidate = types.Candidate.model_construct(content=content, finish_reason=reason)
    return types.GenerateContentResponse(candidates=[candidate])


LIMIT_MESSAGE = "Gemini reached its response limit before a complete summary could be confirmed."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", [None, "", " \n", "Partial result", "An apparently complete summary."]
)
@pytest.mark.parametrize("reason", [types.FinishReason.MAX_TOKENS, "MAX_TOKENS"])
async def test_confirmed_limit_takes_precedence_over_summary_text(
    text: str | None, reason: str
) -> None:
    models = FakeModels([sdk_summary(text, reason)])
    summarizer = GeminiSummarizer(
        client=FakeClient(FakeAsyncClient(models)), model="test", chunk_size=1000
    )

    with pytest.raises(PermanentProcessingError) as caught:
        await summarizer.summarize([PageContent(0, "Synthetic source")])

    assert caught.value.code == "invalid_provider_response"
    assert str(caught.value) == LIMIT_MESSAGE
    assert len(models.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prior", "stage"),
    [
        ([], "Summarize this section"),
        (["a"], "Summarize this section"),
        (["a", "b"], "Synthesize"),
        (["x" * 80, "y" * 80], "Compress"),
        (["x" * 80, "y" * 80, "a", "b", "c", "d"], "Synthesize"),
    ],
    ids=["first-section", "second-section", "final", "reduction", "final-after-reduction"],
)
async def test_summary_limit_stops_the_pipeline_at_every_stage(
    prior: list[str], stage: str
) -> None:
    canary = "TRUNCATED_SUMMARY_CANARY"
    responses: list[GeminiSummaryResponse | Exception] = [FakeResponse(text) for text in prior]
    responses += [sdk_summary(canary, types.FinishReason.MAX_TOKENS)]
    responses += [FakeResponse("q")] * 30
    models = FakeModels(responses)
    summarizer = GeminiSummarizer(
        client=FakeClient(FakeAsyncClient(models)), model="test", chunk_size=100
    )

    with pytest.raises(PermanentProcessingError) as caught:
        await summarizer.summarize([PageContent(0, "A" * 80), PageContent(1, "B" * 80)])

    assert caught.value.code == "invalid_provider_response"
    assert str(caught.value) == LIMIT_MESSAGE
    assert len(models.requests) == len(prior) + 1
    assert models.requests[-1].contents.startswith(stage)
    assert all(canary not in request.contents for request in models.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidates",
    [
        None,
        [],
        [types.Candidate()],
        [types.Candidate(finish_reason=types.FinishReason.STOP)],
        [types.Candidate.model_construct(finish_reason="UNKNOWN_METADATA_CANARY")],
    ],
)
@pytest.mark.parametrize("text", [None, "", "  ", "  Valid summary  "])
async def test_missing_or_unrecognized_finish_metadata_keeps_text_validation(
    candidates: list[types.Candidate] | None, text: str | None
) -> None:
    models = FakeModels([FakeResponse(text, candidates)])
    summarizer = GeminiSummarizer(
        client=FakeClient(FakeAsyncClient(models)), model="test", chunk_size=1000
    )
    if text is not None and text.strip():
        assert await summarizer.summarize([PageContent(0, "Source")]) == text.strip()
    else:
        with pytest.raises(PermanentProcessingError) as caught:
            await summarizer.summarize([PageContent(0, "Source")])
        assert caught.value.code == "invalid_provider_response"
        assert str(caught.value) == "Gemini returned an invalid summary."
    assert len(models.requests) == 1


@pytest.mark.asyncio
async def test_unused_candidate_limit_does_not_override_consumed_summary() -> None:
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(parts=[types.Part.from_text(text="Valid summary")]),
                finish_reason=types.FinishReason.STOP,
            ),
            types.Candidate(finish_reason=types.FinishReason.MAX_TOKENS),
        ]
    )
    models = FakeModels([response])
    summarizer = GeminiSummarizer(
        client=FakeClient(FakeAsyncClient(models)), model="test", chunk_size=1000
    )

    assert await summarizer.summarize([PageContent(0, "Source")]) == "Valid summary"
    assert len(models.requests) == 1


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
        chunk_size=100,
        model="configured-model",
    )

    summary = await summarizer.summarize(
        [
            PageContent(index=0, markdown="Alpha text" + "." * 75),
            PageContent(index=1, markdown="Beta text" + "." * 75),
        ]
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


def content_of(request: CapturedRequest) -> str:
    return request.contents.split("\n\n", 1)[1]


@pytest.mark.asyncio
async def test_mixed_pages_keep_source_order_across_split_chunks() -> None:
    summarizer, models = summarizer_with(["q"] * 10, chunk_size=100)
    pages = (PageContent(0, "First"), PageContent(1, "X" * 250), PageContent(2, "Last"))

    await summarizer.summarize(pages)

    rendered = "".join(content_of(request) for request in models.requests[:-1])
    expected = "".join(f"## Page {page.index + 1}\n\n{page.markdown}" for page in pages)
    assert rendered.replace("\n\n", "") == expected.replace("\n\n", "")
    assert all(len(content_of(request)) <= 100 for request in models.requests)
    assert len(models.requests) == 5


@pytest.mark.asyncio
async def test_oversized_page_is_split_without_losing_source_content() -> None:
    page = PageContent(0, "A" * 250)
    summarizer, models = summarizer_with(["a", "b", "c", "Final"], chunk_size=100)

    result = await summarizer.summarize([page])

    assert all(0 < len(content_of(request)) <= 100 for request in models.requests)
    assert "".join(content_of(request) for request in models.requests[:-1]) == (
        "## Page 1\n\n" + page.markdown
    )
    assert page.markdown == "A" * 250
    assert result == "Final"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 15, 16, 17])
async def test_tiny_and_exact_budgets_never_send_oversized_content(limit: int) -> None:
    summarizer, models = summarizer_with(["q"] * 1000, chunk_size=limit)
    try:
        await summarizer.summarize([PageContent(0, "ABCDE")])
    except PermanentProcessingError as error:
        assert error.code == "summary_too_large"
    assert models.requests
    assert all(0 < len(content_of(request)) <= limit for request in models.requests)
    rendered = "## Page 1\n\nABCDE"
    count = (len(rendered) + limit - 1) // limit
    assert "".join(content_of(request) for request in models.requests[:count]) == rendered


@pytest.mark.asyncio
async def test_oversized_partials_are_reduced_before_bounded_final_synthesis() -> None:
    summarizer, models = summarizer_with(
        ["x" * 80, "y" * 80, "a", "b", "c", "d", "Final"],
        chunk_size=100,
    )
    result = await summarizer.summarize([PageContent(0, "A" * 80), PageContent(1, "B" * 80)])

    assert all(len(content_of(request)) <= 100 for request in models.requests)
    assert result == "Final"
    assert len(models.requests) == 7
    assert models.requests[-1].contents.startswith("Synthesize")


@pytest.mark.asyncio
async def test_non_contracting_summaries_fail_safely_without_oversized_request() -> None:
    summarizer, models = summarizer_with(["x" * 100] * 20, chunk_size=80)

    with pytest.raises(PermanentProcessingError) as caught:
        await summarizer.summarize([PageContent(0, "A" * 50), PageContent(1, "B" * 50)])

    assert caught.value.code == "summary_too_large"
    assert all(len(content_of(request)) <= 80 for request in models.requests)
    assert len(models.requests) == 6
    assert "xxxxx" not in str(caught.value)


@pytest.mark.asyncio
async def test_reduction_stops_after_three_passes_even_if_still_contracting() -> None:
    def slowly_reduce(contents: str) -> str:
        if contents.startswith("Summarize this section"):
            return "x" * 1400
        assert contents.startswith("Compress")
        return contents.split("\n\n", 1)[1][:-30]

    models = FakeModels([], reply=slowly_reduce)
    summarizer = GeminiSummarizer(
        client=FakeClient(FakeAsyncClient(models)),
        model="configured-model",
        chunk_size=1000,
    )
    with pytest.raises(PermanentProcessingError) as caught:
        await summarizer.summarize([PageContent(0, "A" * 950), PageContent(1, "B" * 950)])

    assert caught.value.code == "summary_too_large"
    assert len(models.requests) == 14
    assert all(len(content_of(request)) <= 1000 for request in models.requests)
    assert all(not request.contents.startswith("Synthesize") for request in models.requests)


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
