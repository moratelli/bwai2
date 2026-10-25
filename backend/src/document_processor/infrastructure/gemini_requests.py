"""One admitted SDK generation with content-free request diagnostics."""

import logging
from collections.abc import Awaitable, Callable
from time import perf_counter

from document_processor.domain.errors import RetryableProcessingError
from document_processor.domain.provider import ProcessingStage
from document_processor.infrastructure.provider_errors import (
    classify_gemini_error,
    provider_http_status,
)
from document_processor.ports import ProviderRequestGate
from document_processor.worker_logging import safe_count

_LOGGER = logging.getLogger(__name__)


async def request_gemini[T](
    operation: Callable[[], Awaitable[T]],
    *,
    stage: ProcessingStage,
    gate: ProviderRequestGate | None,
) -> T:
    # Waiting or Redis failure is not a Gemini request and must not be logged as one.
    if gate is not None:
        await gate.acquire()
    started = perf_counter()
    try:
        response = await operation()
    except Exception as error:
        safe = classify_gemini_error(error, stage)
        info = safe.provider_info if isinstance(safe, RetryableProcessingError) else None
        _LOGGER.warning(
            "Gemini request failed",
            extra={
                "event": "gemini_request_failed",
                "provider": "gemini",
                "provider_stage": stage,
                "provider_category": info.category if info else None,
                "http_status": provider_http_status(error),
                "error_code": safe.code,
                "duration_ms": (perf_counter() - started) * 1000,
            },
        )
        if safe is error:
            raise
        raise safe from error
    usage: object = getattr(response, "usage_metadata", None)
    _LOGGER.info(
        "Gemini request completed",
        extra={
            "event": "gemini_request_completed",
            "provider": "gemini",
            "provider_stage": stage,
            "duration_ms": (perf_counter() - started) * 1000,
            "prompt_token_count": safe_count(getattr(usage, "prompt_token_count", None)),
            "candidates_token_count": safe_count(getattr(usage, "candidates_token_count", None)),
        },
    )
    return response
