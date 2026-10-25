"""Worker recovery against durable Redis retry state, without provider I/O."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest

from document_processor.domain.errors import (
    InfrastructureUnavailableError,
    PermanentProcessingError,
    RetryableProcessingError,
)
from document_processor.domain.models import DocumentStatus, PageContent, StateTransition
from document_processor.domain.provider import AttemptFailure, ProviderFailure
from document_processor.infrastructure.redis_streams import StreamMessage
from document_processor.services.processing import ProcessingOutcome
from document_processor.worker import DocumentWorker
from tests.integration.test_retry_state import ENCODED_FAILURE, NOW, OWNER, UNTIL, Store
from tests.integration.test_retry_state import store as store
from tests.unit.test_worker import FakeQueue

INFO = ProviderFailure("unavailable", "summarization", 503)


def provider_error() -> RetryableProcessingError:
    return RetryableProcessingError(
        "gemini_temporarily_unavailable",
        "PRIVATE_PROVIDER_CANARY",
        provider_info=INFO,
        retry_after_seconds=60,
    )


@dataclass
class Run:
    worker: DocumentWorker
    queue: FakeQueue
    calls: list[int]


async def make_worker(
    state: Store,
    results: list[BaseException | None],
    *,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    lock_ttl_seconds: int = 120,
) -> Run:
    await state.repo.release_lock(state.document.id, OWNER)
    calls: list[int] = []

    class Processor:
        async def process(
            self, document_id: UUID, *, token: str, attempt: int
        ) -> ProcessingOutcome:
            calls.append(attempt)
            if not results:
                pytest.fail("Recovery must not make a fourth processing call.")
            result = results.pop(0)
            if result is not None:
                raise result
            assert (
                await state.repo.complete(
                    document_id,
                    token=token,
                    pages=[PageContent(0, "synthetic")],
                    summary="synthetic",
                    completed_at=state.server_now,
                    attempts=attempt,
                )
                is StateTransition.APPLIED
            )
            return ProcessingOutcome.COMPLETED

    async def advance(seconds: float) -> None:
        state.server_now += timedelta(seconds=seconds)

    queue = FakeQueue(stale=(StreamMessage("1-0", state.document.id, 20),))
    worker = DocumentWorker(
        queue=queue,
        repository=state.repo,
        processor=Processor(),
        consumer_name="worker-one",
        max_attempts=3,
        retry_base_seconds=15,
        retry_jitter_seconds=0,
        reclaim_idle_ms=30000,
        read_block_ms=50,
        batch_size=10,
        lock_ttl_seconds=lock_ttl_seconds,
        sleep=sleep or advance,
        random=lambda: 0,
        clock=lambda: state.server_now,
        token_factory=lambda: "replacement-owner",
    )
    return Run(worker, queue, calls)


@pytest.mark.parametrize("expire", [False, True])
async def test_deferred_recovery_cancels_when_lease_or_document_is_lost(
    store: Store,
    expire: bool,
) -> None:
    await store.raw.hset(
        store.key, mapping={"last_failure": ENCODED_FAILURE, "retry_not_before": UNTIL.isoformat()}
    )
    started, finished = asyncio.Event(), asyncio.Event()

    async def blocked(seconds: float) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    run = await make_worker(store, [], sleep=blocked, lock_ttl_seconds=1)
    task = asyncio.create_task(run.worker.run_once())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        if expire:
            await store.redis.pexpire(store.key, 1)
        else:
            await store.raw.set(
                store.repo.lock_key(store.document.id), "another-owner", ex=120, nx=False
            )
        await asyncio.wait_for(task, timeout=2)
        assert finished.is_set() and run.calls == [] and run.queue.events == ["group"]
        record = await store.repo.get(store.document.id)
        assert record is None if expire else record is not None and record.attempts == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_failure_is_durable_before_sleep_and_success_clears_it(store: Store) -> None:
    await store.raw.hset(store.key, mapping={"attempts": 0})

    async def sleep(seconds: float) -> None:
        record = await store.repo.get(store.document.id)
        assert record is not None and record.last_failure == AttemptFailure(1, INFO)
        assert record.retry_not_before == UNTIL and record.attempts == 1
        assert await store.repo.get_source(store.document.id) == b"%PDF-test"
        store.server_now += timedelta(seconds=seconds)

    run = await make_worker(store, [provider_error(), None], sleep=sleep)
    await run.worker.run_once()
    assert run.calls == [1, 2]
    record = await store.repo.get(store.document.id)
    assert record is not None and record.status is DocumentStatus.COMPLETED
    assert record.last_failure is None and record.retry_not_before is None
    assert run.queue.events[-1] == "ack:1-0"


async def test_replacement_worker_honors_future_deadline_before_admission(store: Store) -> None:
    await store.raw.hset(
        store.key, mapping={"last_failure": ENCODED_FAILURE, "retry_not_before": UNTIL.isoformat()}
    )
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        record = await store.repo.get(store.document.id)
        assert record is not None and record.attempts == 1
        assert run.calls == []
        sleeps.append(seconds)
        store.server_now += timedelta(seconds=seconds)

    run = await make_worker(store, [None], sleep=sleep)
    await run.worker.run_once()
    assert sleeps == [60] and run.calls == [2]


async def test_final_failure_survives_crash_before_terminal_publication(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await store.raw.hset(store.key, mapping={"attempts": 0})

    async def crash(*args: object, **kwargs: object) -> StateTransition:
        raise asyncio.CancelledError

    first = await make_worker(store, [provider_error(), provider_error(), provider_error()])
    with monkeypatch.context() as patch:
        patch.setattr(store.repo, "fail", crash)
        with pytest.raises(asyncio.CancelledError):
            await first.worker.run_once()
    record = await store.repo.get(store.document.id)
    assert record is not None and record.last_failure == AttemptFailure(3, INFO)
    assert record.attempts == 3 and record.retry_not_before is None
    assert first.queue.events == ["group"]
    recovered = await make_worker(store, [])
    await recovered.worker.run_once()
    assert first.calls == [1, 2, 3] and recovered.calls == []
    result = await store.repo.get(store.document.id)
    assert result is not None and result.error_code == "retry_exhausted"
    assert result.error_message is not None and "unavailable" in result.error_message
    assert result.last_failure == AttemptFailure(3, INFO)
    assert await store.repo.get_source(store.document.id) is None
    assert recovered.queue.events[-1] == "ack:1-0"
    assert "PRIVATE_PROVIDER_CANARY" not in str(await store.raw.hgetall(store.key))


@pytest.mark.parametrize("committed", [False, True])
async def test_uncertain_final_record_leaves_pending_then_recovers_only_confirmed_context(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    await store.raw.hset(store.key, mapping={"attempts": 2, "last_failure": ENCODED_FAILURE})
    record_failure = store.repo.record_retry_failure

    async def uncertain(
        document_id: UUID,
        *,
        token: str,
        failure: AttemptFailure,
        retry_not_before: object,
        updated_at: object,
    ) -> StateTransition:
        if committed:
            await record_failure(
                document_id,
                token=token,
                failure=failure,
                retry_not_before=None,
                updated_at=store.server_now,
            )
        raise InfrastructureUnavailableError("Retry state")

    first = await make_worker(store, [provider_error()])
    with monkeypatch.context() as patch:
        patch.setattr(store.repo, "record_retry_failure", uncertain)
        await first.worker.run_once()
    assert first.calls == [3] and first.queue.events == ["group"]
    assert await store.repo.get_source(store.document.id) == b"%PDF-test"
    recovered = await make_worker(store, [])
    await recovered.worker.run_once()
    result = await store.repo.get(store.document.id)
    assert result is not None and result.error_message is not None
    assert result.last_failure is not None
    assert result.last_failure.attempt == (3 if committed else 1)
    if committed:
        assert "unavailable" in result.error_message
    else:
        assert "Last confirmed failure on attempt 1" in result.error_message
        assert "API limit" in result.error_message and "unavailable" not in result.error_message
    assert recovered.calls == [] and recovered.queue.events[-1] == "ack:1-0"


@pytest.mark.parametrize("permanent", [False, True])
async def test_later_nonprovider_failure_removes_old_provider_attribution(
    store: Store, permanent: bool
) -> None:
    await store.raw.hset(store.key, mapping={"attempts": 2, "last_failure": ENCODED_FAILURE})
    error = (
        PermanentProcessingError("invalid_pdf", "Invalid PDF.")
        if permanent
        else InfrastructureUnavailableError("Document store")
    )
    run = await make_worker(store, [error])
    await run.worker.run_once()
    record = await store.repo.get(store.document.id)
    assert record is not None and record.status is DocumentStatus.FAILED
    assert record.last_failure == (None if permanent else AttemptFailure(3, None))
    assert record.error_message is not None and "Gemini" not in record.error_message
    assert record.retry_not_before is None and run.calls == [3]


async def test_ownership_loss_while_recording_does_not_ack_or_continue(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.raw.hset(store.key, mapping={"attempts": 0})
    record_failure = store.repo.record_retry_failure

    async def lost(
        document_id: UUID,
        *,
        token: str,
        failure: AttemptFailure,
        retry_not_before: object,
        updated_at: object,
    ) -> StateTransition:
        await store.raw.set(store.repo.lock_key(document_id), "another-owner", ex=120, nx=False)
        return await record_failure(
            document_id, token=token, failure=failure, retry_not_before=UNTIL, updated_at=NOW
        )

    monkeypatch.setattr(store.repo, "record_retry_failure", lost)
    run = await make_worker(store, [provider_error()])
    await run.worker.run_once()
    assert run.calls == [1] and run.queue.events == ["group"]
    assert await store.repo.get_source(store.document.id) == b"%PDF-test"
