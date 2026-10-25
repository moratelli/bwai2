"""Typed interfaces implemented by infrastructure adapters."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from document_processor.domain.models import (
    DocumentRecord,
    NewDocument,
    PageContent,
    ParserKind,
    StateTransition,
)
from document_processor.domain.provider import RetrySchedule


class ProviderRequestGate(Protocol):
    async def acquire(self) -> None:
        """Wait for a permit; uncertainty never grants admission."""
        ...

    async def cool_down(self, seconds: float) -> RetrySchedule:
        """Extend the shared deadline without shortening another worker's wait."""
        ...


class SubmissionGateway(Protocol):
    async def create_batch(self, documents: Sequence[NewDocument]) -> None:
        """Persist and enqueue a validated batch atomically."""
        ...


class HealthProbe(Protocol):
    async def ping(self) -> bool: ...


class DocumentReader(Protocol):
    async def get(self, document_id: UUID) -> DocumentRecord | None: ...


class DocumentRepository(DocumentReader, Protocol):
    async def get(self, document_id: UUID) -> DocumentRecord | None: ...

    async def get_source(self, document_id: UUID) -> bytes | None: ...

    async def complete(
        self,
        document_id: UUID,
        *,
        token: str,
        pages: Sequence[PageContent],
        summary: str,
        completed_at: datetime,
        attempts: int,
    ) -> StateTransition: ...

    async def fail(
        self,
        document_id: UUID,
        *,
        token: str,
        error_code: str,
        error_message: str,
        completed_at: datetime,
        attempts: int,
    ) -> StateTransition: ...


class DocumentParser(Protocol):
    async def parse(self, content: bytes) -> tuple[PageContent, ...]: ...


class DocumentSummarizer(Protocol):
    async def summarize(self, pages: Sequence[PageContent]) -> str: ...


type ParserRegistry = Mapping[ParserKind, DocumentParser]
