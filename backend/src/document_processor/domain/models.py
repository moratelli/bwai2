"""Core document types independent of transport and persistence."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ParserKind(StrEnum):
    PYPDF = "pypdf"
    GEMINI = "gemini"
    MISTRAL = "mistral"


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
