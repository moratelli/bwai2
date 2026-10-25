"""Real Redis integration test for atomic submission publication."""

import os
from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self, cast
from urllib.parse import urlparse
from uuid import UUID

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from document_processor.config import Settings
from document_processor.domain.errors import InfrastructureUnavailableError
from document_processor.domain.models import (
    DocumentStatus,
    NewDocument,
    ParserKind,
    UploadCandidate,
)
from document_processor.infrastructure.redis_gateway import RedisSubmissionGateway
from document_processor.infrastructure.redis_repository import RedisDocumentRepository, RedisValue
from document_processor.infrastructure.redis_streams import RedisStreamQueue
from document_processor.services.submission import SubmissionService


class RedisLifecycle(Protocol):
    async def ping(self) -> bool: ...

    async def flushdb(self) -> bool: ...

    async def aclose(self) -> None: ...


def redis_from_url(url: str) -> Redis:
    parsed = urlparse(url)
    database = int(parsed.path.removeprefix("/") or "0")
    return Redis(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        db=database,
        password=parsed.password,
        decode_responses=False,
    )


class FailingSubmissionPipeline:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def hset(self, name: str, *, mapping: Mapping[str, RedisValue]) -> Self:
        del name, mapping
        return self

    def expire(self, name: str, ttl_seconds: int) -> Self:
        del name, ttl_seconds
        return self

    def set(self, name: str, value: RedisValue, *, ex: int) -> Self:
        del name, value, ex
        return self

    def xadd(self, name: str, fields: Mapping[str, str]) -> Self:
        del name, fields
        return self

    async def execute(self) -> list[object]:
        raise RedisConnectionError("sensitive Redis connection details")


class FailingSubmissionRedis:
    def pipeline(self, *, transaction: bool = True) -> FailingSubmissionPipeline:
        del transaction
        return FailingSubmissionPipeline()


@pytest.fixture
async def redis_client() -> AsyncGenerator[Redis]:
    url = os.getenv("TEST_REDIS_URL")
    if url is None:
        pytest.skip("Set TEST_REDIS_URL to run the Redis 7 integration test.")
    redis = redis_from_url(url)
    lifecycle = cast(RedisLifecycle, redis)
    try:
        await lifecycle.ping()
    except RedisConnectionError:
        await lifecycle.aclose()
        pytest.fail("TEST_REDIS_URL is configured but its disposable Redis is unreachable.")
    await lifecycle.flushdb()
    try:
        yield redis
    finally:
        await lifecycle.flushdb()
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_submission_translates_native_redis_failure_to_safe_error() -> None:
    gateway = RedisSubmissionGateway(
        cast(Redis, FailingSubmissionRedis()),
        ttl_seconds=86_400,
    )
    document = NewDocument(
        id=UUID("00000000-0000-4000-8000-000000000001"),
        filename="one.pdf",
        parser=ParserKind.PYPDF,
        status=DocumentStatus.QUEUED,
        content=b"%PDF-1.7\none",
        created_at=datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
    )

    with pytest.raises(InfrastructureUnavailableError) as caught:
        await gateway.create_batch([document])

    assert caught.value.code == "infrastructure_unavailable"
    assert str(caught.value) == "Document queue is temporarily unavailable."
    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_submission_persists_documents_and_stream_entries_atomically(
    redis_client: Redis,
) -> None:
    repository = RedisDocumentRepository(redis_client, ttl_seconds=86_400)
    queue = RedisStreamQueue(
        redis_client,
        stream_name="integration:documents",
        group_name="integration-workers",
    )
    gateway = RedisSubmissionGateway(
        redis_client,
        ttl_seconds=86_400,
        stream_name="integration:documents",
    )
    identifiers = iter(
        [
            UUID("00000000-0000-4000-8000-000000000001"),
            UUID("00000000-0000-4000-8000-000000000002"),
        ]
    )
    service = SubmissionService(
        gateway=gateway,
        settings=Settings(gemini_api_key=SecretStr("test-key")),
        id_factory=lambda: next(identifiers),
        clock=lambda: datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
    )

    accepted = await service.submit(
        [
            UploadCandidate("one.pdf", "application/pdf", b"%PDF-1.7\none"),
            UploadCandidate("two.pdf", "application/pdf", b"%PDF-1.7\ntwo"),
        ],
        ParserKind.PYPDF,
    )
    await queue.ensure_group()
    messages = await queue.read_new("integration-worker", count=10, block_ms=10)
    stored = [await repository.get(document.id) for document in accepted]

    assert [document.status if document else None for document in stored] == [
        DocumentStatus.QUEUED,
        DocumentStatus.QUEUED,
    ]
    assert [message.document_id for message in messages] == [document.id for document in accepted]
    assert [await repository.get_source(document.id) for document in accepted] == [
        b"%PDF-1.7\none",
        b"%PDF-1.7\ntwo",
    ]
