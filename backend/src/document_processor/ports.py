"""Typed interfaces implemented by infrastructure adapters."""

from collections.abc import Sequence
from typing import Protocol

from document_processor.domain.models import NewDocument


class SubmissionGateway(Protocol):
    async def create_batch(self, documents: Sequence[NewDocument]) -> None:
        """Persist and enqueue a validated batch atomically."""
        ...
