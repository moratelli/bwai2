"""Public request and response schemas."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from document_processor.domain.models import DocumentRecord, DocumentStatus, ParserKind


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


class ProcessingDocumentResponse(DocumentStateResponse):
    status: Literal[DocumentStatus.PROCESSING]


class PageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    markdown: str


class CompletedDocumentResponse(DocumentStateResponse):
    status: Literal[DocumentStatus.COMPLETED]
    pages: list[PageResponse]
    summary: str
    completed_at: datetime


class FailedDocumentResponse(DocumentStateResponse):
    status: Literal[DocumentStatus.FAILED]
    error: ErrorDetail
    completed_at: datetime


type DocumentResponse = Annotated[
    QueuedDocumentResponse
    | ProcessingDocumentResponse
    | CompletedDocumentResponse
    | FailedDocumentResponse,
    Field(discriminator="status"),
]


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
        return ProcessingDocumentResponse(
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
        error=ErrorDetail(code=record.error_code, message=record.error_message),
        completed_at=record.completed_at,
    )
