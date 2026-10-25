"""Queue lifecycle checks against fake and disposable real Redis."""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline
from redis.exceptions import ConnectionError as RedisConnectionError

from document_processor.domain.models import DocumentStatus, NewDocument, ParserKind
from document_processor.infrastructure.redis_gateway import RedisSubmissionGateway
from document_processor.infrastructure.redis_repository import RedisDocumentRepository
from document_processor.infrastructure.redis_streams import RedisStreamQueue

FIRST_ID = UUID("00000000-0000-4000-8000-000000000001")
SECOND_ID = UUID("00000000-0000-4000-8000-000000000002")
THIRD_ID = UUID("00000000-0000-4000-8000-000000000003")
type QueueStore = tuple[RedisStreamQueue, Redis]


async def enqueue(store: QueueStore, identifiers: Sequence[UUID]) -> None:
    stream_queue, redis = store
    await RedisSubmissionGateway(
        redis, ttl_seconds=60, stream_name=stream_queue.stream_name
    ).create_batch(
        [
            NewDocument(
                id=identifier,
                filename=f"{identifier}.pdf",
                parser=ParserKind.PYPDF,
                status=DocumentStatus.QUEUED,
                content=b"%PDF-1.7\nfixture",
                created_at=datetime(2026, 10, 23, tzinfo=UTC),
            )
            for identifier in identifiers
        ]
    )


@pytest.fixture
async def store(queue_redis: Redis) -> AsyncIterator[QueueStore]:
    stream_queue = RedisStreamQueue(
        queue_redis,
        stream_name=f"test-streams:{uuid4()}",
        group_name="test-workers",
    )
    try:
        yield stream_queue, queue_redis
    finally:
        keys = [stream_queue.stream_name]
        for identifier in (FIRST_ID, SECOND_ID, THIRD_ID):
            keys.extend(
                [
                    RedisDocumentRepository.document_key(identifier),
                    RedisDocumentRepository.source_key(identifier),
                    RedisDocumentRepository.lock_key(identifier),
                ]
            )
        await queue_redis.delete(*keys)


@pytest.mark.asyncio
async def test_creates_consumer_group_idempotently(store: QueueStore) -> None:
    queue, redis = store
    await queue.ensure_group()
    await queue.ensure_group()

    groups = await redis.xinfo_groups(queue.stream_name)

    assert len(groups) == 1
    assert groups[0]["name"] == b"test-workers"


@pytest.mark.asyncio
async def test_submission_enqueues_a_complete_batch(store: QueueStore) -> None:
    queue, redis = store
    await enqueue(store, [FIRST_ID, SECOND_ID])
    entries = await redis.xrange(queue.stream_name)

    assert len({message_id for message_id, _fields in entries}) == 2
    assert len(entries) == 2
    assert [fields[b"document_id"] for _message_id, fields in entries] == [
        str(FIRST_ID).encode(),
        str(SECOND_ID).encode(),
    ]


@pytest.mark.asyncio
async def test_reads_and_acknowledges_new_messages(store: QueueStore) -> None:
    queue, redis = store
    await queue.ensure_group()
    await enqueue(store, [FIRST_ID])
    key = RedisDocumentRepository.document_key(FIRST_ID)
    repository = RedisDocumentRepository(redis)
    stored = await repository.get(FIRST_ID)
    ttl = await redis.pttl(key)

    messages = await queue.read_new("worker-one", count=10, block_ms=1)
    pending_before = await redis.xpending(queue.stream_name, queue.group_name)
    acknowledged = await queue.acknowledge(messages[0].message_id)

    assert [(message.document_id, message.delivery_count) for message in messages] == [
        (FIRST_ID, 1)
    ]
    assert pending_before["pending"] == 1
    assert acknowledged == 1
    assert (await redis.xpending(queue.stream_name, queue.group_name))["pending"] == 0
    assert await redis.xlen(queue.stream_name) == 0
    assert await repository.get(FIRST_ID) == stored
    assert 0 < await redis.pttl(key) <= ttl
    assert await queue.acknowledge(messages[0].message_id) == 0
    assert await repository.get(FIRST_ID) == stored


@pytest.mark.asyncio
async def test_reclaims_stale_pending_message_for_another_consumer(store: QueueStore) -> None:
    queue, _redis = store
    await queue.ensure_group()
    await enqueue(store, [FIRST_ID])
    original = await queue.read_new("worker-one", count=1, block_ms=1)

    reclaimed = await queue.reclaim_stale("worker-two", minimum_idle_ms=0, count=10)

    assert [message.message_id for message in reclaimed] == [original[0].message_id]
    assert [message.document_id for message in reclaimed] == [FIRST_ID]
    assert reclaimed[0].delivery_count >= 2


@pytest.mark.asyncio
async def test_acknowledged_redelivery_is_not_reclaimed_again(store: QueueStore) -> None:
    queue, _redis = store
    await queue.ensure_group()
    await enqueue(store, [FIRST_ID])
    await queue.read_new("worker-one", count=1, block_ms=1)
    reclaimed = await queue.reclaim_stale("worker-two", minimum_idle_ms=0, count=1)
    await queue.acknowledge(reclaimed[0].message_id)

    assert await queue.reclaim_stale("worker-three", minimum_idle_ms=0, count=1) == ()


@pytest.mark.asyncio
async def test_cleanup_preserves_pending_and_unread_siblings(store: QueueStore) -> None:
    queue, redis = store
    await queue.ensure_group()
    await enqueue(store, [FIRST_ID, SECOND_ID, THIRD_ID])
    delivered = await queue.read_new("worker-one", count=2, block_ms=1)

    assert await queue.acknowledge(delivered[0].message_id) == 1

    remaining = await redis.xrange(queue.stream_name)
    assert [fields[b"document_id"] for _mid, fields in remaining] == [
        str(SECOND_ID).encode(),
        str(THIRD_ID).encode(),
    ]
    assert (await redis.xpending(queue.stream_name, queue.group_name))["pending"] == 1
    reclaimed = await queue.reclaim_stale("worker-two", minimum_idle_ms=0, count=10)
    assert [message.document_id for message in reclaimed] == [SECOND_ID]
    unread = await queue.read_new("worker-three", count=10, block_ms=1)
    assert [message.document_id for message in unread] == [THIRD_ID]


@pytest.mark.asyncio
async def test_group_remains_usable_after_removing_last_entry(store: QueueStore) -> None:
    queue, redis = store
    await queue.ensure_group()
    await enqueue(store, [FIRST_ID])
    delivered = await queue.read_new("worker-one", count=1, block_ms=1)
    await queue.acknowledge(delivered[0].message_id)
    assert await redis.xlen(queue.stream_name) == 0
    assert len(await redis.xinfo_groups(queue.stream_name)) == 1

    await enqueue(store, [SECOND_ID])
    later = await queue.read_new("worker-two", count=1, block_ms=1)
    assert [message.document_id for message in later] == [SECOND_ID]


@pytest.mark.asyncio
async def test_group_setup_leaves_historical_acknowledged_entries(store: QueueStore) -> None:
    queue, redis = store
    await queue.ensure_group()
    await enqueue(store, [FIRST_ID])
    delivered = await queue.read_new("old-worker", count=1, block_ms=1)
    # Simulate the previous acknowledgement-only implementation.
    await redis.xack(queue.stream_name, queue.group_name, delivered[0].message_id)

    await queue.ensure_group()
    await enqueue(store, [SECOND_ID])
    later = await queue.read_new("new-worker", count=1, block_ms=1)
    await queue.acknowledge(later[0].message_id)

    retained = await redis.xrange(queue.stream_name)
    assert [mid.decode() for mid, _fields in retained] == [delivered[0].message_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True])
async def test_cleanup_executes_both_commands_in_one_transaction(
    store: QueueStore, monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    queue, redis = store
    await queue.ensure_group()
    await enqueue(store, [FIRST_ID])
    delivered = await queue.read_new("worker-one", count=1, block_ms=1)
    message_id = delivered[0].message_id
    original_pipeline = redis.pipeline
    executions: list[Sequence[tuple[object, ...]]] = []

    def inspected_pipeline(*, transaction: bool = True) -> Pipeline:
        assert transaction is True
        pipeline = original_pipeline(transaction=transaction)
        execute = pipeline.execute

        async def inspected_execute(raise_on_error: bool = True) -> list[object]:
            commands = [args for args, _options in pipeline.command_stack]
            executions.append(commands)
            assert commands == [
                ("XACK", queue.stream_name, queue.group_name, message_id),
                ("XDEL", queue.stream_name, message_id),
            ]
            # Buffered commands must not affect server state before EXEC.
            assert (await redis.xpending(queue.stream_name, queue.group_name))["pending"] == 1
            assert await redis.xlen(queue.stream_name) == 1
            if interrupt:
                raise RedisConnectionError("Synthetic interruption before execution")
            return cast(list[object], await execute(raise_on_error=raise_on_error))

        monkeypatch.setattr(pipeline, "execute", inspected_execute)
        return pipeline

    monkeypatch.setattr(redis, "pipeline", inspected_pipeline)
    if interrupt:
        with pytest.raises(RedisConnectionError, match="Synthetic interruption"):
            await queue.acknowledge(message_id)
        assert (await redis.xpending(queue.stream_name, queue.group_name))["pending"] == 1
        assert await redis.xlen(queue.stream_name) == 1
        recovered = await queue.reclaim_stale("replacement", minimum_idle_ms=0, count=1)
        assert [message.message_id for message in recovered] == [message_id]
    else:
        assert await queue.acknowledge(message_id) == 1
        assert (await redis.xpending(queue.stream_name, queue.group_name))["pending"] == 0
        assert await redis.xlen(queue.stream_name) == 0
    assert len(executions) == 1
