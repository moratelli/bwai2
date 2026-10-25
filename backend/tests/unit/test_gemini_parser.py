"""Behavior tests for Gemini PDF-to-Markdown extraction."""

import json
from dataclasses import dataclass
from io import BytesIO

import httpx
import pytest
from google.genai import types
from pypdf import PdfWriter

from document_processor.domain.errors import PermanentProcessingError, RetryableProcessingError
from document_processor.infrastructure.parsers.gemini_parser import (
    GEMINI_DOCUMENT_SCHEMA,
    GeminiPdfParser,
    GeminiResponse,
)


def pdf_bytes(page_count: int = 2) -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


PDF_BYTES = pdf_bytes()


@dataclass
class FakeResponse:
    text: str | None
    candidates: list[types.Candidate] | None = None


@dataclass
class CapturedRequest:
    model: str
    contents: list[types.Part]
    config: types.GenerateContentConfig


class FakeModels:
    def __init__(self, response: GeminiResponse | Exception) -> None:
        self.response = response
        self.requests: list[CapturedRequest] = []

    async def generate_content(
        self,
        *,
        model: str,
        contents: list[types.Part],
        config: types.GenerateContentConfig,
    ) -> GeminiResponse:
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


def test_provider_schema_uses_only_supported_structured_output_keywords() -> None:
    serialized_schema = repr(GEMINI_DOCUMENT_SCHEMA)

    assert GEMINI_DOCUMENT_SCHEMA["type"] == "OBJECT"
    assert "minItems" not in serialized_schema
    assert "additionalProperties" not in serialized_schema
    assert "$defs" not in serialized_schema


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
    ("response", "message"),
    [
        (None, "Gemini returned no extraction output."),
        ("", "Gemini returned no extraction output."),
        (" \n\t", "Gemini returned no extraction output."),
        ("not json", "Gemini returned malformed JSON for the extracted pages."),
        ('{"pages":[', "Gemini returned malformed JSON for the extracted pages."),
        ('{"pages":[]}', "Gemini returned an invalid page structure."),
        ('{"wrong_field":true}', "Gemini returned an invalid page structure."),
        ('{"pages":[{"index":0}]}', "Gemini returned an invalid page structure."),
        (
            '{"pages":[{"index":-1,"markdown":"A"}]}',
            "Gemini returned an invalid page structure.",
        ),
        (
            '{"pages":[{"index":0,"markdown":"A"}]}',
            "Gemini page count mismatch: expected 2, received 1. The result was rejected.",
        ),
        (
            '{"pages":[{"index":0,"markdown":"A"},{"index":1,"markdown":"B"},'
            '{"index":2,"markdown":"C"}]}',
            "Gemini page count mismatch: expected 2, received 3. The result was rejected.",
        ),
        *[
            (
                json.dumps({"pages": [{"index": index, "markdown": "A"} for index in indexes]}),
                "Gemini returned invalid page numbering or order. The result was rejected.",
            )
            for indexes in ([0, 0], [1, 0], [0, 2], [1, 2])
        ],
    ],
)
async def test_explains_rejected_provider_response(response: str | None, message: str) -> None:
    client, _models = client_with(response)

    with pytest.raises(PermanentProcessingError) as caught:
        await GeminiPdfParser(client=client, model="test-model").parse(PDF_BYTES)

    assert caught.value.code == "invalid_provider_response"
    assert str(caught.value) == message


def sdk_candidate(text: str | None, reason: types.FinishReason | None) -> types.Candidate:
    content = None if text is None else types.Content(parts=[types.Part.from_text(text=text)])
    return types.Candidate(content=content, finish_reason=reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        " \n\t",
        '{"pages":[',
        '{"pages":[{"index":0,"markdown":"A"},{"index":1,"markdown":"B"}]}',
    ],
)
async def test_confirmed_output_limit_takes_precedence_over_text(text: str | None) -> None:
    models = FakeModels(
        types.GenerateContentResponse(
            candidates=[sdk_candidate(text, types.FinishReason.MAX_TOKENS)]
        )
    )
    parser = GeminiPdfParser(client=FakeClient(FakeAsyncClient(models)), model="test-model")

    with pytest.raises(PermanentProcessingError) as caught:
        await parser.parse(PDF_BYTES)

    assert caught.value.code == "invalid_provider_response"
    assert str(caught.value) == (
        "Gemini reached its response limit before a complete extraction could be confirmed. "
        "Try a smaller PDF."
    )
    assert len(models.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "UNRECOGNIZED_METADATA_CANARY"])
async def test_absent_or_unrecognized_metadata_does_not_invent_output_limit(
    reason: str | None,
) -> None:
    # Bypass enum parsing to exercise a forward-compatible unknown provider value.
    candidate = types.Candidate.model_construct(finish_reason=reason)
    models = FakeModels(FakeResponse("not json", candidates=[candidate]))

    with pytest.raises(PermanentProcessingError) as caught:
        await GeminiPdfParser(client=FakeClient(FakeAsyncClient(models)), model="test-model").parse(
            PDF_BYTES
        )

    assert str(caught.value) == "Gemini returned malformed JSON for the extracted pages."


@pytest.mark.asyncio
async def test_unused_candidate_limit_does_not_override_consumed_candidate() -> None:
    text = '{"pages":[{"index":0,"markdown":"A"},{"index":1,"markdown":"B"}]}'
    models = FakeModels(
        types.GenerateContentResponse(
            candidates=[
                sdk_candidate(text, types.FinishReason.STOP),
                sdk_candidate("UNUSED_RESPONSE_CANARY", types.FinishReason.MAX_TOKENS),
            ]
        )
    )

    pages = await GeminiPdfParser(
        client=FakeClient(FakeAsyncClient(models)), model="test-model"
    ).parse(PDF_BYTES)

    assert [page.markdown for page in pages] == ["A", "B"]


@pytest.mark.asyncio
async def test_complete_many_page_response_has_no_new_validation_cutoff() -> None:
    text = json.dumps(
        {"pages": [{"index": index, "markdown": f"Synthetic page {index}"} for index in range(240)]}
    )
    models = FakeModels(
        types.GenerateContentResponse(candidates=[sdk_candidate(text, types.FinishReason.STOP)])
    )

    pages = await GeminiPdfParser(
        client=FakeClient(FakeAsyncClient(models)), model="test-model"
    ).parse(pdf_bytes(240))

    assert [page.index for page in pages] == list(range(240))
    assert pages[-1].markdown == "Synthetic page 239"
    assert len(models.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidates",
    [None, [], [types.Candidate.model_construct(finish_reason="UNRECOGNIZED_METADATA_CANARY")]],
)
async def test_complete_response_without_known_finish_reason_still_succeeds(
    candidates: list[types.Candidate] | None,
) -> None:
    models = FakeModels(
        FakeResponse(
            '{"pages":[{"index":0,"markdown":"A"},{"index":1,"markdown":""}]}',
            candidates=candidates,
        )
    )

    pages = await GeminiPdfParser(
        client=FakeClient(FakeAsyncClient(models)), model="test-model"
    ).parse(PDF_BYTES)

    assert [page.markdown for page in pages] == ["A", ""]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [408, 409, 429, 500, 503])
async def test_maps_temporary_provider_errors_to_safe_retryable_error(code: int) -> None:
    client, _models = client_with(FakeProviderError(code))

    with pytest.raises(RetryableProcessingError) as caught:
        await GeminiPdfParser(client=client, model="test-model").parse(PDF_BYTES)

    assert caught.value.code == "gemini_temporarily_unavailable"
    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_maps_transport_timeout_to_safe_retryable_error() -> None:
    client, _models = client_with(httpx.ReadTimeout("sensitive timeout"))

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
