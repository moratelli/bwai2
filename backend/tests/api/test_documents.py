from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from document_processor.api.app import create_app
from document_processor.config import Settings
from document_processor.domain.errors import InfrastructureUnavailableError
from document_processor.domain.models import NewDocument


@dataclass
class RecordingGateway:
    batches: list[tuple[NewDocument, ...]] = field(
        default_factory=lambda: list[tuple[NewDocument, ...]]()
    )

    async def create_batch(self, documents: Sequence[NewDocument]) -> None:
        self.batches.append(tuple(documents))


class FailingGateway:
    async def create_batch(self, documents: Sequence[NewDocument]) -> None:
        del documents
        raise InfrastructureUnavailableError("Document queue")


@asynccontextmanager
async def api_client(
    *,
    gateway: RecordingGateway | FailingGateway | None = None,
    gemini: bool = True,
    max_files: int = 10,
    max_bytes: int = 20 * 1024 * 1024,
) -> AsyncGenerator[tuple[AsyncClient, RecordingGateway | FailingGateway]]:
    selected_gateway = gateway or RecordingGateway()
    settings = Settings(
        gemini_api_key=SecretStr("gemini-test") if gemini else None,
        max_files_per_batch=max_files,
        max_file_size_bytes=max_bytes,
    )
    app = create_app(settings=settings, submission_gateway=selected_gateway)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client, selected_gateway


def pdf_file(
    name: str = "document.pdf", content: bytes = b"%PDF-1.7\ncontent"
) -> tuple[str, bytes, str]:
    return name, content, "application/pdf"


@pytest.mark.asyncio
async def test_uploads_multiple_pdfs() -> None:
    async with api_client() as (client, gateway):
        response = await client.post(
            "/api/v1/documents",
            data={"parser": "pypdf"},
            files=[("files", pdf_file("one.pdf")), ("files", pdf_file("two.pdf"))],
        )

    assert response.status_code == 202
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert len(body["documents"]) == 2
    assert [item["filename"] for item in body["documents"]] == ["one.pdf", "two.pdf"]
    assert {item["parser"] for item in body["documents"]} == {"pypdf"}
    assert {item["status"] for item in body["documents"]} == {"queued"}
    assert isinstance(gateway, RecordingGateway)
    assert len(gateway.batches) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "files", "expected_code"),
    [
        ({"parser": "automatic"}, [("files", pdf_file())], "invalid_request"),
        ({"parser": "pypdf"}, [], "invalid_request"),
        (
            {"parser": "pypdf"},
            [("files", ("notes.txt", b"not pdf", "text/plain"))],
            "invalid_media_type",
        ),
        (
            {"parser": "pypdf"},
            [("files", ("fake.pdf", b"not pdf", "application/pdf"))],
            "invalid_pdf",
        ),
    ],
)
async def test_returns_safe_422_errors(
    data: dict[str, str],
    files: list[tuple[str, tuple[str, bytes, str]]],
    expected_code: str,
) -> None:
    async with api_client() as (client, _gateway):
        response = await client.post("/api/v1/documents", data=data, files=files)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code
    assert set(response.json()["error"]) == {"code", "message"}


@pytest.mark.asyncio
async def test_rejects_too_many_files_with_413() -> None:
    async with api_client(max_files=2) as (client, gateway):
        response = await client.post(
            "/api/v1/documents",
            data={"parser": "pypdf"},
            files=[("files", pdf_file(f"{index}.pdf")) for index in range(3)],
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_many_files"
    assert isinstance(gateway, RecordingGateway)
    assert gateway.batches == []


@pytest.mark.asyncio
async def test_stops_reading_oversized_file_and_returns_413() -> None:
    async with api_client(max_bytes=10) as (client, gateway):
        response = await client.post(
            "/api/v1/documents",
            data={"parser": "pypdf"},
            files=[("files", pdf_file(content=b"%PDF-" + b"x" * 6))],
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert isinstance(gateway, RecordingGateway)
    assert gateway.batches == []


@pytest.mark.asyncio
async def test_returns_503_when_gemini_is_not_configured() -> None:
    async with api_client(gemini=False) as (client, gateway):
        response = await client.post(
            "/api/v1/documents",
            data={"parser": "pypdf"},
            files=[("files", pdf_file())],
        )

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "provider_unavailable",
            "message": "Gemini is not configured for this processing mode.",
        }
    }
    assert isinstance(gateway, RecordingGateway)
    assert gateway.batches == []


@pytest.mark.asyncio
@pytest.mark.parametrize("gemini", [True, False])
async def test_rejects_retired_parser_before_provider_checks_or_job_creation(gemini: bool) -> None:
    async with api_client(gemini=gemini) as (client, gateway):
        response = await client.post(
            "/api/v1/documents",
            data={"parser": "mistral"},
            files=[("files", pdf_file())],
        )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert "pypdf" in error["message"]
    assert "gemini" in error["message"]
    assert "mistral" not in error["message"]
    assert isinstance(gateway, RecordingGateway)
    assert gateway.batches == []


@pytest.mark.asyncio
async def test_returns_503_when_atomic_queue_publication_fails() -> None:
    async with api_client(gateway=FailingGateway()) as (client, _gateway):
        response = await client.post(
            "/api/v1/documents",
            data={"parser": "pypdf"},
            files=[("files", pdf_file())],
        )

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "infrastructure_unavailable",
            "message": "Document queue is temporarily unavailable.",
        }
    }
