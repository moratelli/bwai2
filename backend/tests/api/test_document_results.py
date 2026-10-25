"""Document result polling API tests."""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from document_processor.api.app import create_app
from document_processor.config import Settings
from document_processor.domain.models import (
    DocumentRecord,
    DocumentStatus,
    NewDocument,
    PageContent,
    ParserKind,
)

DOCUMENT_ID = UUID("00000000-0000-4000-8000-000000000001")
UNKNOWN_ID = UUID("00000000-0000-4000-8000-000000000099")
CREATED_AT = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
UPDATED_AT = datetime(2026, 10, 23, 12, 1, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 10, 23, 12, 2, tzinfo=UTC)


class NoopGateway:
    async def create_batch(self, documents: Sequence[NewDocument]) -> None:
        del documents


@dataclass
class FakeDocumentReader:
    documents: dict[UUID, DocumentRecord]

    async def get(self, document_id: UUID) -> DocumentRecord | None:
        return self.documents.get(document_id)


def record(status: DocumentStatus) -> DocumentRecord:
    return DocumentRecord(
        id=DOCUMENT_ID,
        filename="report.pdf",
        parser=ParserKind.PYPDF,
        status=status,
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
        attempts=1 if status is not DocumentStatus.QUEUED else 0,
    )


@asynccontextmanager
async def api_client(document: DocumentRecord | None) -> AsyncGenerator[AsyncClient]:
    documents = {document.id: document} if document is not None else {}
    app = create_app(
        settings=Settings(gemini_api_key=SecretStr("test-key")),
        submission_gateway=NoopGateway(),
        document_reader=FakeDocumentReader(documents),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("document_status", [DocumentStatus.QUEUED, DocumentStatus.PROCESSING])
async def test_returns_non_terminal_state_without_partial_result(
    document_status: DocumentStatus,
) -> None:
    async with api_client(record(document_status)) as client:
        response = await client.get(f"/api/v1/documents/{DOCUMENT_ID}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "id": str(DOCUMENT_ID),
        "filename": "report.pdf",
        "parser": "pypdf",
        "status": document_status.value,
        "created_at": CREATED_AT.isoformat().replace("+00:00", "Z"),
        "updated_at": UPDATED_AT.isoformat().replace("+00:00", "Z"),
        "attempts": 0 if document_status is DocumentStatus.QUEUED else 1,
    }


@pytest.mark.asyncio
async def test_returns_completed_summary_and_all_pages_in_source_order() -> None:
    document = replace(
        record(DocumentStatus.COMPLETED),
        pages=(
            PageContent(index=0, markdown="# First"),
            PageContent(index=1, markdown=""),
        ),
        summary="## Summary\n\nResult",
        completed_at=COMPLETED_AT,
    )

    async with api_client(document) as client:
        response = await client.get(f"/api/v1/documents/{DOCUMENT_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["summary"] == "## Summary\n\nResult"
    assert body["pages"] == [
        {"index": 0, "markdown": "# First"},
        {"index": 1, "markdown": ""},
    ]
    assert body["completed_at"] == COMPLETED_AT.isoformat().replace("+00:00", "Z")
    assert "error" not in body


@pytest.mark.asyncio
async def test_returns_safe_failed_result_without_partial_content() -> None:
    document = replace(
        record(DocumentStatus.FAILED),
        error_code="invalid_pdf",
        error_message="The PDF is corrupt or unsupported.",
        completed_at=COMPLETED_AT,
    )

    async with api_client(document) as client:
        response = await client.get(f"/api/v1/documents/{DOCUMENT_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == {
        "code": "invalid_pdf",
        "message": "The PDF is corrupt or unsupported.",
    }
    assert body["completed_at"] == COMPLETED_AT.isoformat().replace("+00:00", "Z")
    assert "pages" not in body
    assert "summary" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_id", [UNKNOWN_ID, DOCUMENT_ID])
async def test_returns_same_safe_404_for_unknown_or_expired_document(requested_id: UUID) -> None:
    async with api_client(None) as client:
        response = await client.get(f"/api/v1/documents/{requested_id}")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "document_not_found",
            "message": "Document not found or expired.",
        }
    }
    assert str(DOCUMENT_ID) not in response.text
