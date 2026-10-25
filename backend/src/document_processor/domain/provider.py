"""Content-free provider evidence shared by processing boundaries."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

ProviderCategory = Literal[
    "rate_limited", "unavailable", "timeout", "connection_error", "temporary_failure"
]
ProcessingStage = Literal["extraction", "summarization"]


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    category: ProviderCategory
    stage: ProcessingStage
    http_status: int | None = None
    provider: Literal["gemini"] = field(default="gemini", init=False)


@dataclass(frozen=True, slots=True)
class RetrySchedule:
    not_before: datetime
    delay_seconds: float
