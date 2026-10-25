"""State publication invariants against fake and disposable real Redis."""

import asyncio
import os
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

import fakeredis.aioredis
import pytest
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline

from document_processor.domain.errors import PermanentProcessingError, RetryableProcessingError
from document_processor.domain.models import (
    DocumentStatus,
    NewDocument,
    PageContent,
    ParserKind,
    StateTransition,
)
from document_processor.infrastructure.redis_gateway import RedisSubmissionGateway
from document_processor.infrastructure.redis_repository import RedisDocumentRepository
from document_processor.infrastructure.redis_streams import StreamMessage
from document_processor.services.processing import ProcessingOutcome, ProcessingService
from document_processor.worker import DocumentWorker

NOW = datetime(2026, 10, 24, tzinfo=UTC)
OWNER = "current-owner"
type Operation = Literal["processing", "complete", "fail"]


@dataclass
class Store:
    redis: Redis
    repository: RedisDocumentRepository
    document: NewDocument


@pytest.fixture(params=["fake", "real"])
async def store(request: pytest.FixtureRequest) -> AsyncGenerator[Store]:
    if request.param == "real":
        url = os.getenv("TEST_REDIS_URL")
        if url is None:
            pytest.skip("Set TEST_REDIS_URL to a disposable Redis 7 instance.")
        parsed = urlparse(url)
        redis = Redis(
            host=parsed.hostname or "localhost",
            port=parsed.port or 6379,
            db=int(parsed.path.removeprefix("/") or "0"),
            password=parsed.password,
            decode_responses=False,
        )
    else:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    document = NewDocument(
        id=uuid4(),
        filename="synthetic.pdf",
        parser=ParserKind.PYPDF,
        status=DocumentStatus.QUEUED,
        content=b"%PDF-1.7\nsynthetic",
        created_at=NOW,
    )
    repository = RedisDocumentRepository(redis, ttl_seconds=30)
    stream = f"test-transitions:{document.id}"
    try:
        await RedisSubmissionGateway(redis, ttl_seconds=30, stream_name=stream).create_batch(
            [document]
        )
        assert await repository.acquire_lock(document.id, OWNER, ttl_seconds=30)
        await repository.mark_processing(document.id, token=OWNER, attempt=1, updated_at=NOW)
        yield Store(redis, repository, document)
    finally:
        await redis.delete(
            repository.document_key(document.id),
            repository.source_key(document.id),
            repository.lock_key(document.id),
            stream,
        )
        await redis.aclose()


async def transition(
    store: Store,
    operation: Operation,
    *,
    attempt: int = 1,
) -> StateTransition:
    repository, identifier = store.repository, store.document.id
    if operation == "processing":
        return await repository.mark_processing(
            identifier,
            token=OWNER,
            attempt=attempt,
            updated_at=NOW,
        )
    elif operation == "complete":
        return await repository.complete(
            identifier,
            token=OWNER,
            pages=(PageContent(0, "Synthetic page"),),
            summary="Synthetic summary",
            completed_at=NOW,
            attempts=attempt,
        )
    else:
        return await repository.fail(
            identifier,
            token=OWNER,
            error_code="invalid_pdf",
            error_message="Safe failure.",
            completed_at=NOW,
            attempts=attempt,
        )


@pytest.mark.parametrize("operation", ["processing", "complete", "fail"])
async def test_absent_document_stays_absent(store: Store, operation: Operation) -> None:
    key = store.repository.document_key(store.document.id)
    await store.redis.delete(key)

    assert await transition(store, operation) is StateTransition.NOT_FOUND

    assert await store.redis.exists(key) == 0
    assert await store.repository.get(store.document.id) is None
    assert await store.repository.get_source(store.document.id) == store.document.content


@pytest.mark.parametrize("operation", ["processing", "complete", "fail"])
async def test_replaced_owner_cannot_write_or_delete_source(
    store: Store,
    operation: Operation,
) -> None:
    before = await store.repository.get(store.document.id)
    await store.redis.set(store.repository.lock_key(store.document.id), "replacement", ex=30)

    assert await transition(store, operation, attempt=2) is StateTransition.LOST_OWNERSHIP

    assert await store.repository.get(store.document.id) == before
    assert await store.repository.get_source(store.document.id) == store.document.content


@pytest.mark.parametrize("terminal", ["complete", "fail"])
@pytest.mark.parametrize("operation", ["processing", "complete", "fail"])
async def test_terminal_results_are_immutable(
    store: Store,
    terminal: Operation,
    operation: Operation,
) -> None:
    await transition(store, terminal)
    before = await store.repository.get(store.document.id)

    assert await transition(store, operation, attempt=2) is StateTransition.ALREADY_TERMINAL

    assert await store.repository.get(store.document.id) == before


@pytest.mark.parametrize("operation", ["complete", "fail"])
async def test_old_attempt_cannot_publish_under_the_same_lease(
    store: Store,
    operation: Operation,
) -> None:
    await transition(store, "processing", attempt=2)
    before = await store.repository.get(store.document.id)

    assert await transition(store, operation, attempt=1) is StateTransition.LOST_OWNERSHIP

    assert await store.repository.get(store.document.id) == before
    assert await store.repository.get_source(store.document.id) == store.document.content


@pytest.mark.parametrize("operation", ["processing", "complete", "fail"])
@pytest.mark.parametrize("interruption", ["expiry", "replacement"])
async def test_guards_are_rechecked_when_state_changes_before_exec(
    store: Store,
    operation: Operation,
    interruption: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = store.repository.document_key(store.document.id)
    before = await store.repository.get(store.document.id)
    execute = Pipeline.execute
    interrupted = False

    async def interrupt(pipeline: Pipeline, raise_on_error: bool = True) -> list[object]:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            if interruption == "expiry":
                await store.redis.pexpire(key, 10)
                expired = asyncio.Event()
                timer = asyncio.get_running_loop().call_later(0.03, expired.set)
                try:
                    async with asyncio.timeout(2):
                        await expired.wait()
                        assert await store.redis.exists(key) == 0
                finally:
                    timer.cancel()
            else:
                await store.redis.set(
                    store.repository.lock_key(store.document.id),
                    "replacement",
                    ex=30,
                )
        return cast(list[object], await execute(pipeline, raise_on_error=raise_on_error))

    monkeypatch.setattr(Pipeline, "execute", interrupt)

    result = await transition(store, operation)
    assert result is (
        StateTransition.NOT_FOUND if interruption == "expiry" else StateTransition.LOST_OWNERSHIP
    )

    assert interrupted
    if interruption == "expiry":
        assert await store.redis.exists(key) == 0
        assert await store.repository.get(store.document.id) is None
    else:
        assert await store.repository.get(store.document.id) == before
    assert await store.repository.get_source(store.document.id) == store.document.content


@pytest.mark.parametrize("operation", ["complete", "fail"])
async def test_valid_publication_keeps_the_original_retention_deadline(
    store: Store,
    operation: Operation,
) -> None:
    key = store.repository.document_key(store.document.id)
    await store.redis.pexpire(key, 5000)
    before = await store.redis.pttl(key)

    assert await transition(store, operation) is StateTransition.APPLIED

    document = await store.repository.get(store.document.id)
    assert document is not None
    assert document.status.value == ("completed" if operation == "complete" else "failed")
    assert document.attempts == 1
    assert document.completed_at == NOW
    assert await store.repository.get_source(store.document.id) is None
    assert 0 < await store.redis.pttl(key) <= before


async def interrupt_owner(store: Store, interruption: str) -> None:
    if interruption == "expiry":
        await store.redis.delete(store.repository.document_key(store.document.id))
    else:
        await store.redis.set(store.repository.lock_key(store.document.id), "replacement", ex=30)


class InterruptedParser:
    def __init__(self, store: Store, interruption: str, failure: bool) -> None:
        self.store, self.interruption, self.failure = store, interruption, failure

    async def parse(self, content: bytes) -> tuple[PageContent, ...]:
        assert content == self.store.document.content
        await interrupt_owner(self.store, self.interruption)
        if self.failure:
            raise PermanentProcessingError("invalid_pdf", "Safe failure.")
        return (PageContent(0, "Synthetic page"),)


class SyntheticSummarizer:
    async def summarize(self, pages: Sequence[PageContent]) -> str:
        assert pages
        return "Synthetic summary"


@pytest.mark.parametrize("path", ["success", "permanent", "missing_source"])
@pytest.mark.parametrize("interruption", ["expiry", "replacement"])
async def test_service_propagates_rejected_terminal_writes(
    store: Store,
    path: str,
    interruption: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = await store.repository.get(store.document.id)
    if path == "missing_source":

        async def missing_source(_identifier: UUID) -> None:
            await interrupt_owner(store, interruption)

        monkeypatch.setattr(store.repository, "get_source", missing_source)
    processor = ProcessingService(
        repository=store.repository,
        parsers={ParserKind.PYPDF: InterruptedParser(store, interruption, path == "permanent")},
        summarizer=SyntheticSummarizer(),
        clock=lambda: NOW,
    )

    result = await processor.process(store.document.id, token=OWNER, attempt=1)

    assert result == ("not_found" if interruption == "expiry" else "lost_ownership")
    assert await store.repository.get(store.document.id) == (
        None if interruption == "expiry" else before
    )
    assert await store.redis.get(store.repository.source_key(store.document.id)) == (
        store.document.content
    )


class OneMessageQueue:
    def __init__(self, identifier: UUID) -> None:
        self.message = StreamMessage("1-0", identifier, 1)
        self.acknowledged: list[str] = []

    async def ensure_group(self) -> None:
        pass

    async def reclaim_stale(
        self,
        consumer_name: str,
        **_options: int,
    ) -> tuple[StreamMessage, ...]:
        assert consumer_name == "test-worker"
        return ()

    async def read_new(self, consumer_name: str, **_options: int) -> tuple[StreamMessage, ...]:
        assert consumer_name == "test-worker"
        return (self.message,)

    async def acknowledge(self, message_id: str) -> int:
        self.acknowledged.append(message_id)
        return 1


class InterruptedProcessor:
    def __init__(self, store: Store, interruption: str, retryable: bool) -> None:
        self.store, self.interruption, self.retryable = store, interruption, retryable

    async def process(
        self,
        document_id: UUID,
        *,
        attempt: int,
        token: str = OWNER,
    ) -> ProcessingOutcome:
        assert document_id == self.store.document.id
        assert token == OWNER
        assert attempt == 1
        await interrupt_owner(self.store, self.interruption)
        if self.retryable:
            raise RetryableProcessingError("temporary", "Try later.")
        raise PermanentProcessingError("invalid_pdf", "Safe failure.")


@pytest.mark.parametrize("retryable", [False, True])
@pytest.mark.parametrize("interruption", ["expiry", "replacement"])
async def test_worker_error_handlers_respect_rejected_publication(
    store: Store,
    retryable: bool,
    interruption: str,
) -> None:
    before = await store.repository.get(store.document.id)
    await store.repository.release_lock(store.document.id, OWNER)
    queue = OneMessageQueue(store.document.id)
    worker = DocumentWorker(
        queue=queue,
        repository=store.repository,
        processor=InterruptedProcessor(store, interruption, retryable),
        consumer_name="test-worker",
        max_attempts=1,
        retry_base_seconds=0,
        retry_jitter_seconds=0,
        reclaim_idle_ms=1000,
        read_block_ms=1,
        batch_size=1,
        lock_ttl_seconds=30,
        random=lambda: 0,
        token_factory=lambda: OWNER,
        clock=lambda: NOW,
    )

    await worker.run_once()

    assert queue.acknowledged == (["1-0"] if interruption == "expiry" else [])
    assert await store.repository.get(store.document.id) == (
        None if interruption == "expiry" else before
    )
    assert await store.repository.get_source(store.document.id) == store.document.content
