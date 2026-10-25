"""Redis Streams document worker orchestration."""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from document_processor.domain.errors import (
    InfrastructureUnavailableError,
    PermanentProcessingError,
    RetryableProcessingError,
)
from document_processor.domain.models import (
    AttemptAdmission,
    AttemptsExhausted,
    RetryWaiting,
    StateTransition,
)
from document_processor.domain.provider import AttemptFailure
from document_processor.infrastructure.redis_streams import StreamMessage
from document_processor.ports import ProviderRequestGate
from document_processor.services.processing import ProcessingOutcome

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _AttemptLogContext:
    attempt: int | None = None


class WorkerQueue(Protocol):
    async def ensure_group(self) -> None: ...

    async def read_new(
        self,
        consumer_name: str,
        *,
        count: int,
        block_ms: int,
    ) -> tuple[StreamMessage, ...]: ...

    async def reclaim_stale(
        self,
        consumer_name: str,
        *,
        minimum_idle_ms: int,
        count: int,
    ) -> tuple[StreamMessage, ...]: ...

    async def acknowledge(self, message_id: str) -> int: ...


class WorkerRepository(Protocol):
    async def record_retry_failure(
        self,
        document_id: UUID,
        *,
        token: str,
        failure: AttemptFailure,
        retry_not_before: datetime | None,
        updated_at: datetime,
    ) -> StateTransition: ...

    async def admit_attempt(
        self,
        document_id: UUID,
        *,
        token: str,
        max_attempts: int,
        updated_at: datetime,
    ) -> AttemptAdmission: ...

    async def acquire_lock(
        self,
        document_id: UUID,
        token: str,
        *,
        ttl_seconds: int,
    ) -> bool: ...

    async def release_lock(self, document_id: UUID, token: str) -> bool: ...

    async def renew_lock(
        self,
        document_id: UUID,
        token: str,
        *,
        ttl_seconds: int,
    ) -> StateTransition: ...

    async def fail(
        self,
        document_id: UUID,
        *,
        token: str,
        error_code: str,
        error_message: str,
        completed_at: datetime,
        attempts: int,
    ) -> StateTransition: ...


class WorkerProcessor(Protocol):
    async def process(
        self,
        document_id: UUID,
        *,
        token: str,
        attempt: int,
    ) -> ProcessingOutcome: ...


class DocumentWorker:
    """Consume Redis Stream messages with lock-protected bounded retries."""

    def __init__(
        self,
        *,
        queue: WorkerQueue,
        repository: WorkerRepository,
        processor: WorkerProcessor,
        consumer_name: str,
        max_attempts: int,
        retry_base_seconds: float,
        retry_jitter_seconds: float,
        reclaim_idle_ms: int,
        read_block_ms: int,
        batch_size: int,
        lock_ttl_seconds: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        renewal_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random: Callable[[], float],
        token_factory: Callable[[], str] = lambda: str(uuid4()),
        clock: Callable[[], datetime] | None = None,
        provider_gate: ProviderRequestGate | None = None,
        retry_max_seconds: float = 60,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._provider_gate = provider_gate
        self._queue = queue
        self._repository = repository
        self._processor = processor
        self._consumer_name = consumer_name
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._retry_jitter_seconds = retry_jitter_seconds
        self._reclaim_idle_ms = reclaim_idle_ms
        self._read_block_ms = read_block_ms
        self._batch_size = batch_size
        self._lock_ttl_seconds = lock_ttl_seconds
        self._sleep = sleep
        self._renewal_sleep = renewal_sleep
        self._renewal_interval = lock_ttl_seconds / 3
        self._random = random
        self._token_factory = token_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run_forever(self) -> None:
        await self._queue.ensure_group()
        while True:
            messages = await self._next_messages()
            await self._handle_all(messages)

    async def run_once(self) -> int:
        await self._queue.ensure_group()
        messages = await self._next_messages()
        await self._handle_all(messages)
        return len(messages)

    async def _next_messages(self) -> tuple[StreamMessage, ...]:
        reclaimed = await self._queue.reclaim_stale(
            self._consumer_name,
            minimum_idle_ms=self._reclaim_idle_ms,
            count=self._batch_size,
        )
        if reclaimed:
            return reclaimed
        return await self._queue.read_new(
            self._consumer_name,
            count=self._batch_size,
            block_ms=self._read_block_ms,
        )

    async def _handle_all(self, messages: Sequence[StreamMessage]) -> None:
        for message in messages:
            await self._handle(message)

    async def _handle(self, message: StreamMessage) -> None:
        token = self._token_factory()
        acquired = await self._repository.acquire_lock(
            message.document_id,
            token,
            ttl_seconds=self._lock_ttl_seconds,
        )
        if not acquired:
            _LOGGER.info(
                "document_lock_unavailable",
                extra={
                    "event": "document_lock_unavailable",
                    "document_id": str(message.document_id),
                    "message_id": message.message_id,
                },
            )
            return
        context = _AttemptLogContext()
        processing = asyncio.create_task(self._process_with_retries(message, token, context))
        renewal = asyncio.create_task(self._renew_lease(message, token, context))
        try:
            done, _ = await asyncio.wait(
                {processing, renewal},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if processing in done:
                await processing
            else:
                await renewal
        finally:
            processing.cancel()
            renewal.cancel()
            await asyncio.gather(processing, renewal, return_exceptions=True)
            try:
                async with asyncio.timeout(min(5.0, self._renewal_interval)):
                    await self._repository.release_lock(message.document_id, token)
            except (InfrastructureUnavailableError, TimeoutError):
                _LOGGER.warning(
                    "document_lease_release_unavailable",
                    extra={
                        "event": "document_lease_release_unavailable",
                        "document_id": str(message.document_id),
                        "message_id": message.message_id,
                        "attempt": context.attempt,
                    },
                )

    async def _renew_lease(
        self,
        message: StreamMessage,
        token: str,
        context: _AttemptLogContext,
    ) -> None:
        while True:
            await self._renewal_sleep(self._renewal_interval)
            try:
                async with asyncio.timeout(self._renewal_interval):
                    result = await self._repository.renew_lock(
                        message.document_id,
                        token,
                        ttl_seconds=self._lock_ttl_seconds,
                    )
            except (InfrastructureUnavailableError, TimeoutError):
                reason = "renewal_unavailable"
            else:
                if result is StateTransition.APPLIED:
                    continue
                reason = result.value
            _LOGGER.warning(
                "document_lease_lost",
                extra={
                    "event": "document_lease_lost",
                    "document_id": str(message.document_id),
                    "message_id": message.message_id,
                    "error_code": reason,
                    "attempt": context.attempt,
                },
            )
            return

    async def _process_with_retries(
        self,
        message: StreamMessage,
        token: str,
        context: _AttemptLogContext,
    ) -> None:
        while True:
            try:
                admission = await self._repository.admit_attempt(
                    message.document_id,
                    token=token,
                    max_attempts=self._max_attempts,
                    updated_at=self._clock(),
                )
            except InfrastructureUnavailableError:
                _LOGGER.warning(
                    "document_admission_unavailable",
                    extra={
                        "event": "document_admission_unavailable",
                        "document_id": str(message.document_id),
                        "message_id": message.message_id,
                    },
                )
                return
            if isinstance(admission, StateTransition):
                if admission in {StateTransition.NOT_FOUND, StateTransition.ALREADY_TERMINAL}:
                    await self._queue.acknowledge(message.message_id)
                return
            if isinstance(admission, RetryWaiting):
                context.attempt = admission.attempts
                await self._sleep(admission.schedule.delay_seconds)
                continue
            if isinstance(admission, AttemptsExhausted):
                context.attempt = admission.attempts
                await self._publish_failure(
                    message,
                    token,
                    admission.attempts,
                    error_code="retry_exhausted",
                    error_message=self._exhaustion_message(
                        admission.attempts,
                        admission.last_failure,
                        recovered=True,
                    ),
                )
                return
            attempt = admission
            context.attempt = attempt
            _LOGGER.info(
                "document_processing_started",
                extra={
                    "event": "document_processing_started",
                    "document_id": str(message.document_id),
                    "message_id": message.message_id,
                    "attempt": attempt,
                },
            )
            try:
                outcome = await self._processor.process(
                    message.document_id,
                    token=token,
                    attempt=attempt,
                )
            except (RetryableProcessingError, InfrastructureUnavailableError) as error:
                delay = self._retry_delay(attempt, error)
                not_before = self._clock() + timedelta(seconds=delay)
                info = error.provider_info if isinstance(error, RetryableProcessingError) else None
                if info is not None and self._provider_gate is not None:
                    try:
                        schedule = await self._provider_gate.cool_down(delay)
                        delay = schedule.delay_seconds
                        not_before = schedule.not_before
                    except InfrastructureUnavailableError:
                        _LOGGER.warning(
                            "document_cooldown_unavailable",
                            extra={
                                "event": "document_cooldown_unavailable",
                                "document_id": str(message.document_id),
                                "message_id": message.message_id,
                                "attempt": attempt,
                                "error_code": "infrastructure_unavailable",
                            },
                        )
                        return
                failure = AttemptFailure(attempt, info)
                try:
                    recorded = await self._repository.record_retry_failure(
                        message.document_id,
                        token=token,
                        failure=failure,
                        retry_not_before=not_before if attempt < self._max_attempts else None,
                        updated_at=self._clock(),
                    )
                except InfrastructureUnavailableError:
                    _LOGGER.warning(
                        "document_retry_state_unavailable",
                        extra={
                            "event": "document_retry_state_unavailable",
                            "document_id": str(message.document_id),
                            "message_id": message.message_id,
                            "attempt": attempt,
                            "error_code": "infrastructure_unavailable",
                        },
                    )
                    return
                if recorded is not StateTransition.APPLIED:
                    if recorded in {StateTransition.NOT_FOUND, StateTransition.ALREADY_TERMINAL}:
                        await self._queue.acknowledge(message.message_id)
                    return
                if attempt >= self._max_attempts:
                    await self._publish_failure(
                        message,
                        token,
                        attempt,
                        error_code="retry_exhausted",
                        error_message=self._exhaustion_message(attempt, failure),
                    )
                    return
                _LOGGER.warning(
                    "document_retry_scheduled",
                    extra={
                        "event": "document_retry_scheduled",
                        "document_id": str(message.document_id),
                        "message_id": message.message_id,
                        "attempt": attempt,
                        "error_code": error.code,
                        "delay_seconds": delay,
                        "provider": "gemini" if info is not None else None,
                        "provider_category": info.category if info is not None else None,
                        "provider_stage": info.stage if info is not None else None,
                        "http_status": info.http_status if info is not None else None,
                    },
                )
                await self._sleep(delay)
            except PermanentProcessingError as error:
                await self._publish_failure(
                    message,
                    token,
                    attempt,
                    error_code=error.code,
                    error_message=str(error),
                )
                return
            else:
                event = {
                    ProcessingOutcome.COMPLETED: "document_processing_completed",
                    ProcessingOutcome.FAILED: "document_processing_failed",
                    ProcessingOutcome.ALREADY_TERMINAL: "document_already_terminal",
                    ProcessingOutcome.NOT_FOUND: "document_not_found",
                    ProcessingOutcome.LOST_OWNERSHIP: "document_ownership_lost",
                }[outcome]
                level = (
                    logging.WARNING
                    if outcome in {ProcessingOutcome.FAILED, ProcessingOutcome.LOST_OWNERSHIP}
                    else logging.INFO
                )
                _LOGGER.log(
                    level,
                    event,
                    extra={
                        "event": event,
                        "document_id": str(message.document_id),
                        "message_id": message.message_id,
                        "attempt": attempt,
                        "outcome": outcome.value,
                    },
                )
                if outcome in {
                    ProcessingOutcome.COMPLETED,
                    ProcessingOutcome.FAILED,
                    ProcessingOutcome.ALREADY_TERMINAL,
                    ProcessingOutcome.NOT_FOUND,
                }:
                    await self._queue.acknowledge(message.message_id)
                return

    def _exhaustion_message(
        self,
        attempt: int,
        failure: AttemptFailure | None,
        *,
        recovered: bool = False,
    ) -> str:
        prefix = (
            f"Processing reached the retry limit ({self._max_attempts} attempts)."
            if recovered
            else f"Processing failed after {self._max_attempts} attempts."
        )
        if failure is None or failure.provider_info is None:
            return prefix
        info = failure.provider_info
        reason = {
            "rate_limited": "Gemini API limit reached",
            "unavailable": "Gemini was temporarily unavailable",
            "timeout": "Gemini timed out",
            "connection_error": "The connection to Gemini failed",
            "temporary_failure": "Gemini had a temporary failure",
        }[info.category]
        detail = f"{reason} during {info.stage}."
        if failure.attempt < attempt:
            detail = f"Last confirmed failure on attempt {failure.attempt}: {detail}"
        return f"{prefix} {detail}"

    def _retry_delay(
        self, attempt: int, error: RetryableProcessingError | InfrastructureUnavailableError
    ) -> float:
        try:
            exponential = math.ldexp(self._retry_base_seconds, attempt - 1)
        except OverflowError:
            exponential = self._retry_max_seconds
        hint = error.retry_after_seconds if isinstance(error, RetryableProcessingError) else None
        return max(min(exponential, self._retry_max_seconds), hint or 0) + (
            self._random() * self._retry_jitter_seconds
        )

    async def _publish_failure(
        self,
        message: StreamMessage,
        token: str,
        attempt: int,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        result = await self._repository.fail(
            message.document_id,
            token=token,
            error_code=error_code,
            error_message=error_message,
            completed_at=self._clock(),
            attempts=attempt,
        )
        event = (
            "document_processing_failed"
            if result is StateTransition.APPLIED
            else "document_publication_rejected"
        )
        level = (
            logging.ERROR
            if result is StateTransition.APPLIED and error_code == "retry_exhausted"
            else logging.WARNING
        )
        _LOGGER.log(
            level,
            event,
            extra={
                "event": event,
                "document_id": str(message.document_id),
                "message_id": message.message_id,
                "attempt": attempt,
                "error_code": error_code,
                "outcome": result.value,
            },
        )
        if result in {
            StateTransition.APPLIED,
            StateTransition.NOT_FOUND,
            StateTransition.ALREADY_TERMINAL,
        }:
            await self._queue.acknowledge(message.message_id)
