"""Public request and response schemas."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from document_processor.domain.models import DocumentRecord, DocumentStatus, ParserKind
from document_processor.domain.provider import ProcessingStage, ProviderCategory


class ErrorDetail(BaseModel):
    """Stable, user-safe error information."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail


class AcceptedDocumentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    filename: str
    parser: ParserKind
    status: DocumentStatus
    created_at: datetime


class BatchAcceptedResponse(BaseModel):
    """One accepted asynchronous document per uploaded file."""

    model_config = ConfigDict(extra="forbid")

    documents: list[AcceptedDocumentResponse]


class LivenessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "unhealthy"]
    checks: dict[str, str]


class DocumentStateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    filename: str
    parser: ParserKind
    created_at: datetime
    updated_at: datetime
    attempts: int


class QueuedDocumentResponse(DocumentStateResponse):
    status: Literal[DocumentStatus.QUEUED]


class ProviderCauseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["gemini"]
    category: ProviderCategory
    stage: ProcessingStage
    attempt: int = Field(gt=0, strict=True)
    http_status: int | None = Field(default=None, ge=100, le=599, strict=True)

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        status = self.http_status
        if status is None:
            return self
        valid = {
            "rate_limited": status == 429,
            "unavailable": status >= 500,
            "temporary_failure": status in {408, 409},
            "timeout": status < 500 and status != 429,
            "connection_error": status < 500 and status != 429,
        }[self.category]
        if not valid:
            raise ValueError("Provider category and HTTP status are inconsistent.")
        return self


class RetryWaitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    until: AwareDatetime
    cause: ProviderCauseResponse


class ProcessingDocumentResponse(DocumentStateResponse):
    status: Literal[DocumentStatus.PROCESSING]
    retry_wait: RetryWaitResponse | None = None


class PageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    markdown: str


class CompletedDocumentResponse(DocumentStateResponse):
    status: Literal[DocumentStatus.COMPLETED]
    pages: list[PageResponse]
    summary: str
    completed_at: datetime


class FailedDocumentError(ErrorDetail):
    cause: ProviderCauseResponse | None = None


class FailedDocumentResponse(DocumentStateResponse):
    status: Literal[DocumentStatus.FAILED]
    error: FailedDocumentError
    completed_at: datetime


type DocumentResponse = Annotated[
    QueuedDocumentResponse
    | ProcessingDocumentResponse
    | CompletedDocumentResponse
    | FailedDocumentResponse,
    Field(discriminator="status"),
]


def _provider_cause(record: DocumentRecord) -> ProviderCauseResponse | None:
    failure = record.last_failure
    if failure is None or failure.provider_info is None or failure.attempt > record.attempts:
        return None
    info = failure.provider_info
    try:
        return ProviderCauseResponse(
            provider=info.provider,
            category=info.category,
            stage=info.stage,
            attempt=failure.attempt,
            http_status=info.http_status,
        )
    except ValidationError:
        return None


def document_response(record: DocumentRecord) -> DocumentResponse:
    if record.status is DocumentStatus.QUEUED:
        return QueuedDocumentResponse(
            id=record.id,
            filename=record.filename,
            parser=record.parser,
            status=DocumentStatus.QUEUED,
            created_at=record.created_at,
            updated_at=record.updated_at,
            attempts=record.attempts,
        )
    if record.status is DocumentStatus.PROCESSING:
        cause = _provider_cause(record)
        retry_wait = None
        if (
            cause is not None
            and cause.attempt == record.attempts
            and record.retry_not_before is not None
            and record.retry_not_before.tzinfo is not None
        ):
            retry_wait = RetryWaitResponse(until=record.retry_not_before, cause=cause)
        return ProcessingDocumentResponse(
            retry_wait=retry_wait,
            id=record.id,
            filename=record.filename,
            parser=record.parser,
            status=DocumentStatus.PROCESSING,
            created_at=record.created_at,
            updated_at=record.updated_at,
            attempts=record.attempts,
        )
    if record.status is DocumentStatus.COMPLETED:
        if record.pages is None or record.summary is None or record.completed_at is None:
            raise ValueError("Completed document is missing terminal result fields.")
        return CompletedDocumentResponse(
            id=record.id,
            filename=record.filename,
            parser=record.parser,
            status=DocumentStatus.COMPLETED,
            created_at=record.created_at,
            updated_at=record.updated_at,
            attempts=record.attempts,
            pages=[PageResponse(index=page.index, markdown=page.markdown) for page in record.pages],
            summary=record.summary,
            completed_at=record.completed_at,
        )
    if record.error_code is None or record.error_message is None or record.completed_at is None:
        raise ValueError("Failed document is missing terminal error fields.")
    return FailedDocumentResponse(
        id=record.id,
        filename=record.filename,
        parser=record.parser,
        status=DocumentStatus.FAILED,
        created_at=record.created_at,
        updated_at=record.updated_at,
        attempts=record.attempts,
        error=FailedDocumentError(
            code=record.error_code,
            message=record.error_message,
            cause=_provider_cause(record) if record.error_code == "retry_exhausted" else None,
        ),
        completed_at=record.completed_at,
    )
