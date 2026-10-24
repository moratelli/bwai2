"""Document submission validation and job creation."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import UUID, uuid4

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
    AcceptedDocument,
    DocumentStatus,
    NewDocument,
    ParserKind,
    UploadCandidate,
)
from document_processor.ports import SubmissionGateway


class SubmissionService:
    """Validate complete batches before publishing any document job."""

    def __init__(
        self,
        *,
        gateway: SubmissionGateway,
        settings: Settings,
        id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._gateway = gateway
        self._settings = settings
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def submit(
        self,
        candidates: Sequence[UploadCandidate],
        parser: ParserKind,
    ) -> tuple[AcceptedDocument, ...]:
        self._validate_availability(parser)
        self._validate_batch(candidates)

        created_at = self._clock()
        documents = tuple(
            NewDocument(
                id=self._id_factory(),
                filename=self._safe_filename(candidate.filename),
                parser=parser,
                status=DocumentStatus.QUEUED,
                content=candidate.content,
                created_at=created_at,
            )
            for candidate in candidates
        )
        await self._gateway.create_batch(documents)

        return tuple(
            AcceptedDocument(
                id=document.id,
                filename=document.filename,
                parser=document.parser,
                status=document.status,
                created_at=document.created_at,
            )
            for document in documents
        )

    def _validate_availability(self, parser: ParserKind) -> None:
        if not self._settings.summary_available:
            raise ProviderUnavailableError("Gemini")
        if parser is ParserKind.GEMINI and not self._settings.gemini_available:
            raise ProviderUnavailableError("Gemini")

    def _validate_batch(self, candidates: Sequence[UploadCandidate]) -> None:
        if not candidates:
            raise EmptyBatchError
        if len(candidates) > self._settings.max_files_per_batch:
            raise TooManyFilesError(self._settings.max_files_per_batch)

        for candidate in candidates:
            filename = self._safe_filename(candidate.filename)
            if candidate.content_type != "application/pdf":
                raise InvalidMediaTypeError(filename)
            if len(candidate.content) > self._settings.max_file_size_bytes:
                raise FileTooLargeError(filename, self._settings.max_file_size_bytes)
            if not candidate.content.startswith(b"%PDF-"):
                raise InvalidPdfError(filename)

    @staticmethod
    def _safe_filename(filename: str) -> str:
        normalized = filename.replace("\\", "/")
        safe_name = PurePosixPath(normalized).name.strip()
        return safe_name or "document.pdf"
