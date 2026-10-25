"""Durable retry metadata uses the same owner, attempt and expiry fences."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline
from redis.exceptions import ConnectionError as RedisConnectionError

from document_processor.domain import models, provider
from document_processor.domain.errors import InfrastructureUnavailableError
from document_processor.domain.models import (
    DocumentStatus,
    NewDocument,
    PageContent,
    ParserKind,
    StateTransition,
)
from document_processor.domain.provider import ProviderFailure
from document_processor.infrastructure.redis_gateway import RedisSubmissionGateway
from document_processor.infrastructure.redis_repository import (
    AsyncRedisClient,
    RedisDocumentRepository,
)

OWNER = "test-owner"
NOW = datetime(2026, 10, 25, 18, tzinfo=UTC)
UNTIL = NOW + timedelta(seconds=60)
ENCODED_FAILURE = json.dumps(
    {
        "attempt": 1,
        "provider_info": {
            "provider": "gemini",
            "category": "rate_limited",
            "stage": "summarization",
            "http_status": 429,
        },
    }
)


@dataclass
class Store:
    redis: Redis
    repo: RedisDocumentRepository
    document: NewDocument
    server_now: datetime = NOW

    @property
    def raw(self) -> AsyncRedisClient:
        return cast(AsyncRedisClient, self.redis)

    @property
    def key(self) -> str:
        return self.repo.document_key(self.document.id)


@pytest.fixture
async def store(queue_redis: Redis, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Store]:
    document = NewDocument(
        uuid4(), "synthetic.pdf", ParserKind.PYPDF, DocumentStatus.QUEUED, b"%PDF-test", NOW
    )
    repo = RedisDocumentRepository(queue_redis)
    value = Store(queue_redis, repo, document)
    stream = "test-retry:" + str(document.id)

    async def time(self: Pipeline) -> tuple[int, int]:
        stamp = value.server_now.timestamp()
        return int(stamp), int((stamp % 1) * 1000000)

    monkeypatch.setattr(Pipeline, "time", time)
    try:
        await RedisSubmissionGateway(queue_redis, ttl_seconds=120, stream_name=stream).create_batch(
            [document]
        )
        assert await repo.acquire_lock(document.id, OWNER, ttl_seconds=120)
        assert (
            await repo.admit_attempt(document.id, token=OWNER, max_attempts=3, updated_at=NOW) == 1
        )
        yield value
    finally:
        await queue_redis.delete(
            stream, value.key, repo.source_key(document.id), repo.lock_key(document.id)
        )


async def test_future_deadline_blocks_admission_using_server_not_worker_time(store: Store) -> None:
    await store.raw.hset(
        store.key, mapping={"last_failure": ENCODED_FAILURE, "retry_not_before": UNTIL.isoformat()}
    )
    result = await store.repo.admit_attempt(
        store.document.id, token=OWNER, max_attempts=3, updated_at=NOW + timedelta(days=365)
    )
    record = await store.repo.get(store.document.id)
    assert record is not None and record.attempts == 1
    assert isinstance(result, models.RetryWaiting)
    assert result.attempts == 1 and result.schedule.delay_seconds == 60
    assert result.schedule.not_before == UNTIL
    store.server_now = UNTIL
    assert (
        await store.repo.admit_attempt(
            store.document.id, token=OWNER, max_attempts=3, updated_at=UNTIL
        )
        == 2
    )
    record = await store.repo.get(store.document.id)
    assert record is not None and record.retry_not_before is None
    assert record.last_failure is not None and record.last_failure.attempt == 1


async def test_record_failure_preserves_source_and_retention(store: Store) -> None:
    failure = provider.AttemptFailure(1, ProviderFailure("rate_limited", "summarization", 429))
    ttl = await store.redis.pttl(store.key)
    assert (
        await store.repo.record_retry_failure(
            store.document.id,
            token=OWNER,
            failure=failure,
            retry_not_before=UNTIL,
            updated_at=NOW,
        )
        is StateTransition.APPLIED
    )
    record = await store.repo.get(store.document.id)
    assert record is not None and record.last_failure == failure
    assert record.retry_not_before == UNTIL and record.attempts == 1
    assert record.status is DocumentStatus.PROCESSING
    assert await store.repo.get_source(store.document.id) == b"%PDF-test"
    assert 0 < await store.redis.pttl(store.key) <= ttl


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("owner", StateTransition.LOST_OWNERSHIP),
        ("attempt", StateTransition.LOST_OWNERSHIP),
        ("absent", StateTransition.NOT_FOUND),
        ("terminal", StateTransition.ALREADY_TERMINAL),
    ],
)
async def test_stale_retry_writes_cannot_change_state(
    store: Store, mutation: str, expected: StateTransition
) -> None:
    if mutation == "owner":
        await store.raw.set(store.repo.lock_key(store.document.id), "replacement", ex=120, nx=False)
    elif mutation == "attempt":
        await store.raw.hset(store.key, mapping={"attempts": "2"})
    elif mutation == "absent":
        await store.redis.delete(store.key)
    else:
        await store.repo.complete(
            store.document.id,
            token=OWNER,
            pages=[PageContent(0, "ok")],
            summary="ok",
            completed_at=NOW,
            attempts=1,
        )
    before = await store.raw.hgetall(store.key)
    result = await store.repo.record_retry_failure(
        store.document.id,
        token=OWNER,
        failure=provider.AttemptFailure(1, None),
        retry_not_before=UNTIL,
        updated_at=NOW,
    )
    assert result is expected
    assert await store.raw.hgetall(store.key) == before


async def test_infrastructure_context_replaces_provider_context_and_exhaustion_keeps_it(
    store: Store,
) -> None:
    await store.raw.hset(store.key, mapping={"last_failure": ENCODED_FAILURE, "attempts": 3})
    failure = provider.AttemptFailure(3, None)
    await store.repo.record_retry_failure(
        store.document.id, token=OWNER, failure=failure, retry_not_before=None, updated_at=NOW
    )
    admission = await store.repo.admit_attempt(
        store.document.id, token=OWNER, max_attempts=3, updated_at=NOW
    )
    assert isinstance(admission, models.AttemptsExhausted)
    assert admission.last_failure == failure
    await store.repo.fail(
        store.document.id,
        token=OWNER,
        error_code="retry_exhausted",
        error_message="Safe failure.",
        completed_at=NOW,
        attempts=3,
    )
    record = await store.repo.get(store.document.id)
    assert record is not None and record.last_failure == failure
    assert record.retry_not_before is None


@pytest.mark.parametrize("terminal", ["completed", "permanent"])
async def test_terminal_results_clear_unrelated_retry_metadata(store: Store, terminal: str) -> None:
    await store.raw.hset(
        store.key, mapping={"last_failure": ENCODED_FAILURE, "retry_not_before": UNTIL.isoformat()}
    )
    if terminal == "completed":
        await store.repo.complete(
            store.document.id,
            token=OWNER,
            pages=[PageContent(0, "ok")],
            summary="ok",
            completed_at=NOW,
            attempts=1,
        )
    else:
        await store.repo.fail(
            store.document.id,
            token=OWNER,
            error_code="invalid_pdf",
            error_message="Invalid PDF.",
            completed_at=NOW,
            attempts=1,
        )
    record = await store.repo.get(store.document.id)
    assert record is not None and record.last_failure is None and record.retry_not_before is None
    assert await store.repo.get_source(store.document.id) is None


@pytest.mark.parametrize(
    "encoded",
    [
        "broken",
        "null",
        "[]",
        "{}",
        pytest.param("[" * 3000, id="oversized"),
        b"\xff",
        '{"attempt":true,"provider_info":null}',
        '{"attempt":2,"provider_info":null}',
        ENCODED_FAILURE.replace('"rate_limited"', '"unknown"'),
        ENCODED_FAILURE.replace('"summarization"', '"unknown"'),
        ENCODED_FAILURE.replace("429", "503"),
    ],
)
async def test_invalid_optional_context_does_not_break_legacy_record(
    store: Store, encoded: str | bytes
) -> None:
    await store.raw.hset(
        store.key, mapping={"last_failure": encoded, "retry_not_before": "invalid"}
    )
    record = await store.repo.get(store.document.id)
    assert record is not None and record.id == store.document.id
    assert record.last_failure is None and record.retry_not_before is None


@pytest.mark.parametrize(
    "category,status",
    [
        ("unavailable", 503),
        ("unavailable", 500),
        ("temporary_failure", 408),
        ("temporary_failure", 409),
        ("timeout", None),
        ("connection_error", None),
    ],
)
async def test_safe_provider_context_roundtrips(
    store: Store,
    category: provider.ProviderCategory,
    status: int | None,
) -> None:
    failure = provider.AttemptFailure(1, ProviderFailure(category, "extraction", status))
    assert (
        await store.repo.record_retry_failure(
            store.document.id,
            token=OWNER,
            failure=failure,
            retry_not_before=UNTIL,
            updated_at=NOW,
        )
        is StateTransition.APPLIED
    )
    record = await store.repo.get(store.document.id)
    assert record is not None and record.last_failure == failure


@pytest.mark.parametrize("expire", [False, True])
async def test_retry_transaction_rechecks_owner_and_absence_after_watch_conflict(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    expire: bool,
) -> None:
    execute = Pipeline.execute
    interrupted = False

    async def raced(self: Pipeline, raise_on_error: bool = True) -> list[object]:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            if expire:
                await store.redis.delete(store.key)
            else:
                await store.raw.set(
                    store.repo.lock_key(store.document.id), "replacement", ex=120, nx=False
                )
        return cast(list[object], await execute(self, raise_on_error=raise_on_error))

    monkeypatch.setattr(Pipeline, "execute", raced)
    outcome = await store.repo.record_retry_failure(
        store.document.id,
        token=OWNER,
        failure=provider.AttemptFailure(1, None),
        retry_not_before=UNTIL,
        updated_at=NOW,
    )
    assert outcome is (StateTransition.NOT_FOUND if expire else StateTransition.LOST_OWNERSHIP)
    raw = await store.raw.hgetall(store.key)
    assert b"last_failure" not in raw and b"retry_not_before" not in raw
    assert await store.repo.get_source(store.document.id) == b"%PDF-test"


async def test_absent_optional_context_is_compatible(store: Store) -> None:
    record = await store.repo.get(store.document.id)
    assert record is not None and record.last_failure is None and record.retry_not_before is None


@pytest.mark.parametrize("after_commit", [False, True])
async def test_uncertain_retry_write_does_not_claim_success(
    store: Store, monkeypatch: pytest.MonkeyPatch, after_commit: bool
) -> None:
    execute = Pipeline.execute

    async def interrupted(self: Pipeline, raise_on_error: bool = True) -> list[object]:
        if after_commit:
            await execute(self, raise_on_error=raise_on_error)
        raise RedisConnectionError("PRIVATE_REDIS_CANARY")

    with monkeypatch.context() as patch:
        patch.setattr(Pipeline, "execute", interrupted)
        with pytest.raises(InfrastructureUnavailableError):
            await store.repo.record_retry_failure(
                store.document.id,
                token=OWNER,
                failure=provider.AttemptFailure(1, None),
                retry_not_before=UNTIL,
                updated_at=NOW,
            )
    record = await store.repo.get(store.document.id)
    assert record is not None and record.attempts == 1
    assert (record.retry_not_before == UNTIL) is after_commit
    assert await store.repo.get_source(store.document.id) == b"%PDF-test"


async def test_expiry_cannot_be_reversed_by_retry_recording(store: Store) -> None:
    await store.redis.pexpire(store.key, 1)
    await asyncio.sleep(0.02)
    assert (
        await store.repo.record_retry_failure(
            store.document.id,
            token=OWNER,
            failure=provider.AttemptFailure(1, None),
            retry_not_before=UNTIL,
            updated_at=NOW,
        )
        is StateTransition.NOT_FOUND
    )
    assert await store.repo.get(store.document.id) is None
