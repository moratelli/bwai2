"""Redis Streams document worker orchestration."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from document_processor.domain.errors import (
    InfrastructureUnavailableError,
    PermanentProcessingError,
    RetryableProcessingError,
)
from document_processor.domain.models import AttemptAdmission, AttemptsExhausted, StateTransition
from document_processor.infrastructure.redis_streams import StreamMessage
from document_processor.services.processing import ProcessingOutcome

_LOGGER = logging.getLogger(__name__)


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
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._queue = queue
        self._repository = repository
        self._processor = processor
        self._consumer_name = consumer_name
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
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
        processing = asyncio.create_task(self._process_with_retries(message, token))
        renewal = asyncio.create_task(self._renew_lease(message, token))
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
                    },
                )

    async def _renew_lease(self, message: StreamMessage, token: str) -> None:
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
                },
            )
            return

    async def _process_with_retries(self, message: StreamMessage, token: str) -> None:
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
            if isinstance(admission, AttemptsExhausted):
                await self._publish_failure(
                    message,
                    token,
                    admission.attempts,
                    error_code="retry_exhausted",
                    error_message=(
                        f"Processing reached the retry limit ({self._max_attempts} attempts)."
                    ),
                )
                return
            attempt = admission
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
                if attempt >= self._max_attempts:
                    await self._publish_failure(
                        message,
                        token,
                        attempt,
                        error_code="retry_exhausted",
                        error_message=f"Processing failed after {self._max_attempts} attempts.",
                    )
                    return
                delay = self._retry_base_seconds * (2 ** (attempt - 1))
                delay += self._random() * self._retry_jitter_seconds
                _LOGGER.warning(
                    "document_retry_scheduled",
                    extra={
                        "event": "document_retry_scheduled",
                        "document_id": str(message.document_id),
                        "message_id": message.message_id,
                        "attempt": attempt,
                        "error_code": error.code,
                        "delay_seconds": delay,
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
                event = (
                    "document_ownership_lost"
                    if outcome is ProcessingOutcome.LOST_OWNERSHIP
                    else "document_processing_completed"
                )
                _LOGGER.info(
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
