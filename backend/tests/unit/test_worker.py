"""Redis Streams worker behavior tests."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from document_processor.domain.errors import PermanentProcessingError, RetryableProcessingError
from document_processor.infrastructure.redis_streams import StreamMessage
from document_processor.services.processing import ProcessingOutcome
from document_processor.worker import DocumentWorker

DOCUMENT_ID = UUID("00000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
MESSAGE = StreamMessage(message_id="1-0", document_id=DOCUMENT_ID, delivery_count=1)
LOCK_OWNER = "lock-owner"


@dataclass
class FakeQueue:
    new: tuple[StreamMessage, ...] = ()
    stale: tuple[StreamMessage, ...] = ()
    events: list[str] = field(default_factory=lambda: list[str]())
    reads: int = 0

    async def ensure_group(self) -> None:
        self.events.append("group")

    async def read_new(
        self,
        consumer_name: str,
        *,
        count: int,
        block_ms: int,
    ) -> tuple[StreamMessage, ...]:
        assert consumer_name == "worker-one"
        assert count == 10
        assert block_ms == 50
        self.reads += 1
        return self.new

    async def reclaim_stale(
        self,
        consumer_name: str,
        *,
        minimum_idle_ms: int,
        count: int,
    ) -> tuple[StreamMessage, ...]:
        assert consumer_name == "worker-one"
        assert minimum_idle_ms == 30_000
        assert count == 10
        messages, self.stale = self.stale, ()
        return messages

    async def acknowledge(self, message_id: str) -> int:
        self.events.append(f"ack:{message_id}")
        return 1


@dataclass
class FakeRepository:
    lock_available: bool = True
    events: list[str] = field(default_factory=lambda: list[str]())

    async def acquire_lock(self, document_id: UUID, token: str, *, ttl_seconds: int) -> bool:
        assert document_id == DOCUMENT_ID
        assert token == LOCK_OWNER
        assert ttl_seconds == 60
        self.events.append("lock")
        return self.lock_available

    async def release_lock(self, document_id: UUID, token: str) -> bool:
        assert document_id == DOCUMENT_ID
        assert token == LOCK_OWNER
        self.events.append("release")
        return True

    async def fail(
        self,
        document_id: UUID,
        *,
        error_code: str,
        error_message: str,
        completed_at: datetime,
        attempts: int,
    ) -> None:
        assert document_id == DOCUMENT_ID
        assert completed_at == NOW
        self.events.append(f"fail:{error_code}:{error_message}:{attempts}")


@dataclass
class FakeProcessor:
    results: list[ProcessingOutcome | BaseException]
    events: list[str] = field(default_factory=lambda: list[str]())
    attempts: list[int] = field(default_factory=lambda: list[int]())

    async def process(self, document_id: UUID, *, attempt: int) -> ProcessingOutcome:
        assert document_id == DOCUMENT_ID
        self.attempts.append(attempt)
        self.events.append(f"process:{attempt}")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def worker(
    queue: FakeQueue,
    repository: FakeRepository,
    processor: FakeProcessor,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> DocumentWorker:
    return DocumentWorker(
        queue=queue,
        repository=repository,
        processor=processor,
        consumer_name="worker-one",
        max_attempts=3,
        retry_base_seconds=1,
        retry_jitter_seconds=0,
        reclaim_idle_ms=30_000,
        read_block_ms=50,
        batch_size=10,
        lock_ttl_seconds=60,
        sleep=sleep,
        random=lambda: 0,
        token_factory=lambda: LOCK_OWNER,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_acknowledges_only_after_successful_terminal_processing() -> None:
    queue = FakeQueue(new=(MESSAGE,))
    repository = FakeRepository()
    processor = FakeProcessor([ProcessingOutcome.COMPLETED])

    processed = await worker(queue, repository, processor).run_once()

    assert processed == 1
    assert processor.events == ["process:1"]
    assert queue.events == ["group", "ack:1-0"]
    assert repository.events == ["lock", "release"]


@pytest.mark.asyncio
async def test_retries_with_exponential_backoff_then_acknowledges_success() -> None:
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    queue = FakeQueue(new=(MESSAGE,))
    repository = FakeRepository()
    processor = FakeProcessor(
        [
            RetryableProcessingError("temporary", "Try later."),
            RetryableProcessingError("temporary", "Try later."),
            ProcessingOutcome.COMPLETED,
        ]
    )

    await worker(queue, repository, processor, sleep=record_sleep).run_once()

    assert processor.attempts == [1, 2, 3]
    assert delays == [1, 2]
    assert queue.events[-1] == "ack:1-0"
    assert not any(event.startswith("fail:") for event in repository.events)


@pytest.mark.asyncio
async def test_persists_terminal_failure_before_ack_after_three_attempts() -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    queue = FakeQueue(new=(MESSAGE,))
    repository = FakeRepository()
    processor = FakeProcessor(
        [RetryableProcessingError("temporary", "Try later.") for _ in range(3)]
    )

    await worker(queue, repository, processor, sleep=no_sleep).run_once()

    assert processor.attempts == [1, 2, 3]
    assert repository.events == [
        "lock",
        "fail:retry_exhausted:Processing failed after 3 attempts.:3",
        "release",
    ]
    assert queue.events == ["group", "ack:1-0"]


@pytest.mark.asyncio
async def test_permanent_failure_is_persisted_without_retry_before_ack() -> None:
    queue = FakeQueue(new=(MESSAGE,))
    repository = FakeRepository()
    processor = FakeProcessor([PermanentProcessingError("invalid_pdf", "Safe failure.")])

    await worker(queue, repository, processor).run_once()

    assert processor.attempts == [1]
    assert repository.events == [
        "lock",
        "fail:invalid_pdf:Safe failure.:1",
        "release",
    ]
    assert queue.events[-1] == "ack:1-0"


@pytest.mark.asyncio
async def test_reclaims_stale_pending_work_before_reading_new_messages() -> None:
    queue = FakeQueue(new=(MESSAGE,), stale=(MESSAGE,))
    processor = FakeProcessor([ProcessingOutcome.COMPLETED])

    processed = await worker(queue, FakeRepository(), processor).run_once()

    assert processed == 1
    assert queue.reads == 0
    assert queue.events[-1] == "ack:1-0"


@pytest.mark.asyncio
async def test_leaves_message_pending_when_lock_is_owned_elsewhere() -> None:
    queue = FakeQueue(new=(MESSAGE,))
    repository = FakeRepository(lock_available=False)
    processor = FakeProcessor([ProcessingOutcome.COMPLETED])

    await worker(queue, repository, processor).run_once()

    assert processor.attempts == []
    assert queue.events == ["group"]
    assert repository.events == ["lock"]


@pytest.mark.asyncio
async def test_terminal_redelivery_is_acknowledged_without_retry() -> None:
    queue = FakeQueue(stale=(MESSAGE,))
    processor = FakeProcessor([ProcessingOutcome.ALREADY_TERMINAL])

    await worker(queue, FakeRepository(), processor).run_once()

    assert processor.attempts == [1]
    assert queue.events[-1] == "ack:1-0"


@pytest.mark.asyncio
async def test_emits_structured_content_safe_lifecycle_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    sensitive = "credential=super-secret raw-pdf-text"
    queue = FakeQueue(new=(MESSAGE,))
    processor = FakeProcessor(
        [
            RetryableProcessingError("provider_temporary", sensitive),
            ProcessingOutcome.COMPLETED,
        ]
    )

    with caplog.at_level(logging.INFO, logger="document_processor.worker"):
        await worker(queue, FakeRepository(), processor, sleep=no_sleep).run_once()

    worker_records = [
        record for record in caplog.records if record.name == "document_processor.worker"
    ]
    record_data = [cast(Mapping[str, object], vars(record)) for record in worker_records]
    assert [data.get("event") for data in record_data] == [
        "document_processing_started",
        "document_retry_scheduled",
        "document_processing_completed",
    ]
    assert all(data.get("document_id") == str(DOCUMENT_ID) for data in record_data)
    assert record_data[1].get("attempt") == 1
    assert record_data[1].get("error_code") == "provider_temporary"
    rendered = " ".join(
        record.getMessage() + repr(data)
        for record, data in zip(worker_records, record_data, strict=True)
    )
    assert sensitive not in rendered
    assert "super-secret" not in rendered
    assert "raw-pdf-text" not in rendered


@pytest.mark.asyncio
async def test_cancellation_releases_lock_and_leaves_message_pending() -> None:
    queue = FakeQueue(new=(MESSAGE,))
    repository = FakeRepository()
    processor = FakeProcessor([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await worker(queue, repository, processor).run_once()

    assert repository.events == ["lock", "release"]
    assert queue.events == ["group"]
