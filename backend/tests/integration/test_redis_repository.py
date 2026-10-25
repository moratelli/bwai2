"""Integration tests for temporary Redis document persistence."""

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import fakeredis.aioredis
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from document_processor.domain.errors import InfrastructureUnavailableError
from document_processor.domain.models import (
    DocumentStatus,
    NewDocument,
    PageContent,
    ParserKind,
    StateTransition,
)
from document_processor.infrastructure.redis_repository import RedisDocumentRepository

DOCUMENT_ID = UUID("00000000-0000-4000-8000-000000000001")
CREATED_AT = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 10, 23, 12, 1, tzinfo=UTC)
OWNER = "repository-test-owner"


def queued_document() -> NewDocument:
    return NewDocument(
        id=DOCUMENT_ID,
        filename="sample.pdf",
        parser=ParserKind.PYPDF,
        status=DocumentStatus.QUEUED,
        content=b"%PDF-1.7\ncontent",
        created_at=CREATED_AT,
    )


class FailingDocumentRedis:
    async def hgetall(self, name: str) -> Mapping[bytes | str, bytes | str]:
        del name
        raise RedisConnectionError("sensitive Redis connection details")


@asynccontextmanager
async def repository() -> AsyncGenerator[tuple[RedisDocumentRepository, Redis]]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    repo = RedisDocumentRepository(redis, ttl_seconds=86_400)
    try:
        yield repo, redis
    finally:
        await redis.aclose()


async def begin_processing(repo: RedisDocumentRepository) -> None:
    assert await repo.acquire_lock(DOCUMENT_ID, OWNER, ttl_seconds=30)
    assert (
        await repo.mark_processing(
            DOCUMENT_ID,
            token=OWNER,
            attempt=1,
            updated_at=COMPLETED_AT,
        )
        is StateTransition.APPLIED
    )


@pytest.mark.asyncio
async def test_translates_native_redis_failure_to_safe_infrastructure_error() -> None:
    repo = RedisDocumentRepository(
        cast(Redis, FailingDocumentRedis()),
        ttl_seconds=86_400,
    )

    with pytest.raises(InfrastructureUnavailableError) as caught:
        await repo.get(DOCUMENT_ID)

    assert caught.value.code == "infrastructure_unavailable"
    assert str(caught.value) == "Document store is temporarily unavailable."
    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_saves_queued_metadata_source_and_retention() -> None:
    async with repository() as (repo, redis):
        await repo.save_queued([queued_document()])

        stored = await repo.get(DOCUMENT_ID)
        source = await repo.get_source(DOCUMENT_ID)
        metadata_ttl = await redis.ttl(repo.document_key(DOCUMENT_ID))
        source_ttl = await redis.ttl(repo.source_key(DOCUMENT_ID))

    assert stored is not None
    assert stored.id == DOCUMENT_ID
    assert stored.filename == "sample.pdf"
    assert stored.parser is ParserKind.PYPDF
    assert stored.status is DocumentStatus.QUEUED
    assert stored.created_at == CREATED_AT
    assert stored.updated_at == CREATED_AT
    assert stored.attempts == 0
    assert stored.pages is None
    assert stored.summary is None
    assert source == b"%PDF-1.7\ncontent"
    assert 0 < metadata_ttl <= 86_400
    assert 0 < source_ttl <= 86_400


@pytest.mark.asyncio
async def test_returns_none_for_unknown_document_and_source() -> None:
    async with repository() as (repo, _redis):
        assert await repo.get(DOCUMENT_ID) is None
        assert await repo.get_source(DOCUMENT_ID) is None


@pytest.mark.asyncio
async def test_marks_document_processing_with_attempt_count() -> None:
    async with repository() as (repo, _redis):
        await repo.save_queued([queued_document()])

        assert await repo.acquire_lock(DOCUMENT_ID, OWNER, ttl_seconds=30)
        changed = await repo.mark_processing(
            DOCUMENT_ID,
            token=OWNER,
            attempt=2,
            updated_at=COMPLETED_AT,
        )
        stored = await repo.get(DOCUMENT_ID)

    assert changed is StateTransition.APPLIED
    assert stored is not None
    assert stored.status is DocumentStatus.PROCESSING
    assert stored.attempts == 2
    assert stored.updated_at == COMPLETED_AT


@pytest.mark.asyncio
async def test_completes_document_and_deletes_source_atomically() -> None:
    pages = (PageContent(index=0, markdown="# First"), PageContent(index=1, markdown="Second"))
    async with repository() as (repo, redis):
        await repo.save_queued([queued_document()])
        await begin_processing(repo)
        ttl_before = await redis.ttl(repo.document_key(DOCUMENT_ID))

        await repo.complete(
            DOCUMENT_ID,
            token=OWNER,
            pages=pages,
            summary="## Summary",
            completed_at=COMPLETED_AT,
            attempts=1,
        )
        stored = await repo.get(DOCUMENT_ID)
        source = await repo.get_source(DOCUMENT_ID)
        ttl_after = await redis.ttl(repo.document_key(DOCUMENT_ID))

    assert stored is not None
    assert stored.status is DocumentStatus.COMPLETED
    assert stored.pages == pages
    assert stored.summary == "## Summary"
    assert stored.error_code is None
    assert stored.error_message is None
    assert stored.completed_at == COMPLETED_AT
    assert stored.attempts == 1
    assert source is None
    assert 0 < ttl_after <= ttl_before


@pytest.mark.asyncio
async def test_fails_document_and_deletes_source_atomically() -> None:
    async with repository() as (repo, _redis):
        await repo.save_queued([queued_document()])

        await begin_processing(repo)
        await repo.fail(
            DOCUMENT_ID,
            token=OWNER,
            error_code="invalid_pdf",
            error_message="The PDF is corrupt or encrypted.",
            completed_at=COMPLETED_AT,
            attempts=1,
        )
        stored = await repo.get(DOCUMENT_ID)
        source = await repo.get_source(DOCUMENT_ID)

    assert stored is not None
    assert stored.status is DocumentStatus.FAILED
    assert stored.error_code == "invalid_pdf"
    assert stored.error_message == "The PDF is corrupt or encrypted."
    assert stored.pages is None
    assert stored.summary is None
    assert source is None


@pytest.mark.asyncio
async def test_lock_requires_owner_token_to_release() -> None:
    async with repository() as (repo, _redis):
        assert await repo.acquire_lock(DOCUMENT_ID, "worker-one", ttl_seconds=30) is True
        assert await repo.acquire_lock(DOCUMENT_ID, "worker-two", ttl_seconds=30) is False
        assert await repo.release_lock(DOCUMENT_ID, "worker-two") is False
        assert await repo.acquire_lock(DOCUMENT_ID, "worker-two", ttl_seconds=30) is False
        assert await repo.release_lock(DOCUMENT_ID, "worker-one") is True
        assert await repo.acquire_lock(DOCUMENT_ID, "worker-two", ttl_seconds=30) is True
