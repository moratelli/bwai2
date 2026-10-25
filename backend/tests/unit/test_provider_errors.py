"""Provider attribution and untrusted retry hints, with no external requests."""

from datetime import UTC, datetime
from typing import Literal

import httpx
import pytest
from google import genai
from google.genai import errors, types

from document_processor.domain.errors import (
    InfrastructureUnavailableError,
    PermanentProcessingError,
    RetryableProcessingError,
)
from document_processor.domain.models import PageContent
from document_processor.infrastructure import provider_errors
from document_processor.infrastructure.parsers.gemini_parser import GeminiPdfParser
from document_processor.infrastructure.summarizers.gemini_summarizer import GeminiSummarizer
from tests.unit.test_gemini_parser import PDF_BYTES

NOW = datetime(2026, 10, 25, 18, tzinfo=UTC)


@pytest.mark.parametrize("status,category", [(429, "rate_limited"), (503, "unavailable")])
@pytest.mark.parametrize("stage", ["extraction", "summarization"])
async def test_adapters_keep_safe_sdk_evidence(
    status: int,
    category: str,
    stage: Literal["extraction", "summarization"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def forbidden_network(*args: object, **kwargs: object) -> httpx.Response:
        pytest.fail("A real HTTP transport must never be used.")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_network)

    def response(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={"Retry-After": "60"},
            json={"error": {"code": status, "message": "PRIVATE_PROVIDER_CANARY"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as transport:
        client = genai.Client(
            api_key="test-only",
            vertexai=False,
            http_options=types.HttpOptions(
                httpx_async_client=transport,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        try:
            with pytest.raises(RetryableProcessingError) as caught:
                if stage == "extraction":
                    await GeminiPdfParser(client=client, model="test-model").parse(PDF_BYTES)
                else:
                    await GeminiSummarizer(
                        client=client, model="test-model", chunk_size=1000
                    ).summarize([PageContent(0, "synthetic text")])
            failure = caught.value.provider_info
            assert failure is not None
            assert failure.provider == "gemini"
            assert failure.category == category
            assert failure.stage == stage
            assert failure.http_status == status
            assert caught.value.retry_after_seconds == 60
            assert caught.value.code == "gemini_temporarily_unavailable"
            assert "PRIVATE_PROVIDER_CANARY" not in str(caught.value)
            assert "PRIVATE_PROVIDER_CANARY" not in repr(failure)
            assert calls == 1
        finally:
            await client.aio.aclose()
            client.close()


def sdk_error(header: str | None = None, retry_info: object = None) -> errors.APIError:
    return errors.ServerError(
        503,
        {"error": {"message": "PRIVATE_PROVIDER_CANARY", "details": retry_info}},
        httpx.Response(503, headers={"Retry-After": header} if header is not None else {}),
    )


@pytest.mark.parametrize(
    "header,expected",
    [
        (None, None),
        ("", None),
        ("0", 0),
        ("60", 60),
        ("0.5", 0.5),
        ("86400", 86400),
        ("86401", None),
        ("-1", None),
        ("NaN", None),
        ("inf", None),
        ("1e300", None),
        ("private", None),
        ("9" * 200, None),
        ("Sun, 25 Oct 2026 18:01:00 GMT", 60),
        ("Sat, 24 Oct 2026 18:00:00 GMT", None),
    ],
)
def test_retry_after_boundaries(header: str | None, expected: float | None) -> None:
    failure = provider_errors.classify_gemini_error(sdk_error(header), "extraction", now=NOW)
    assert isinstance(failure, RetryableProcessingError)
    assert failure.retry_after_seconds == expected


@pytest.mark.parametrize(
    "delay,expected",
    [("90s", 90), ("0.125s", 60), ("-2s", 60), ("NaNs", 60), ("86401s", 60), (90, 60), (None, 60)],
)
def test_retry_info_uses_largest_supported_hint(delay: object, expected: float) -> None:
    details = [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
        {"@type": "private-type", "retryDelay": "80000s"},
        {"message": "PRIVATE_PROVIDER_CANARY"},
    ]
    failure = provider_errors.classify_gemini_error(sdk_error("60", details), "summarization")
    assert isinstance(failure, RetryableProcessingError)
    assert failure.retry_after_seconds == expected


@pytest.mark.parametrize("details", [None, 1, "private", {}, [None, 2, "private"]])
def test_malformed_details_do_not_mask_original_failure(details: object) -> None:
    failure = provider_errors.classify_gemini_error(sdk_error(None, details), "extraction")
    assert isinstance(failure, RetryableProcessingError)
    assert failure.retry_after_seconds is None


@pytest.mark.parametrize(
    "error,category,status",
    [
        (TimeoutError("private"), "timeout", None),
        (httpx.ReadTimeout("private"), "timeout", None),
        (ConnectionError("private"), "connection_error", None),
        (httpx.ConnectError("private"), "connection_error", None),
        (errors.APIError(408, {}), "temporary_failure", 408),
        (errors.APIError(409, {}), "temporary_failure", 409),
        (errors.APIError(500, {}), "unavailable", 500),
    ],
)
def test_transient_categories(error: Exception, category: str, status: int | None) -> None:
    failure = provider_errors.classify_gemini_error(error, "summarization")
    assert isinstance(failure, RetryableProcessingError)
    assert failure.provider_info is not None
    assert failure.provider_info.category == category
    assert failure.provider_info.http_status == status
    assert "private" not in str(failure)


@pytest.mark.parametrize(
    "error",
    [
        InfrastructureUnavailableError("Request coordination"),
        PermanentProcessingError("invalid_pdf", "Invalid PDF."),
        RetryableProcessingError("test", "A safe message."),
    ],
)
def test_domain_errors_are_not_reclassified(error: Exception) -> None:
    assert provider_errors.classify_gemini_error(error, "extraction") is error


def test_permanent_provider_failure_keeps_existing_code() -> None:
    failure = provider_errors.classify_gemini_error(errors.APIError(403, {}), "summarization")
    assert isinstance(failure, PermanentProcessingError)
    assert failure.code == "gemini_rejected_document"
    assert str(failure) == "Gemini could not summarize this document."
