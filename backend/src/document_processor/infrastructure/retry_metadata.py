"""Strict, content-free codecs for optional retry metadata in legacy records."""

import json
from datetime import UTC, datetime
from typing import cast, get_args

from document_processor.domain.provider import (
    AttemptFailure,
    ProcessingStage,
    ProviderCategory,
    ProviderFailure,
)


def encode_failure(failure: AttemptFailure) -> str:
    info = failure.provider_info
    encoded = json.dumps(
        {
            "attempt": failure.attempt,
            "provider_info": None
            if info is None
            else {
                "provider": "gemini",
                "category": info.category,
                "stage": info.stage,
                "http_status": info.http_status,
            },
        },
        separators=(",", ":"),
    )
    if decode_failure(encoded, failure.attempt) is None:
        raise ValueError("Invalid retry evidence.")
    return encoded


def decode_failure(encoded: str | None, attempts: int) -> AttemptFailure | None:
    if encoded is None or len(encoded) > 1024:
        return None
    try:
        raw = cast(object, json.loads(encoded))
    except (ValueError, RecursionError):
        return None
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, object], raw)
    if set(data) != {"attempt", "provider_info"}:
        return None
    attempt = data["attempt"]
    if type(attempt) is not int or not 1 <= attempt <= attempts:
        return None
    if data["provider_info"] is None:
        return AttemptFailure(attempt, None)
    info = decode_provider_failure(data["provider_info"])
    return AttemptFailure(attempt, info) if info is not None else None


def decode_provider_failure(value: object) -> ProviderFailure | None:
    if not isinstance(value, dict):
        return None
    info = cast(dict[str, object], value)
    if set(info) != {"provider", "category", "stage", "http_status"}:
        return None
    category, stage, status = info["category"], info["stage"], info["http_status"]
    if info["provider"] != "gemini" or category not in get_args(ProviderCategory):
        return None
    if stage not in get_args(ProcessingStage):
        return None
    if status is not None:
        if type(status) is not int or not 100 <= status <= 599:
            return None
        if category == "rate_limited" and status != 429:
            return None
        if category == "unavailable" and status < 500:
            return None
        if category == "temporary_failure" and status not in {408, 409}:
            return None
        if category in {"timeout", "connection_error"} and (status == 429 or status >= 500):
            return None
    return ProviderFailure(cast(ProviderCategory, category), cast(ProcessingStage, stage), status)


def decode_deadline(encoded: str | None) -> datetime | None:
    if encoded is None:
        return None
    try:
        value = datetime.fromisoformat(encoded)
    except ValueError:
        return None
    return value.astimezone(UTC) if value.tzinfo is not None else None
