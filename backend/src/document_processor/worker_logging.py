"""Allowlisted JSON logs without message, exception, or provider payloads."""

import json
import logging
import math
from datetime import UTC, datetime
from typing import get_args

from document_processor.domain.provider import ProcessingStage, ProviderCategory

_TEXT_FIELDS = ("event", "document_id", "message_id", "outcome", "error_code")


def safe_count(value: object) -> int | None:
    """Accept counts only when supplied as nonnegative integers, never booleans."""
    return value if type(value) is int and value >= 0 else None


class SafeWorkerFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
        }
        for name in _TEXT_FIELDS:
            value: object = getattr(record, name, None)
            fields[name] = value if isinstance(value, str) else None
        attempt: object = getattr(record, "attempt", None)
        fields["attempt"] = attempt if type(attempt) is int else None
        delay: object = getattr(record, "delay_seconds", None)
        fields["delay_seconds"] = (
            delay
            if isinstance(delay, int | float)
            and not isinstance(delay, bool)
            and math.isfinite(delay)
            else None
        )
        for name, allowed in {
            "provider": ("gemini",),
            "provider_category": get_args(ProviderCategory),
            "provider_stage": get_args(ProcessingStage),
        }.items():
            value = getattr(record, name, None)
            fields[name] = value if isinstance(value, str) and value in allowed else None
        for name in ("prompt_token_count", "candidates_token_count"):
            fields[name] = safe_count(getattr(record, name, None))
        status: object = getattr(record, "http_status", None)
        fields["http_status"] = status if type(status) is int and 100 <= status <= 599 else None
        duration: object = getattr(record, "duration_ms", None)
        fields["duration_ms"] = (
            duration
            if isinstance(duration, int | float)
            and not isinstance(duration, bool)
            and math.isfinite(duration)
            and duration >= 0
            else None
        )
        return json.dumps(fields, allow_nan=False)


def configure_worker_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeWorkerFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
