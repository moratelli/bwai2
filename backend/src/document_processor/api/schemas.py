"""Public request and response schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from document_processor.domain.models import DocumentStatus, ParserKind


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
