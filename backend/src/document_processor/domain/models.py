"""Core document types independent of transport and persistence."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ParserKind(StrEnum):
    PYPDF = "pypdf"
    GEMINI = "gemini"


class DocumentStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UploadCandidate:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class NewDocument:
    id: UUID
    filename: str
    parser: ParserKind
    status: DocumentStatus
    content: bytes
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AcceptedDocument:
    id: UUID
    filename: str
    parser: ParserKind
    status: DocumentStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PageContent:
    index: int
    markdown: str


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    id: UUID
    filename: str
    parser: ParserKind
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime
    attempts: int
    pages: tuple[PageContent, ...] | None = None
    summary: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    completed_at: datetime | None = None
