"""Admission invariants against both fake and disposable real Redis."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import WatchError

from document_processor.domain.errors import (
    InfrastructureUnavailableError,
    RetryableProcessingError,
)
from document_processor.domain.models import (
    DocumentStatus,
    NewDocument,
    PageContent,
    ParserKind,
    StateTransition,
)
from document_processor.domain.provider import ProviderCategory, ProviderFailure
from document_processor.infrastructure.gemini_rate_gate import RedisGeminiRateGate
from document_processor.infrastructure.redis_gateway import RedisSubmissionGateway
from document_processor.infrastructure.redis_repository import (
    AsyncRedisClient,
    RedisDocumentRepository,
)
from document_processor.infrastructure.redis_streams import RedisStreamQueue
from document_processor.services.processing import ProcessingOutcome
from document_processor.worker import DocumentWorker


class ServerTime(Protocol):
    async def time(self) -> tuple[int, int]: ...


@dataclass
class ManualClock:
    now_ms: int = 1_800_000_000_000
    delays: list[float] = field(default_factory=list[float])
    sleepers: asyncio.Queue[asyncio.Future[None]] = field(
        default_factory=asyncio.Queue[asyncio.Future[None]]
    )

    async def time(self) -> tuple[int, int]:
        return self.now_ms // 1000, self.now_ms % 1000 * 1000

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.sleepers.put_nowait(future)
        await future

    async def next_sleep(self) -> asyncio.Future[None]:
        return await asyncio.wait_for(self.sleepers.get(), timeout=2)


@dataclass
class Harness:
    redis: Redis
    clock: ManualClock
    scope: str
    tasks: list[asyncio.Task[None]] = field(default_factory=list[asyncio.Task[None]])

    @property
    def store(self) -> AsyncRedisClient:
        return cast(AsyncRedisClient, self.redis)

    def gate(self, rpm: int = 12, model: str = "test-model") -> RedisGeminiRateGate:
        return RedisGeminiRateGate(
            self.redis,
            scope=self.scope,
            model=model,
            requests_per_minute=rpm,
            sleep=self.clock.sleep,
        )

    def start(self, gate: RedisGeminiRateGate) -> asyncio.Task[None]:
        task = asyncio.create_task(gate.acquire())
        self.tasks.append(task)
        return task


@pytest.fixture
async def harness(queue_redis: Redis, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    clock = ManualClock()

    async def server_time(self: Pipeline) -> tuple[int, int]:
        return await clock.time()

    monkeypatch.setattr(Pipeline, "time", server_time)
    fixture = Harness(queue_redis, clock, uuid4().hex)
    try:
        yield fixture
    finally:
        for task in fixture.tasks:
            task.cancel()
        await asyncio.gather(*fixture.tasks, return_exceptions=True)
        await queue_redis.delete(fixture.gate().key, fixture.gate(model="other").key)


async def test_simultaneous_instances_cannot_burst(harness: Harness) -> None:
    first, second = harness.start(harness.gate()), harness.start(harness.gate())
    done, pending = await asyncio.wait(
        {first, second}, timeout=2, return_when=asyncio.FIRST_COMPLETED
    )
    assert len(done) == len(pending) == 1
    await next(iter(done))
    sleeping = await harness.clock.next_sleep()
    assert harness.clock.delays == [5.0]
    harness.clock.now_ms += 4999
    sleeping.set_result(None)
    sleeping = await harness.clock.next_sleep()
    assert harness.clock.delays[-1] == 0.001
    assert not next(iter(pending)).done()
    harness.clock.now_ms += 1
    sleeping.set_result(None)
    await asyncio.wait_for(next(iter(pending)), timeout=2)


async def test_restart_and_extended_cooldown_recheck_after_wake(harness: Harness) -> None:
    first = harness.gate()
    await first.acquire()
    restarted = harness.start(harness.gate())
    sleeping = await harness.clock.next_sleep()
    cooldown = await first.cool_down(60)
    assert cooldown.delay_seconds == 60
    assert cooldown.not_before.timestamp() * 1000 == harness.clock.now_ms + 60000
    harness.clock.now_ms += 5000
    sleeping.set_result(None)
    sleeping = await harness.clock.next_sleep()
    assert harness.clock.delays[-1] == 55
    assert not restarted.done()
    unchanged = await harness.gate().cool_down(10)
    assert unchanged.not_before == cooldown.not_before
    assert unchanged.delay_seconds == 55
    harness.clock.now_ms += 55000
    sleeping.set_result(None)
    await asyncio.wait_for(restarted, timeout=2)


async def test_model_isolation_and_ttl_outlive_latest_deadline(harness: Harness) -> None:
    gate = harness.gate()
    await gate.acquire()
    await gate.cool_down(86405)
    ttl = await harness.redis.pttl(gate.key)
    assert 86464000 <= ttl <= 86465000
    await harness.gate(model="other").acquire()


async def test_cancelled_wait_does_not_reset_or_refund_admission(harness: Harness) -> None:
    gate = harness.gate()
    await gate.acquire()
    before = await harness.store.hgetall(gate.key)
    waiter = harness.start(gate)
    await harness.clock.next_sleep()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert await harness.store.hgetall(gate.key) == before
    another = harness.start(harness.gate())
    await harness.clock.next_sleep()
    assert not another.done()
    assert harness.clock.delays == [5, 5]


async def test_budget_disagreement_fails_closed(harness: Harness) -> None:
    gate = harness.gate()
    await gate.acquire()
    before = await harness.store.hgetall(gate.key)
    with pytest.raises(InfrastructureUnavailableError):
        await harness.gate(rpm=15).acquire()
    assert await harness.store.hgetall(gate.key) == before


@pytest.mark.parametrize("value", ["broken", "-1", "9" * 5000])
async def test_invalid_state_cannot_authorize_a_request(harness: Harness, value: str) -> None:
    gate = harness.gate()
    await gate.acquire()
    await harness.store.hset(gate.key, mapping={"next_ms": value})
    with pytest.raises(InfrastructureUnavailableError) as caught:
        await gate.acquire()
    assert value not in str(caught.value)


async def test_missing_state_field_is_not_treated_as_an_empty_budget(harness: Harness) -> None:
    gate = harness.gate()
    await harness.store.hset(gate.key, mapping={"interval_ms": 5000})
    with pytest.raises(InfrastructureUnavailableError):
        await gate.acquire()


@pytest.mark.parametrize("after_commit", [False, True])
async def test_uncertain_commit_never_returns_a_permit(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, after_commit: bool
) -> None:
    execute = Pipeline.execute

    async def disconnected(self: Pipeline, raise_on_error: bool = True) -> list[object]:
        if after_commit:
            await execute(self, raise_on_error=raise_on_error)
        raise RedisConnectionError("PRIVATE_REDIS_CANARY")

    with monkeypatch.context() as patch:
        patch.setattr(Pipeline, "execute", disconnected)
        with pytest.raises(InfrastructureUnavailableError) as caught:
            await harness.gate().acquire()
        assert "PRIVATE_REDIS_CANARY" not in str(caught.value)
    if after_commit:
        waiter = harness.start(harness.gate())
        await harness.clock.next_sleep()
        assert not waiter.done()
        assert harness.clock.delays == [5]
    else:
        await harness.gate().acquire()


async def test_conflict_retries_are_bounded(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def conflict(self: Pipeline, raise_on_error: bool = True) -> list[object]:
        nonlocal calls
        calls += 1
        raise WatchError()

    monkeypatch.setattr(Pipeline, "execute", conflict)
    with pytest.raises(InfrastructureUnavailableError):
        await harness.gate().acquire()
    assert calls == 5


async def test_expired_state_can_restart_after_its_deadline(harness: Harness) -> None:
    gate = harness.gate()
    await gate.acquire()
    harness.clock.now_ms += 65000
    await harness.redis.pexpire(gate.key, 1)
    await asyncio.sleep(0.02)
    assert not await harness.redis.exists(gate.key)
    await harness.gate().acquire()
    assert 64000 <= await harness.redis.pttl(gate.key) <= 65000


async def test_unavailable_redis_does_not_authorize_calls(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(self: Pipeline, *names: object) -> None:
        raise RedisConnectionError("PRIVATE_REDIS_CANARY")

    monkeypatch.setattr(Pipeline, "watch", unavailable)
    with pytest.raises(InfrastructureUnavailableError):
        await harness.gate().acquire()


@pytest.mark.parametrize("rpm,spacing", [(1, 60), (13, 4.616), (1000, 0.06)])
async def test_budget_boundaries_round_spacing_up(
    harness: Harness, rpm: int, spacing: float
) -> None:
    gate = harness.gate(rpm=rpm)
    await gate.acquire()
    harness.start(gate)
    await harness.clock.next_sleep()
    assert harness.clock.delays == [spacing]


async def test_unrepresentable_deadline_fails_as_coordination_uncertainty(harness: Harness) -> None:
    with pytest.raises(InfrastructureUnavailableError):
        await harness.gate().cool_down(1e308)


async def test_real_server_time_and_retention_are_used(queue_redis: Redis) -> None:
    gate = RedisGeminiRateGate(queue_redis, scope=uuid4().hex, model="test")
    store = cast(AsyncRedisClient, queue_redis)
    clock = cast(ServerTime, queue_redis)
    before = await clock.time()
    try:
        await gate.acquire()
        state = await store.hgetall(gate.key)
        ttl = await queue_redis.pttl(gate.key)
        after = await clock.time()
        next_ms = int(state[b"next_ms"])
        before_ms = before[0] * 1000 + before[1] // 1000
        after_ms = after[0] * 1000 + after[1] // 1000
        assert before_ms + 5000 <= next_ms <= after_ms + 5000
        assert next_ms - after_ms + 60000 <= ttl <= 65000
    finally:
        await queue_redis.delete(gate.key)


@pytest.mark.parametrize("rpm", [0, -1, 1001, True])
def test_invalid_budget_rejected(rpm: int) -> None:
    with pytest.raises(ValueError):
        RedisGeminiRateGate(cast(Redis, None), scope="test", model="test", requests_per_minute=rpm)


@pytest.mark.parametrize("scope", ["", "bad:scope", "a" * 65, "line\nbreak"])
def test_invalid_scope_rejected(scope: str) -> None:
    with pytest.raises(ValueError):
        RedisGeminiRateGate(cast(Redis, None), scope=scope, model="test", requests_per_minute=12)


@pytest.mark.parametrize("status,category", [(429, "rate_limited"), (503, "unavailable")])
async def test_worker_terminal_failure_cools_down_another_worker(
    harness: Harness,
    status: int,
    category: ProviderCategory,
) -> None:
    repository = RedisDocumentRepository(harness.redis)
    stream = "test-cooldown:" + harness.scope
    queue = RedisStreamQueue(harness.redis, stream_name=stream, group_name="workers")
    now = datetime.now(UTC)
    documents = [
        NewDocument(
            id=uuid4(),
            filename="synthetic.pdf",
            parser=ParserKind.PYPDF,
            status=DocumentStatus.QUEUED,
            content=b"%PDF-synthetic",
            created_at=now,
        )
        for _ in range(2)
    ]
    calls: list[tuple[UUID, int]] = []

    class Processor:
        def __init__(self, gate: RedisGeminiRateGate, fail: bool) -> None:
            self.gate, self.fail = gate, fail

        async def process(
            self, document_id: UUID, *, token: str, attempt: int
        ) -> ProcessingOutcome:
            await self.gate.acquire()
            calls.append((document_id, harness.clock.now_ms))
            if self.fail:
                raise RetryableProcessingError(
                    "gemini_temporarily_unavailable",
                    "Safe provider failure.",
                    provider_info=ProviderFailure(category, "summarization", status),
                    retry_after_seconds=60,
                )
            assert (
                await repository.complete(
                    document_id,
                    token=token,
                    pages=[PageContent(0, "synthetic")],
                    summary="synthetic",
                    completed_at=now,
                    attempts=attempt,
                )
                is StateTransition.APPLIED
            )
            return ProcessingOutcome.COMPLETED

    def worker(fail: bool) -> DocumentWorker:
        gate = harness.gate()
        return DocumentWorker(
            queue=queue,
            repository=repository,
            processor=Processor(gate, fail),
            consumer_name="first" if fail else "second",
            max_attempts=1,
            retry_base_seconds=15,
            retry_jitter_seconds=0,
            reclaim_idle_ms=30000,
            read_block_ms=1,
            batch_size=1,
            lock_ttl_seconds=60,
            random=lambda: 0,
            provider_gate=gate,
            clock=lambda: now,
        )

    second: asyncio.Task[int] | None = None
    try:
        await RedisSubmissionGateway(
            harness.redis, ttl_seconds=120, stream_name=stream
        ).create_batch(documents)
        assert await worker(True).run_once() == 1
        failed = await repository.get(documents[0].id)
        assert failed is not None and failed.status is DocumentStatus.FAILED
        second = asyncio.create_task(worker(False).run_once())
        sleeping = await harness.clock.next_sleep()
        assert len(calls) == 1
        assert harness.clock.delays[-1] == 60
        harness.clock.now_ms += 59999
        sleeping.set_result(None)
        sleeping = await harness.clock.next_sleep()
        assert len(calls) == 1 and not second.done()
        harness.clock.now_ms += 1
        sleeping.set_result(None)
        assert await asyncio.wait_for(second, timeout=2) == 1
        assert calls[1][1] - calls[0][1] == 60000
    finally:
        if second is not None:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        keys = [stream]
        for document in documents:
            keys.extend(
                [
                    repository.document_key(document.id),
                    repository.source_key(document.id),
                    repository.lock_key(document.id),
                ]
            )
        await harness.redis.delete(*keys)
