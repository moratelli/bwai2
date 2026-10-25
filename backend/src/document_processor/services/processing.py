"""Document processing use case."""

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from document_processor.domain.errors import PermanentProcessingError
from document_processor.domain.models import DocumentStatus, StateTransition
from document_processor.ports import (
    DocumentRepository,
    DocumentSummarizer,
    ParserRegistry,
)


class ProcessingOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    ALREADY_TERMINAL = "already_terminal"
    NOT_FOUND = "not_found"
    LOST_OWNERSHIP = "lost_ownership"


class ProcessingService:
    """Run one idempotent parse-and-summarize attempt."""

    def __init__(
        self,
        *,
        repository: DocumentRepository,
        parsers: ParserRegistry,
        summarizer: DocumentSummarizer,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._parsers = parsers
        self._summarizer = summarizer
        self._clock = clock or (lambda: datetime.now(UTC))

    async def process(
        self,
        document_id: UUID,
        *,
        token: str,
        attempt: int,
    ) -> ProcessingOutcome:
        document = await self._repository.get(document_id)
        if document is None:
            return ProcessingOutcome.NOT_FOUND
        if document.status in {DocumentStatus.COMPLETED, DocumentStatus.FAILED}:
            return ProcessingOutcome.ALREADY_TERMINAL

        started = await self._repository.mark_processing(
            document_id,
            token=token,
            attempt=attempt,
            updated_at=self._clock(),
        )
        if started is not StateTransition.APPLIED:
            return ProcessingOutcome(started.value)

        source = await self._repository.get_source(document_id)
        if source is None:
            result = await self._repository.fail(
                document_id,
                token=token,
                error_code="source_missing",
                error_message="The uploaded PDF is no longer available.",
                completed_at=self._clock(),
                attempts=attempt,
            )
            return self._outcome(result, ProcessingOutcome.FAILED)

        try:
            parser = self._parsers[document.parser]
            pages = await parser.parse(source)
            summary = await self._summarizer.summarize(pages)
        except PermanentProcessingError as error:
            result = await self._repository.fail(
                document_id,
                token=token,
                error_code=error.code,
                error_message=str(error),
                completed_at=self._clock(),
                attempts=attempt,
            )
            return self._outcome(result, ProcessingOutcome.FAILED)

        result = await self._repository.complete(
            document_id,
            token=token,
            pages=pages,
            summary=summary,
            completed_at=self._clock(),
            attempts=attempt,
        )
        return self._outcome(result, ProcessingOutcome.COMPLETED)

    @staticmethod
    def _outcome(result: StateTransition, applied: ProcessingOutcome) -> ProcessingOutcome:
        return applied if result is StateTransition.APPLIED else ProcessingOutcome(result.value)
