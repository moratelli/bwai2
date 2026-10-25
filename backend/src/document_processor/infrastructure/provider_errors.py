"""Translate SDK failures without retaining provider text or headers."""

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import cast

import httpx

from document_processor.domain.errors import (
    DocumentProcessorError,
    PermanentProcessingError,
    RetryableProcessingError,
)
from document_processor.domain.provider import ProcessingStage, ProviderCategory, ProviderFailure

_RETRYABLE_STATUS_CODES = {408, 409, 429}
MAX_RETRY_HINT_SECONDS = 86_400


def _http_status(error: Exception) -> int | None:
    for attribute in ("code", "status_code"):
        status = getattr(error, attribute, None)
        if type(status) is int and 100 <= status <= 599:
            return status
    return None


def _bounded_delay(value: float) -> float | None:
    return value if math.isfinite(value) and 0 <= value <= MAX_RETRY_HINT_SECONDS else None


def _header_delay(value: object, now: datetime) -> float | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        return _bounded_delay(float(value))
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                return None
            return _bounded_delay((deadline - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _retry_delay(error: Exception, now: datetime) -> float | None:
    delays: list[float] = []
    headers = _mapping(getattr(getattr(error, "response", None), "headers", None))
    delay = _header_delay(headers.get("retry-after", headers.get("Retry-After")), now)
    if delay is not None:
        delays.append(delay)
    payload = _mapping(getattr(error, "details", None))
    details = _mapping(payload.get("error", payload)).get("details")
    if isinstance(details, list):
        for item in cast(list[object], details):
            detail = _mapping(item)
            if detail.get("@type") != "type.googleapis.com/google.rpc.RetryInfo":
                continue
            value = detail.get("retryDelay")
            if not isinstance(value, str) or not value.endswith("s") or len(value) > 128:
                continue
            try:
                delay = _bounded_delay(float(value[:-1]))
            except ValueError:
                continue
            if delay is not None:
                delays.append(delay)
    return max(delays) if delays else None


def classify_gemini_error(
    error: Exception, stage: ProcessingStage, *, now: datetime | None = None
) -> DocumentProcessorError:
    """Only typed SDK status/transport evidence can identify a retryable Gemini failure.

    Supported delay hints are finite and nonnegative, at most one day. Invalid
    metadata falls back to worker backoff. Neither exception text nor quota
    dimensions are inspected, persisted, or copied into the safe exception.
    """
    if isinstance(error, DocumentProcessorError):
        return error
    status = _http_status(error)
    category: ProviderCategory | None = None
    if status == 429:
        category = "rate_limited"
    elif status is not None and status >= 500:
        category = "unavailable"
    elif isinstance(error, (TimeoutError, httpx.TimeoutException)):
        category = "timeout"
    elif isinstance(error, (ConnectionError, httpx.TransportError)):
        category = "connection_error"
    elif status in _RETRYABLE_STATUS_CODES:
        category = "temporary_failure"
    if category is not None:
        return RetryableProcessingError(
            "gemini_temporarily_unavailable",
            "Gemini is temporarily unavailable.",
            provider_info=ProviderFailure(category, stage, status),
            retry_after_seconds=_retry_delay(error, now or datetime.now(UTC)),
        )
    return PermanentProcessingError(
        "gemini_rejected_document",
        "Gemini could not process this PDF."
        if stage == "extraction"
        else "Gemini could not summarize this document.",
    )
