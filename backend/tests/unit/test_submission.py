from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import SecretStr

from document_processor.config import Settings
from document_processor.domain.errors import (
    EmptyBatchError,
    FileTooLargeError,
    InvalidMediaTypeError,
    InvalidPdfError,
    ProviderUnavailableError,
    TooManyFilesError,
)
from document_processor.domain.models import (
    DocumentStatus,
    NewDocument,
    ParserKind,
    UploadCandidate,
)
from document_processor.services.submission import SubmissionService


@dataclass
class RecordingGateway:
    batches: list[tuple[NewDocument, ...]] = field(default_factory=lambda: [])

    async def create_batch(self, documents: Sequence[NewDocument]) -> None:
        self.batches.append(tuple(documents))


def pdf(
    name: str = "document.pdf",
    *,
    content: bytes = b"%PDF-1.7\ncontent",
    content_type: str = "application/pdf",
) -> UploadCandidate:
    return UploadCandidate(filename=name, content_type=content_type, content=content)


def settings(*, gemini: bool = True) -> Settings:
    return Settings(gemini_api_key=SecretStr("gemini-test") if gemini else None)


def service(
    gateway: RecordingGateway,
    *,
    app_settings: Settings | None = None,
) -> SubmissionService:
    identifiers = iter(
        [
            UUID("00000000-0000-4000-8000-000000000001"),
            UUID("00000000-0000-4000-8000-000000000002"),
        ]
    )
    return SubmissionService(
        gateway=gateway,
        settings=app_settings or settings(),
        id_factory=lambda: next(identifiers),
        clock=lambda: datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
    )


@pytest.mark.parametrize("value", ["pypdf", "gemini"])
def test_parser_kind_accepts_supported_values(value: str) -> None:
    assert ParserKind(value).value == value


@pytest.mark.parametrize("value", ["automatic", "mistral"])
def test_parser_kind_rejects_unsupported_values(value: str) -> None:
    with pytest.raises(ValueError):
        ParserKind(value)


@pytest.mark.asyncio
async def test_accepts_multiple_pdfs_atomically() -> None:
    gateway = RecordingGateway()

    accepted = await service(gateway).submit(
        [pdf("one.pdf"), pdf("../two.pdf")],
        ParserKind.PYPDF,
    )

    assert [item.status for item in accepted] == [DocumentStatus.QUEUED] * 2
    assert [str(item.id) for item in accepted] == [
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    ]
    assert [item.filename for item in accepted] == ["one.pdf", "two.pdf"]
    assert len(gateway.batches) == 1
    assert [document.parser for document in gateway.batches[0]] == [ParserKind.PYPDF] * 2
    assert all(document.status is DocumentStatus.QUEUED for document in gateway.batches[0])


@pytest.mark.asyncio
async def test_rejects_empty_batch_before_gateway_call() -> None:
    gateway = RecordingGateway()

    with pytest.raises(EmptyBatchError):
        await service(gateway).submit([], ParserKind.PYPDF)

    assert gateway.batches == []


@pytest.mark.asyncio
async def test_rejects_more_than_ten_files() -> None:
    gateway = RecordingGateway()

    with pytest.raises(TooManyFilesError):
        await service(gateway).submit(
            [pdf(f"{index}.pdf") for index in range(11)], ParserKind.PYPDF
        )

    assert gateway.batches == []


@pytest.mark.asyncio
async def test_accepts_file_at_size_limit() -> None:
    gateway = RecordingGateway()
    app_settings = settings()
    content = b"%PDF-" + b"x" * (app_settings.max_file_size_bytes - len(b"%PDF-"))

    accepted = await service(gateway, app_settings=app_settings).submit(
        [pdf(content=content)], ParserKind.PYPDF
    )

    assert len(accepted) == 1


@pytest.mark.asyncio
async def test_rejects_file_above_size_limit() -> None:
    gateway = RecordingGateway()
    app_settings = settings()
    content = b"%PDF-" + b"x" * (app_settings.max_file_size_bytes + 1 - len(b"%PDF-"))

    with pytest.raises(FileTooLargeError):
        await service(gateway, app_settings=app_settings).submit(
            [pdf(content=content)], ParserKind.PYPDF
        )

    assert gateway.batches == []


@pytest.mark.asyncio
async def test_rejects_non_pdf_media_type() -> None:
    gateway = RecordingGateway()

    with pytest.raises(InvalidMediaTypeError):
        await service(gateway).submit(
            [pdf(content_type="text/plain")],
            ParserKind.PYPDF,
        )


@pytest.mark.asyncio
async def test_rejects_spoofed_pdf() -> None:
    gateway = RecordingGateway()

    with pytest.raises(InvalidPdfError):
        await service(gateway).submit(
            [pdf(content=b"not a pdf")],
            ParserKind.PYPDF,
        )


@pytest.mark.asyncio
async def test_validates_entire_batch_before_persistence() -> None:
    gateway = RecordingGateway()

    with pytest.raises(InvalidPdfError):
        await service(gateway).submit(
            [pdf("valid.pdf"), pdf("invalid.pdf", content=b"not a pdf")],
            ParserKind.PYPDF,
        )

    assert gateway.batches == []


@pytest.mark.asyncio
async def test_requires_gemini_for_every_submission() -> None:
    gateway = RecordingGateway()

    with pytest.raises(ProviderUnavailableError, match="Gemini"):
        await service(gateway, app_settings=settings(gemini=False)).submit(
            [pdf()], ParserKind.PYPDF
        )


@pytest.mark.asyncio
async def test_allows_pypdf_with_explicit_smoke_summarizer() -> None:
    gateway = RecordingGateway()
    app_settings = Settings(test_summarizer_enabled=True)

    accepted = await service(gateway, app_settings=app_settings).submit([pdf()], ParserKind.PYPDF)

    assert len(accepted) == 1
    with pytest.raises(ProviderUnavailableError, match="Gemini"):
        await service(gateway, app_settings=app_settings).submit([pdf()], ParserKind.GEMINI)
