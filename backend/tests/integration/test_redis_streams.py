"""Integration tests for Redis Streams queue behavior."""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import fakeredis.aioredis
import pytest
from redis.asyncio import Redis

from document_processor.domain.models import DocumentStatus, NewDocument, ParserKind
from document_processor.infrastructure.redis_gateway import RedisSubmissionGateway
from document_processor.infrastructure.redis_streams import RedisStreamQueue

FIRST_ID = UUID("00000000-0000-4000-8000-000000000001")
SECOND_ID = UUID("00000000-0000-4000-8000-000000000002")


async def enqueue(redis: Redis, identifiers: Sequence[UUID]) -> None:
    await RedisSubmissionGateway(redis, ttl_seconds=60, stream_name="test:documents").create_batch(
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


@asynccontextmanager
async def queue() -> AsyncGenerator[tuple[RedisStreamQueue, Redis]]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    stream_queue = RedisStreamQueue(
        redis,
        stream_name="test:documents",
        group_name="test-workers",
    )
    try:
        yield stream_queue, redis
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_creates_consumer_group_idempotently() -> None:
    async with queue() as (stream_queue, redis):
        await stream_queue.ensure_group()
        await stream_queue.ensure_group()

        groups = await redis.xinfo_groups("test:documents")

    assert len(groups) == 1
    assert groups[0]["name"] == b"test-workers"


@pytest.mark.asyncio
async def test_submission_enqueues_a_complete_batch() -> None:
    async with queue() as (_stream_queue, redis):
        await enqueue(redis, [FIRST_ID, SECOND_ID])
        entries = await redis.xrange("test:documents")

    assert len({message_id for message_id, _fields in entries}) == 2
    assert len(entries) == 2
    assert [fields[b"document_id"] for _message_id, fields in entries] == [
        str(FIRST_ID).encode(),
        str(SECOND_ID).encode(),
    ]


@pytest.mark.asyncio
async def test_reads_and_acknowledges_new_messages() -> None:
    async with queue() as (stream_queue, redis):
        await stream_queue.ensure_group()
        await enqueue(redis, [FIRST_ID])

        messages = await stream_queue.read_new("worker-one", count=10, block_ms=1)
        pending_before = await redis.xpending("test:documents", "test-workers")
        acknowledged = await stream_queue.acknowledge(messages[0].message_id)
        pending_after = await redis.xpending("test:documents", "test-workers")

    assert [(message.document_id, message.delivery_count) for message in messages] == [
        (FIRST_ID, 1)
    ]
    assert pending_before["pending"] == 1
    assert acknowledged == 1
    assert pending_after["pending"] == 0


@pytest.mark.asyncio
async def test_reclaims_stale_pending_message_for_another_consumer() -> None:
    async with queue() as (stream_queue, redis):
        await stream_queue.ensure_group()
        await enqueue(redis, [FIRST_ID])
        original = await stream_queue.read_new("worker-one", count=1, block_ms=1)

        reclaimed = await stream_queue.reclaim_stale(
            "worker-two",
            minimum_idle_ms=0,
            count=10,
        )

    assert [message.message_id for message in reclaimed] == [original[0].message_id]
    assert [message.document_id for message in reclaimed] == [FIRST_ID]
    assert reclaimed[0].delivery_count >= 2


@pytest.mark.asyncio
async def test_acknowledged_redelivery_is_not_reclaimed_again() -> None:
    async with queue() as (stream_queue, redis):
        await stream_queue.ensure_group()
        await enqueue(redis, [FIRST_ID])
        await stream_queue.read_new("worker-one", count=1, block_ms=1)
        reclaimed = await stream_queue.reclaim_stale(
            "worker-two",
            minimum_idle_ms=0,
            count=1,
        )
        await stream_queue.acknowledge(reclaimed[0].message_id)

        later = await stream_queue.reclaim_stale(
            "worker-three",
            minimum_idle_ms=0,
            count=1,
        )

    assert later == ()
