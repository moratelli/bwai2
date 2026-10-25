"""Redis Streams worker behavior tests."""

import asyncio
import io
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from document_processor.domain.errors import (
    InfrastructureUnavailableError,
    PermanentProcessingError,
    RetryableProcessingError,
)
from document_processor.domain.models import AttemptAdmission, AttemptsExhausted, StateTransition
from document_processor.domain.provider import AttemptFailure
from document_processor.infrastructure.redis_streams import StreamMessage
from document_processor.services.processing import ProcessingOutcome
from document_processor.worker import DocumentWorker
from document_processor.worker_logging import SafeWorkerFormatter

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
    release_error: Exception | None = None
    attempts: int = 0
    admission_error: Exception | None = None
    lease_seconds: int = 60
    renewal_result: StateTransition | Exception = StateTransition.APPLIED
    renewal_gate: asyncio.Event | None = None
    renewal_seen: asyncio.Event = field(default_factory=asyncio.Event)
    renewals: int = 0
    release_gate: asyncio.Event | None = None
    release_finished: asyncio.Event = field(default_factory=asyncio.Event)
    last_failure: AttemptFailure | None = None
    retry_deadlines: list[datetime | None] = field(default_factory=list[datetime | None])

    async def record_retry_failure(
        self,
        document_id: UUID,
        *,
        token: str,
        failure: AttemptFailure,
        retry_not_before: datetime | None,
        updated_at: datetime,
    ) -> StateTransition:
        assert document_id == DOCUMENT_ID and token == LOCK_OWNER and updated_at == NOW
        assert failure.attempt == self.attempts
        self.last_failure = failure
        self.retry_deadlines.append(retry_not_before)
        return StateTransition.APPLIED

    async def admit_attempt(
        self,
        document_id: UUID,
        *,
        token: str,
        max_attempts: int,
        updated_at: datetime,
    ) -> AttemptAdmission:
        assert document_id == DOCUMENT_ID and token == LOCK_OWNER
        assert updated_at == NOW
        if self.admission_error is not None:
            raise self.admission_error
        if self.attempts >= max_attempts:
            return AttemptsExhausted(self.attempts, self.last_failure)
        self.attempts += 1
        return self.attempts

    async def acquire_lock(self, document_id: UUID, token: str, *, ttl_seconds: int) -> bool:
        assert document_id == DOCUMENT_ID
        assert token == LOCK_OWNER
        assert ttl_seconds == self.lease_seconds
        self.events.append("lock")
        return self.lock_available

    async def release_lock(self, document_id: UUID, token: str) -> bool:
        assert document_id == DOCUMENT_ID
        assert token == LOCK_OWNER
        self.events.append("release")
        try:
            if self.release_gate is not None:
                await self.release_gate.wait()
            if self.release_error is not None:
                raise self.release_error
            return True
        finally:
            self.release_finished.set()

    async def renew_lock(
        self,
        document_id: UUID,
        token: str,
        *,
        ttl_seconds: int,
    ) -> StateTransition:
        assert document_id == DOCUMENT_ID
        assert token == LOCK_OWNER
        assert ttl_seconds == self.lease_seconds
        self.events.append("renew")
        self.renewals += 1
        self.renewal_seen.set()
        if self.renewal_gate is not None:
            await self.renewal_gate.wait()
        if isinstance(self.renewal_result, Exception):
            raise self.renewal_result
        return self.renewal_result

    async def fail(
        self,
        document_id: UUID,
        *,
        token: str,
        error_code: str,
        error_message: str,
        completed_at: datetime,
        attempts: int,
    ) -> StateTransition:
        assert token == LOCK_OWNER
        assert document_id == DOCUMENT_ID
        assert completed_at == NOW
        self.events.append(f"fail:{error_code}:{error_message}:{attempts}")
        return StateTransition.APPLIED


@dataclass
class FakeProcessor:
    results: list[ProcessingOutcome | BaseException]
    events: list[str] = field(default_factory=lambda: list[str]())
    attempts: list[int] = field(default_factory=lambda: list[int]())
    gate: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    async def process(
        self,
        document_id: UUID,
        *,
        token: str,
        attempt: int,
    ) -> ProcessingOutcome:
        assert token == LOCK_OWNER
        assert document_id == DOCUMENT_ID
        self.attempts.append(attempt)
        self.events.append(f"process:{attempt}")
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        finally:
            self.finished.set()


def worker(
    queue: FakeQueue,
    repository: FakeRepository,
    processor: FakeProcessor,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    renewal_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
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
        lock_ttl_seconds=repository.lease_seconds,
        sleep=sleep,
        renewal_sleep=renewal_sleep,
        random=lambda: 0,
        token_factory=lambda: LOCK_OWNER,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_redeliveries_and_lock_contention_do_not_consume_attempts() -> None:
    async def no_sleep(_delay: float) -> None:
        pass

    queue = FakeQueue(new=(StreamMessage("1-0", DOCUMENT_ID, 99),))
    repository = FakeRepository(lock_available=False)
    processor = FakeProcessor(
        [
            RetryableProcessingError("temporary", "Try later."),
            ProcessingOutcome.COMPLETED,
        ]
    )
    instance = worker(queue, repository, processor, sleep=no_sleep)
    await instance.run_once()
    await instance.run_once()
    assert repository.attempts == 0
    assert processor.attempts == []
    repository.lock_available = True

    await instance.run_once()

    assert processor.attempts == [1, 2]
    assert repository.attempts == 2
    assert queue.events[-1] == "ack:1-0"


@pytest.mark.asyncio
async def test_recovery_continues_persisted_attempts_not_delivery_count() -> None:
    queue, repository = FakeQueue(new=(MESSAGE,)), FakeRepository(attempts=2)
    processor = FakeProcessor([ProcessingOutcome.COMPLETED])

    await worker(queue, repository, processor).run_once()

    assert processor.attempts == [3]
    assert repository.attempts == 3


@pytest.mark.asyncio
async def test_exhausted_recovery_does_not_start_a_fourth_attempt() -> None:
    queue, repository = FakeQueue(new=(MESSAGE,)), FakeRepository(attempts=3)
    processor = FakeProcessor([])

    await worker(queue, repository, processor).run_once()

    assert processor.attempts == []
    assert repository.attempts == 3
    assert any(event.startswith("fail:retry_exhausted:") for event in repository.events)
    assert queue.events[-1] == "ack:1-0"


@pytest.mark.asyncio
async def test_admission_outage_does_not_consume_budget_or_publish_failure() -> None:
    queue = FakeQueue(new=(MESSAGE,))
    repository = FakeRepository(admission_error=InfrastructureUnavailableError("Document store"))
    processor = FakeProcessor([ProcessingOutcome.COMPLETED])

    await worker(queue, repository, processor).run_once()

    assert processor.attempts == []
    assert repository.attempts == 0
    assert queue.events == ["group"]
    assert not any(event.startswith("fail:") for event in repository.events)


@pytest.mark.asyncio
async def test_cancelled_admitted_attempt_is_preserved_for_replacement() -> None:
    repository = FakeRepository()
    with pytest.raises(asyncio.CancelledError):
        await worker(
            FakeQueue(new=(MESSAGE,)),
            repository,
            FakeProcessor(
                [
                    asyncio.CancelledError(),
                ]
            ),
        ).run_once()
    assert repository.attempts == 1
    processor = FakeProcessor([ProcessingOutcome.COMPLETED])

    await worker(FakeQueue(new=(MESSAGE,)), repository, processor).run_once()

    assert processor.attempts == [2]
    assert repository.attempts == 2


class LeasePulse:
    def __init__(self) -> None:
        self.ticks: asyncio.Queue[None] = asyncio.Queue()
        self.waiting = asyncio.Event()
        self.cancelled = False
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.waiting.set()
        try:
            await self.ticks.get()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.waiting.clear()


@asynccontextmanager
async def running_worker(instance: DocumentWorker) -> AsyncGenerator[asyncio.Task[int]]:
    task = asyncio.create_task(instance.run_once())
    try:
        async with asyncio.timeout(3):
            yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_lease_renews_while_processing_and_stops_after_completion() -> None:
    queue, repository = FakeQueue(new=(MESSAGE,)), FakeRepository()
    gate, pulse = asyncio.Event(), LeasePulse()
    processor = FakeProcessor([ProcessingOutcome.COMPLETED], gate=gate)
    before = asyncio.all_tasks()
    async with running_worker(worker(queue, repository, processor, renewal_sleep=pulse)) as task:
        await processor.started.wait()
        for expected in (1, 2):
            repository.renewal_seen.clear()
            pulse.ticks.put_nowait(None)
            await repository.renewal_seen.wait()
            assert repository.renewals == expected
            assert not task.done()
        gate.set()
        await task
    assert pulse.delays and all(delay == 20 for delay in pulse.delays)
    assert pulse.cancelled
    assert queue.events == ["group", "ack:1-0"]
    assert asyncio.all_tasks() <= before


@pytest.mark.asyncio
async def test_lease_renews_during_retry_backoff() -> None:
    queue, repository = FakeQueue(new=(MESSAGE,)), FakeRepository()
    backoff, pulse = LeasePulse(), LeasePulse()
    processor = FakeProcessor(
        [
            RetryableProcessingError("temporary", "Safe retry."),
            ProcessingOutcome.COMPLETED,
        ]
    )
    async with running_worker(
        worker(
            queue,
            repository,
            processor,
            sleep=backoff,
            renewal_sleep=pulse,
        )
    ) as task:
        await backoff.waiting.wait()
        pulse.ticks.put_nowait(None)
        await repository.renewal_seen.wait()
        assert processor.attempts == [1]
        backoff.ticks.put_nowait(None)
        await task
    assert repository.renewals == 1
    assert processor.attempts == [1, 2]
    assert queue.events == ["group", "ack:1-0"]
    assert pulse.cancelled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rejection",
    [
        StateTransition.LOST_OWNERSHIP,
        StateTransition.NOT_FOUND,
        StateTransition.ALREADY_TERMINAL,
        InfrastructureUnavailableError("Document store"),
        TimeoutError(),
    ],
)
async def test_lease_rejection_stops_work_without_acknowledgement(
    rejection: StateTransition | Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue, pulse = FakeQueue(new=(MESSAGE,)), LeasePulse()
    repository = FakeRepository(renewal_result=rejection)
    processor = FakeProcessor([ProcessingOutcome.COMPLETED], gate=asyncio.Event())
    before = asyncio.all_tasks()
    async with running_worker(worker(queue, repository, processor, renewal_sleep=pulse)) as task:
        await processor.started.wait()
        pulse.ticks.put_nowait(None)
        await task
    assert processor.finished.is_set()
    assert processor.results == [ProcessingOutcome.COMPLETED]
    lease_records = [
        entry for entry in emitted_logs(caplog.records) if entry["event"] == "document_lease_lost"
    ]
    assert len(lease_records) == 1
    assert lease_records[0]["attempt"] == 1
    assert repository.events[-1] == "release"
    assert not any(event.startswith("fail:") for event in repository.events)
    assert queue.events == ["group"]
    assert asyncio.all_tasks() <= before


@pytest.mark.asyncio
async def test_lease_renewal_has_a_bounded_operation_timeout() -> None:
    queue, pulse = FakeQueue(new=(MESSAGE,)), LeasePulse()
    repository = FakeRepository(lease_seconds=1, renewal_gate=asyncio.Event())
    processor = FakeProcessor([ProcessingOutcome.COMPLETED], gate=asyncio.Event())
    before = asyncio.all_tasks()
    async with running_worker(worker(queue, repository, processor, renewal_sleep=pulse)) as task:
        await processor.started.wait()
        pulse.ticks.put_nowait(None)
        await repository.renewal_seen.wait()
        await task
    assert processor.finished.is_set()
    assert queue.events == ["group"]
    assert asyncio.all_tasks() <= before


@pytest.mark.asyncio
async def test_worker_cancellation_drains_lease_and_processing_tasks() -> None:
    queue, repository, pulse = FakeQueue(new=(MESSAGE,)), FakeRepository(), LeasePulse()
    processor = FakeProcessor([ProcessingOutcome.COMPLETED], gate=asyncio.Event())
    before = asyncio.all_tasks()
    async with running_worker(worker(queue, repository, processor, renewal_sleep=pulse)) as task:
        await processor.started.wait()
        await pulse.waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert pulse.cancelled
    assert processor.finished.is_set()
    assert repository.events == ["lock", "release"]
    assert queue.events == ["group"]
    assert asyncio.all_tasks() <= before


@pytest.mark.asyncio
async def test_lease_release_timeout_does_not_leave_an_owned_task() -> None:
    queue = FakeQueue(new=(MESSAGE,))
    repository = FakeRepository(lease_seconds=1, release_gate=asyncio.Event())
    processor = FakeProcessor([ProcessingOutcome.COMPLETED])
    before = asyncio.all_tasks()
    async with running_worker(worker(queue, repository, processor)) as task:
        await task
    assert repository.release_finished.is_set()
    assert queue.events == ["group", "ack:1-0"]
    assert asyncio.all_tasks() <= before


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_lease_release_failure_does_not_mask_processing_outcome(cancelled: bool) -> None:
    queue = FakeQueue(new=(MESSAGE,))
    repository = FakeRepository(release_error=InfrastructureUnavailableError("Document store"))
    processor = FakeProcessor(
        [asyncio.CancelledError() if cancelled else ProcessingOutcome.COMPLETED]
    )

    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await worker(queue, repository, processor).run_once()
    else:
        await worker(queue, repository, processor).run_once()

    assert queue.events == (["group"] if cancelled else ["group", "ack:1-0"])
    assert repository.events == ["lock", "release"]


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
async def test_retries_safe_infrastructure_outage_then_acknowledges_success() -> None:
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    queue = FakeQueue(new=(MESSAGE,))
    repository = FakeRepository()
    processor = FakeProcessor(
        [
            InfrastructureUnavailableError("Document store"),
            ProcessingOutcome.COMPLETED,
        ]
    )

    await worker(queue, repository, processor, sleep=record_sleep).run_once()

    assert processor.attempts == [1, 2]
    assert delays == [1]
    assert queue.events[-1] == "ack:1-0"


@pytest.mark.asyncio
async def test_persists_terminal_failure_before_ack_after_three_attempts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    queue = FakeQueue(new=(MESSAGE,))
    repository = FakeRepository()
    processor = FakeProcessor(
        [RetryableProcessingError("temporary", "Try later.") for _ in range(3)]
    )

    await worker(queue, repository, processor, sleep=no_sleep).run_once()

    assert processor.attempts == [1, 2, 3]
    assert any(
        record.getMessage() == "document_processing_failed" and record.levelno == logging.ERROR
        for record in caplog.records
    )
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


def emitted_logs(records: Sequence[logging.LogRecord]) -> list[Mapping[str, object]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeWorkerFormatter())
    for record in records:
        handler.handle(record)
    result: list[Mapping[str, object]] = []
    for line in stream.getvalue().splitlines():
        value: object = json.loads(line)
        assert isinstance(value, dict)
        result.append(cast(Mapping[str, object], value))
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "event"),
    [
        (ProcessingOutcome.COMPLETED, "document_processing_completed"),
        (ProcessingOutcome.FAILED, "document_processing_failed"),
        (ProcessingOutcome.ALREADY_TERMINAL, "document_already_terminal"),
        (ProcessingOutcome.NOT_FOUND, "document_not_found"),
        (ProcessingOutcome.LOST_OWNERSHIP, "document_ownership_lost"),
    ],
)
async def test_terminal_logs_name_the_actual_processing_outcome(
    outcome: ProcessingOutcome,
    event: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="document_processor.worker"):
        await worker(
            FakeQueue(new=(MESSAGE,)), FakeRepository(), FakeProcessor([outcome])
        ).run_once()
    emitted = emitted_logs(caplog.records)
    assert emitted[-1]["event"] == event
    assert emitted[-1]["outcome"] == outcome.value
    assert emitted[-1]["attempt"] == 1


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
    record_data = emitted_logs(worker_records)
    assert [data.get("event") for data in record_data] == [
        "document_processing_started",
        "document_retry_scheduled",
        "document_processing_started",
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
@pytest.mark.parametrize(
    "outcome,expected_events",
    [
        (ProcessingOutcome.LOST_OWNERSHIP, ["group"]),
        (ProcessingOutcome.NOT_FOUND, ["group", "ack:1-0"]),
    ],
)
async def test_acknowledgement_distinguishes_lost_ownership_from_absence(
    outcome: ProcessingOutcome,
    expected_events: list[str],
) -> None:
    queue = FakeQueue(new=(MESSAGE,))
    repository = FakeRepository()
    processor = FakeProcessor([outcome])

    await worker(queue, repository, processor).run_once()

    assert queue.events == expected_events
    assert repository.events == ["lock", "release"]


@pytest.mark.asyncio
async def test_cancellation_releases_lock_and_leaves_message_pending() -> None:
    queue = FakeQueue(new=(MESSAGE,))
    repository = FakeRepository()
    processor = FakeProcessor([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await worker(queue, repository, processor).run_once()

    assert repository.events == ["lock", "release"]
    assert queue.events == ["group"]
