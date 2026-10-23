"""Processing use-case tests."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from document_processor.domain.errors import PermanentProcessingError, RetryableProcessingError
from document_processor.domain.models import (
    DocumentRecord,
    DocumentStatus,
    PageContent,
    ParserKind,
)
from document_processor.services.processing import ProcessingOutcome, ProcessingService

DOCUMENT_ID = UUID("00000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
PDF_BYTES = b"%PDF-1.7\ncontent"
PAGES = (PageContent(index=0, markdown="Text"),)


@dataclass
class FakeRepository:
    record: DocumentRecord | None
    source: bytes | None = PDF_BYTES
    events: list[str] = field(default_factory=lambda: list[str]())

    async def get(self, document_id: UUID) -> DocumentRecord | None:
        assert document_id == DOCUMENT_ID
        return self.record

    async def get_source(self, document_id: UUID) -> bytes | None:
        assert document_id == DOCUMENT_ID
        return self.source

    async def mark_processing(
        self,
        document_id: UUID,
        *,
        attempt: int,
        updated_at: datetime,
    ) -> bool:
        assert document_id == DOCUMENT_ID
        self.events.append("processing")
        if self.record is None:
            return False
        self.record = replace(
            self.record,
            status=DocumentStatus.PROCESSING,
            attempts=attempt,
            updated_at=updated_at,
        )
        return True

    async def complete(
        self,
        document_id: UUID,
        *,
        pages: Sequence[PageContent],
        summary: str,
        completed_at: datetime,
        attempts: int,
    ) -> None:
        assert document_id == DOCUMENT_ID
        self.events.append("complete")
        assert self.record is not None
        self.record = replace(
            self.record,
            status=DocumentStatus.COMPLETED,
            pages=tuple(pages),
            summary=summary,
            attempts=attempts,
            updated_at=completed_at,
            completed_at=completed_at,
        )
        self.source = None

    async def fail(
        self,
        document_id: UUID,
        *,
        error_code: str,
        error_message: str,
        completed_at: datetime,
        attempts: int,
    ) -> None:
        assert document_id == DOCUMENT_ID
        self.events.append(f"fail:{error_code}:{error_message}")
        assert self.record is not None
        self.record = replace(
            self.record,
            status=DocumentStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
            attempts=attempts,
            updated_at=completed_at,
            completed_at=completed_at,
        )
        self.source = None


@dataclass
class FakeParser:
    result: tuple[PageContent, ...] | Exception = PAGES
    calls: list[bytes] = field(default_factory=lambda: list[bytes]())

    async def parse(self, content: bytes) -> tuple[PageContent, ...]:
        self.calls.append(content)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@dataclass
class FakeSummarizer:
    result: str | Exception = "Summary"
    calls: list[tuple[PageContent, ...]] = field(
        default_factory=lambda: list[tuple[PageContent, ...]]()
    )

    async def summarize(self, pages: Sequence[PageContent]) -> str:
        self.calls.append(tuple(pages))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def record(
    *,
    parser: ParserKind = ParserKind.PYPDF,
    status: DocumentStatus = DocumentStatus.QUEUED,
) -> DocumentRecord:
    return DocumentRecord(
        id=DOCUMENT_ID,
        filename="document.pdf",
        parser=parser,
        status=status,
        created_at=NOW,
        updated_at=NOW,
        attempts=0,
    )


def service(
    repository: FakeRepository,
    parsers: Mapping[ParserKind, FakeParser],
    summarizer: FakeSummarizer,
) -> ProcessingService:
    return ProcessingService(
        repository=repository,
        parsers=parsers,
        summarizer=summarizer,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", list(ParserKind))
async def test_dispatches_selected_parser_and_publishes_one_complete_result(
    selected: ParserKind,
) -> None:
    repository = FakeRepository(record(parser=selected))
    parsers = {kind: FakeParser() for kind in ParserKind}
    summarizer = FakeSummarizer()

    outcome = await service(repository, parsers, summarizer).process(DOCUMENT_ID, attempt=1)

    assert outcome is ProcessingOutcome.COMPLETED
    assert repository.events == ["processing", "complete"]
    assert repository.record is not None
    assert repository.record.status is DocumentStatus.COMPLETED
    assert repository.record.pages == PAGES
    assert repository.record.summary == "Summary"
    assert repository.record.attempts == 1
    assert repository.source is None
    assert parsers[selected].calls == [PDF_BYTES]
    assert all(not parser.calls for kind, parser in parsers.items() if kind is not selected)
    assert summarizer.calls == [PAGES]


@pytest.mark.asyncio
async def test_persists_safe_permanent_parser_failure_and_deletes_source() -> None:
    repository = FakeRepository(record())
    parser = FakeParser(PermanentProcessingError("invalid_pdf", "Safe failure."))
    summarizer = FakeSummarizer()

    outcome = await service(
        repository,
        {ParserKind.PYPDF: parser},
        summarizer,
    ).process(DOCUMENT_ID, attempt=2)

    assert outcome is ProcessingOutcome.FAILED
    assert repository.events == ["processing", "fail:invalid_pdf:Safe failure."]
    assert repository.record is not None
    assert repository.record.status is DocumentStatus.FAILED
    assert repository.record.attempts == 2
    assert repository.source is None
    assert summarizer.calls == []


@pytest.mark.asyncio
async def test_persists_safe_permanent_summary_failure() -> None:
    repository = FakeRepository(record())
    summarizer = FakeSummarizer(PermanentProcessingError("empty_extraction", "Use OCR."))

    outcome = await service(
        repository,
        {ParserKind.PYPDF: FakeParser()},
        summarizer,
    ).process(DOCUMENT_ID, attempt=1)

    assert outcome is ProcessingOutcome.FAILED
    assert repository.events == ["processing", "fail:empty_extraction:Use OCR."]
    assert repository.source is None
    assert summarizer.calls == [PAGES]


@pytest.mark.asyncio
async def test_propagates_retryable_failure_without_publishing_terminal_state() -> None:
    repository = FakeRepository(record())
    parser = FakeParser(RetryableProcessingError("provider_temporarily_unavailable", "Try later."))

    with pytest.raises(RetryableProcessingError):
        await service(
            repository,
            {ParserKind.PYPDF: parser},
            FakeSummarizer(),
        ).process(DOCUMENT_ID, attempt=1)

    assert repository.events == ["processing"]
    assert repository.record is not None
    assert repository.record.status is DocumentStatus.PROCESSING
    assert repository.source == PDF_BYTES


@pytest.mark.asyncio
async def test_skips_terminal_redelivery_without_provider_calls() -> None:
    repository = FakeRepository(record(status=DocumentStatus.COMPLETED), source=None)
    parser = FakeParser()
    summarizer = FakeSummarizer()

    outcome = await service(
        repository,
        {ParserKind.PYPDF: parser},
        summarizer,
    ).process(DOCUMENT_ID, attempt=2)

    assert outcome is ProcessingOutcome.ALREADY_TERMINAL
    assert repository.events == []
    assert parser.calls == []
    assert summarizer.calls == []


@pytest.mark.asyncio
async def test_missing_source_becomes_safe_terminal_failure() -> None:
    repository = FakeRepository(record(), source=None)

    outcome = await service(
        repository,
        {ParserKind.PYPDF: FakeParser()},
        FakeSummarizer(),
    ).process(DOCUMENT_ID, attempt=1)

    assert outcome is ProcessingOutcome.FAILED
    assert repository.events == [
        "processing",
        "fail:source_missing:The uploaded PDF is no longer available.",
    ]
