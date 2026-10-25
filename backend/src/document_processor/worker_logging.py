"""Allowlisted JSON logs without message, exception, or provider payloads."""

import json
import logging
import math
from datetime import UTC, datetime

_TEXT_FIELDS = ("event", "document_id", "message_id", "outcome", "error_code")


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
        return json.dumps(fields, allow_nan=False)


def configure_worker_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeWorkerFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
