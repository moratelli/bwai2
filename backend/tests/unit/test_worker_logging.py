"""Tests of the formatter configured by the actual worker entrypoint."""

import io
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from typing import cast

import pytest

import document_processor.worker_main as worker_main


@pytest.fixture
def worker_formatter(monkeypatch: pytest.MonkeyPatch) -> logging.Formatter:
    configuration: dict[str, object] = {}

    def configure(**options: object) -> None:
        configuration.update(options)

    async def no_workers() -> None:
        return None

    monkeypatch.setattr(logging, "basicConfig", configure)
    monkeypatch.setattr(worker_main, "run_worker_pool", no_workers)
    worker_main.main()
    assert configuration["level"] == logging.INFO
    old_format = configuration.get("format")
    if isinstance(old_format, str):
        return logging.Formatter(old_format)
    handlers = cast(Sequence[logging.Handler], configuration["handlers"])
    assert len(handlers) == 1
    formatter = handlers[0].formatter
    assert formatter is not None
    return formatter


def emit(
    formatter: logging.Formatter, record: logging.LogRecord
) -> tuple[str, Mapping[str, object]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    handler.handle(record)
    output = stream.getvalue()
    payload: object = json.loads(output)
    assert isinstance(payload, dict)
    return output, cast(Mapping[str, object], payload)


@pytest.mark.parametrize(
    ("event", "extra"),
    [
        ("document_processing_started", {}),
        ("document_retry_scheduled", {"error_code": "provider_temporary", "delay_seconds": 1.25}),
        ("document_lease_lost", {"error_code": "lost_ownership"}),
        ("document_processing_failed", {"error_code": "invalid_pdf", "outcome": "failed"}),
    ],
)
def test_emits_only_safe_correlation_fields(
    worker_formatter: logging.Formatter,
    event: str,
    extra: dict[str, object],
) -> None:
    record = logging.makeLogRecord(
        {
            "name": "document_processor.worker",
            "levelname": "INFO",
            "levelno": logging.INFO,
            "msg": "PROVIDER_MESSAGE_CANARY",
            "event": event,
            "document_id": "00000000-0000-4000-8000-000000000001",
            "message_id": "1-0",
            "attempt": 2,
            "lease_token": "LEASE_CANARY",
            "api_key": "KEY_CANARY",
            "filename": "PRIVATE_FILENAME_CANARY",
            "source": b"SOURCE_CANARY",
            "provider_body": "PROVIDER_BODY_CANARY",
            "stack_info": "STACK_CANARY",
            **extra,
        }
    )
    try:
        raise ValueError("EXCEPTION_CANARY")
    except ValueError:
        record.exc_info = sys.exc_info()
    output, payload = emit(worker_formatter, record)

    assert payload["event"] == event
    assert payload["document_id"] == "00000000-0000-4000-8000-000000000001"
    assert payload["message_id"] == "1-0"
    assert payload["attempt"] == 2
    assert payload["error_code"] == extra.get("error_code")
    assert payload["outcome"] == extra.get("outcome")
    assert payload["delay_seconds"] == extra.get("delay_seconds")
    assert "CANARY" not in output
    assert set(payload) == {
        "timestamp",
        "level",
        "logger",
        "event",
        "document_id",
        "message_id",
        "attempt",
        "outcome",
        "error_code",
        "delay_seconds",
        "provider",
        "provider_category",
        "provider_stage",
        "http_status",
        "duration_ms",
        "prompt_token_count",
        "candidates_token_count",
    }


def test_context_free_library_record_uses_safe_defaults(
    worker_formatter: logging.Formatter,
) -> None:
    output, payload = emit(
        worker_formatter,
        logging.makeLogRecord(
            {
                "name": "some.library",
                "levelname": "WARNING",
                "levelno": logging.WARNING,
                "msg": "LIBRARY_PAYLOAD_CANARY",
            }
        ),
    )

    assert payload["logger"] == "some.library"
    assert payload["level"] == "WARNING"
    assert payload["event"] is None
    assert payload["document_id"] is None
    assert payload["message_id"] is None
    assert payload["attempt"] is None
    assert "CANARY" not in output


def test_untrusted_provider_metadata_is_not_serialized(worker_formatter: logging.Formatter) -> None:
    output, payload = emit(
        worker_formatter,
        logging.makeLogRecord(
            {
                "provider": "PRIVATE_CANARY",
                "provider_category": "PRIVATE_CANARY",
                "provider_stage": "PRIVATE_CANARY",
                "http_status": True,
                "duration_ms": float("nan"),
                "prompt_token_count": -1,
                "candidates_token_count": "CANARY",
            }
        ),
    )
    for name in (
        "provider",
        "provider_category",
        "provider_stage",
        "http_status",
        "duration_ms",
        "prompt_token_count",
        "candidates_token_count",
    ):
        assert payload[name] is None
    assert "CANARY" not in output


def test_invalid_context_values_are_not_serialized(worker_formatter: logging.Formatter) -> None:
    output, payload = emit(
        worker_formatter,
        logging.makeLogRecord(
            {
                "name": "some.library",
                "levelname": "INFO",
                "levelno": logging.INFO,
                "msg": "ignored",
                "event": {"key": "KEY_CANARY"},
                "document_id": b"SOURCE_CANARY",
                "attempt": "not-a-number",
                "delay_seconds": float("inf"),
                "error_code": ValueError("EXCEPTION_CANARY"),
            }
        ),
    )

    assert "CANARY" not in output
    assert all(
        payload[key] is None
        for key in (
            "event",
            "document_id",
            "attempt",
            "delay_seconds",
            "error_code",
        )
    )
