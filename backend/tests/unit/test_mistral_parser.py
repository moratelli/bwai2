"""Behavior tests for Mistral OCR extraction."""

from dataclasses import dataclass

import pytest
from mistralai.models import DocumentURLChunk

from document_processor.domain.errors import PermanentProcessingError, RetryableProcessingError
from document_processor.infrastructure.parsers.mistral_parser import MistralOcrParser

PDF_BYTES = b"%PDF-1.7\nscan"
EXPECTED_DATA_URL = "data:application/pdf;base64,JVBERi0xLjcKc2Nhbg=="


@dataclass
class FakePage:
    index: int
    markdown: str


@dataclass
class FakeResponse:
    pages: list[FakePage]


@dataclass
class CapturedRequest:
    model: str
    document: DocumentURLChunk


class FakeOcr:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.requests: list[CapturedRequest] = []

    def process(self, *, model: str, document: DocumentURLChunk) -> FakeResponse:
        self.requests.append(CapturedRequest(model, document))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@dataclass
class FakeClient:
    ocr: FakeOcr


class FakeProviderError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"sensitive provider error {status_code}")


@pytest.mark.asyncio
async def test_normalizes_pages_from_base64_pdf_request() -> None:
    ocr = FakeOcr(FakeResponse([FakePage(0, "# Scan"), FakePage(1, "")]))
    parser = MistralOcrParser(client=FakeClient(ocr), model="mistral-ocr-latest")

    pages = await parser.parse(PDF_BYTES)

    assert [(page.index, page.markdown) for page in pages] == [(0, "# Scan"), (1, "")]
    assert len(ocr.requests) == 1
    request = ocr.requests[0]
    assert request.model == "mistral-ocr-latest"
    assert request.document.type == "document_url"
    assert request.document.document_url == EXPECTED_DATA_URL


@pytest.mark.asyncio
async def test_runs_synchronous_sdk_in_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, tuple[object, ...]]] = []

    async def immediate_to_thread(function: object, *args: object) -> object:
        calls.append((function, args))
        assert callable(function)
        return function(*args)

    monkeypatch.setattr(
        "document_processor.infrastructure.parsers.mistral_parser.asyncio.to_thread",
        immediate_to_thread,
    )
    parser = MistralOcrParser(
        client=FakeClient(FakeOcr(FakeResponse([FakePage(0, "Text")]))),
        model="test-model",
    )

    pages = await parser.parse(PDF_BYTES)

    assert len(calls) == 1
    assert pages[0].markdown == "Text"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pages",
    [
        [],
        [FakePage(0, "A"), FakePage(0, "B")],
        [FakePage(1, "B"), FakePage(0, "A")],
        [FakePage(0, "A"), FakePage(2, "C")],
    ],
)
async def test_rejects_invalid_or_unordered_pages(pages: list[FakePage]) -> None:
    parser = MistralOcrParser(client=FakeClient(FakeOcr(FakeResponse(pages))), model="test")

    with pytest.raises(PermanentProcessingError) as caught:
        await parser.parse(PDF_BYTES)

    assert caught.value.code == "invalid_provider_response"
    assert str(caught.value) == "Mistral returned an invalid page structure."


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [408, 409, 429, 500, 503])
async def test_maps_temporary_provider_errors_to_safe_retryable_error(status_code: int) -> None:
    parser = MistralOcrParser(
        client=FakeClient(FakeOcr(FakeProviderError(status_code))),
        model="test",
    )

    with pytest.raises(RetryableProcessingError) as caught:
        await parser.parse(PDF_BYTES)

    assert caught.value.code == "mistral_temporarily_unavailable"
    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_maps_permanent_provider_errors_without_leaking_payload() -> None:
    parser = MistralOcrParser(
        client=FakeClient(FakeOcr(FakeProviderError(400))),
        model="test",
    )

    with pytest.raises(PermanentProcessingError) as caught:
        await parser.parse(PDF_BYTES)

    assert caught.value.code == "mistral_rejected_document"
    assert str(caught.value) == "Mistral could not process this PDF."
