"""Optional result metadata must not change general API errors or legacy results."""

from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest
from pydantic import ValidationError

from document_processor.api import schemas
from document_processor.domain.models import DocumentStatus
from document_processor.domain.provider import (
    AttemptFailure,
    ProcessingStage,
    ProviderCategory,
    ProviderFailure,
)
from tests.api.test_document_results import (
    COMPLETED_AT,
    DOCUMENT_ID,
    UPDATED_AT,
    api_client,
    record,
)


@pytest.mark.parametrize(
    "category,status",
    [
        ("rate_limited", 429),
        ("unavailable", 503),
        ("timeout", None),
        ("connection_error", None),
        ("temporary_failure", 408),
    ],
)
@pytest.mark.parametrize("state", [DocumentStatus.PROCESSING, DocumentStatus.FAILED])
async def test_exposes_only_confirmed_result_cause(
    category: ProviderCategory, status: int | None, state: DocumentStatus
) -> None:
    document = replace(
        record(state),
        last_failure=AttemptFailure(1, ProviderFailure(category, "summarization", status)),
        retry_not_before=UPDATED_AT + timedelta(seconds=60),
        error_code="retry_exhausted",
        error_message="Safe failure.",
        completed_at=COMPLETED_AT,
    )
    async with api_client(document) as client:
        response = await client.get(f"/api/v1/documents/{DOCUMENT_ID}")
    assert response.status_code == 200
    body = response.json()
    cause = {"provider": "gemini", "category": category, "stage": "summarization", "attempt": 1}
    if status is not None:
        cause["http_status"] = status
    if state is DocumentStatus.PROCESSING:
        assert body["retry_wait"] == {"until": "2026-10-23T12:02:00Z", "cause": cause}
        assert "error" not in body
    else:
        assert body["error"] == {
            "code": "retry_exhausted",
            "message": "Safe failure.",
            "cause": cause,
        }
        assert "retry_wait" not in body
    assert "pages" not in body and "summary" not in body


@pytest.mark.parametrize(
    "failure",
    [
        None,
        AttemptFailure(1, None),
        AttemptFailure(0, ProviderFailure("unavailable", "extraction", 503)),
        AttemptFailure(True, ProviderFailure("unavailable", "extraction", 503)),
        AttemptFailure(2, ProviderFailure("unavailable", "extraction", 503)),
        AttemptFailure(1, ProviderFailure("rate_limited", "extraction", 503)),
        AttemptFailure(
            1, ProviderFailure("unavailable", cast(ProcessingStage, "PRIVATE_CANARY"), 503)
        ),
    ],
)
async def test_invalid_or_absent_optional_evidence_is_omitted(
    failure: AttemptFailure | None,
) -> None:
    for state in [DocumentStatus.PROCESSING, DocumentStatus.FAILED]:
        document = replace(
            record(state),
            last_failure=failure,
            retry_not_before=UPDATED_AT,
            error_code="retry_exhausted",
            error_message="Generic failure.",
            completed_at=COMPLETED_AT,
        )
        async with api_client(document) as client:
            response = await client.get(f"/api/v1/documents/{DOCUMENT_ID}")
        body = response.json()
        assert response.status_code == 200 and "retry_wait" not in body
        assert "cause" not in body.get("error", {}) and "PRIVATE_CANARY" not in response.text


async def test_resumed_attempt_omits_wait_but_exhaustion_can_identify_prior_attempt() -> None:
    failure = AttemptFailure(1, ProviderFailure("rate_limited", "extraction", 429))
    processing = replace(record(DocumentStatus.PROCESSING), attempts=2, last_failure=failure)
    async with api_client(processing) as client:
        assert "retry_wait" not in (await client.get(f"/api/v1/documents/{DOCUMENT_ID}")).json()
    message = "Last confirmed failure on attempt 1: Gemini API limit reached during extraction."
    failed = replace(
        processing,
        status=DocumentStatus.FAILED,
        attempts=3,
        completed_at=COMPLETED_AT,
        error_code="retry_exhausted",
        error_message=message,
    )
    async with api_client(failed) as client:
        body = (await client.get(f"/api/v1/documents/{DOCUMENT_ID}")).json()
    assert body["error"]["cause"]["attempt"] == 1 and body["error"]["message"] == message


async def test_permanent_failures_do_not_inherit_provider_cause() -> None:
    document = replace(
        record(DocumentStatus.FAILED),
        last_failure=AttemptFailure(1, ProviderFailure("unavailable", "extraction", 503)),
        completed_at=COMPLETED_AT,
        error_code="invalid_pdf",
        error_message="Invalid PDF.",
    )
    async with api_client(document) as client:
        body = (await client.get(f"/api/v1/documents/{DOCUMENT_ID}")).json()
    assert body["error"] == {"code": "invalid_pdf", "message": "Invalid PDF."}


@pytest.mark.parametrize("attempt", [0, -1, True, 1.5])
def test_public_cause_schema_requires_positive_integral_attempt(attempt: object) -> None:
    with pytest.raises(ValidationError):
        schemas.ProviderCauseResponse.model_validate(
            {
                "provider": "gemini",
                "category": "timeout",
                "stage": "summarization",
                "attempt": attempt,
            }
        )


def test_general_api_error_schema_has_not_gained_cause() -> None:
    assert set(schemas.ErrorDetail.model_fields) == {"code", "message"}
