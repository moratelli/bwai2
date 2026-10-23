"""Behavior tests for Gemini PDF-to-Markdown extraction."""

from dataclasses import dataclass

import pytest
from google.genai import types

from document_processor.domain.errors import PermanentProcessingError, RetryableProcessingError
from document_processor.infrastructure.parsers.gemini_parser import GeminiPdfParser

PDF_BYTES = b"%PDF-1.7\ncontent"


@dataclass
class FakeResponse:
    text: str | None


@dataclass
class CapturedRequest:
    model: str
    contents: list[types.Part]
    config: types.GenerateContentConfig


class FakeModels:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.requests: list[CapturedRequest] = []

    async def generate_content(
        self,
        *,
        model: str,
        contents: list[types.Part],
        config: types.GenerateContentConfig,
    ) -> FakeResponse:
        self.requests.append(CapturedRequest(model, contents, config))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


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


def client_with(response: str | Exception | None) -> tuple[FakeClient, FakeModels]:
    models = FakeModels(FakeResponse(response) if not isinstance(response, Exception) else response)
    return FakeClient(aio=FakeAsyncClient(models=models)), models


@pytest.mark.asyncio
async def test_extracts_schema_valid_markdown_pages_from_inline_pdf() -> None:
    client, models = client_with(
        '{"pages":[{"index":0,"markdown":"# First"},{"index":1,"markdown":"Second"}]}'
    )
    parser = GeminiPdfParser(client=client, model="gemini-3.1-flash-lite")

    pages = await parser.parse(PDF_BYTES)

    assert [(page.index, page.markdown) for page in pages] == [
        (0, "# First"),
        (1, "Second"),
    ]
    assert len(models.requests) == 1
    request = models.requests[0]
    assert request.model == "gemini-3.1-flash-lite"
    assert request.config.response_mime_type == "application/json"
    pdf_parts = [part for part in request.contents if part.inline_data is not None]
    assert len(pdf_parts) == 1
    assert pdf_parts[0].inline_data is not None
    assert pdf_parts[0].inline_data.mime_type == "application/pdf"
    assert pdf_parts[0].inline_data.data == PDF_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        None,
        "not json",
        '{"pages":[]}',
        '{"pages":[{"index":0,"markdown":"A"},{"index":0,"markdown":"B"}]}',
        '{"pages":[{"index":1,"markdown":"B"},{"index":0,"markdown":"A"}]}',
        '{"pages":[{"index":0,"markdown":"A"},{"index":2,"markdown":"C"}]}',
    ],
)
async def test_rejects_invalid_or_unordered_provider_response(response: str | None) -> None:
    client, _models = client_with(response)

    with pytest.raises(PermanentProcessingError) as caught:
        await GeminiPdfParser(client=client, model="test-model").parse(PDF_BYTES)

    assert caught.value.code == "invalid_provider_response"
    assert str(caught.value) == "Gemini returned an invalid page structure."


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [408, 409, 429, 500, 503])
async def test_maps_temporary_provider_errors_to_safe_retryable_error(code: int) -> None:
    client, _models = client_with(FakeProviderError(code))

    with pytest.raises(RetryableProcessingError) as caught:
        await GeminiPdfParser(client=client, model="test-model").parse(PDF_BYTES)

    assert caught.value.code == "gemini_temporarily_unavailable"
    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_maps_permanent_provider_errors_without_leaking_payload() -> None:
    client, _models = client_with(FakeProviderError(400))

    with pytest.raises(PermanentProcessingError) as caught:
        await GeminiPdfParser(client=client, model="test-model").parse(PDF_BYTES)

    assert caught.value.code == "gemini_rejected_document"
    assert str(caught.value) == "Gemini could not process this PDF."
