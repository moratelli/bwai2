"""Redis Streams document worker orchestration."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from document_processor.domain.errors import PermanentProcessingError, RetryableProcessingError
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
    async def acquire_lock(
        self,
        document_id: UUID,
        token: str,
        *,
        ttl_seconds: int,
    ) -> bool: ...

    async def release_lock(self, document_id: UUID, token: str) -> bool: ...

    async def fail(
        self,
        document_id: UUID,
        *,
        error_code: str,
        error_message: str,
        completed_at: datetime,
        attempts: int,
    ) -> None: ...


class WorkerProcessor(Protocol):
    async def process(
        self,
        document_id: UUID,
        *,
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
        _LOGGER.info(
            "document_processing_started",
            extra={
                "event": "document_processing_started",
                "document_id": str(message.document_id),
                "message_id": message.message_id,
                "attempt": max(message.delivery_count, 1),
            },
        )
        try:
            await self._process_with_retries(message)
        finally:
            await asyncio.shield(self._repository.release_lock(message.document_id, token))

    async def _process_with_retries(self, message: StreamMessage) -> None:
        attempt = min(max(message.delivery_count, 1), self._max_attempts)
        while True:
            try:
                outcome = await self._processor.process(
                    message.document_id,
                    attempt=attempt,
                )
            except RetryableProcessingError as error:
                if attempt >= self._max_attempts:
                    await self._repository.fail(
                        message.document_id,
                        error_code="retry_exhausted",
                        error_message=(f"Processing failed after {self._max_attempts} attempts."),
                        completed_at=self._clock(),
                        attempts=attempt,
                    )
                    _LOGGER.error(
                        "document_processing_failed",
                        extra={
                            "event": "document_processing_failed",
                            "document_id": str(message.document_id),
                            "message_id": message.message_id,
                            "attempt": attempt,
                            "error_code": "retry_exhausted",
                        },
                    )
                    await self._queue.acknowledge(message.message_id)
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
                attempt += 1
            except PermanentProcessingError as error:
                await self._repository.fail(
                    message.document_id,
                    error_code=error.code,
                    error_message=str(error),
                    completed_at=self._clock(),
                    attempts=attempt,
                )
                _LOGGER.warning(
                    "document_processing_failed",
                    extra={
                        "event": "document_processing_failed",
                        "document_id": str(message.document_id),
                        "message_id": message.message_id,
                        "attempt": attempt,
                        "error_code": error.code,
                    },
                )
                await self._queue.acknowledge(message.message_id)
                return
            else:
                _LOGGER.info(
                    "document_processing_completed",
                    extra={
                        "event": "document_processing_completed",
                        "document_id": str(message.document_id),
                        "message_id": message.message_id,
                        "attempt": attempt,
                        "outcome": outcome.value,
                    },
                )
                await self._queue.acknowledge(message.message_id)
                return
