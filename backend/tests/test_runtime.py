from datetime import UTC, datetime
from typing import Never, cast
from uuid import uuid4

import fakeredis.aioredis
import httpx
import pytest
from pydantic import SecretStr, ValidationError
from redis.asyncio import Redis

from document_processor.config import Settings
from document_processor.domain.models import DocumentStatus, NewDocument, ParserKind
from document_processor.infrastructure.redis_gateway import RedisSubmissionGateway
from document_processor.infrastructure.redis_repository import RedisDocumentRepository
from document_processor.runtime import RedisHealthProbe, build_worker, create_runtime_app
from document_processor.worker import DocumentWorker


class FakeRedis:
    async def ping(self) -> bool:
        return True

    async def hgetall(self, _name: str) -> dict[str, str]:
        return {}


@pytest.mark.asyncio
async def test_runtime_app_uses_redis_dependencies() -> None:
    redis = cast(Redis, FakeRedis())
    app = create_runtime_app(
        settings=Settings(gemini_api_key=SecretStr("test-key")),
        redis=redis,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        readiness = await client.get("/health/ready")
        missing = await client.get(f"/api/v1/documents/{uuid4()}")

    assert readiness.status_code == 200
    assert readiness.json() == {
        "status": "ok",
        "checks": {"redis": "available", "gemini": "configured"},
    }
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {
            "code": "document_not_found",
            "message": "Document not found or expired.",
        }
    }


@pytest.mark.asyncio
async def test_redis_health_probe_reports_connectivity() -> None:
    probe = RedisHealthProbe(cast(Redis, FakeRedis()))

    assert await probe.ping() is True


def test_worker_requires_gemini_without_exposing_configuration() -> None:
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is required") as error:
        build_worker(
            settings=Settings(),
            redis=cast(Redis, FakeRedis()),
            consumer_name="worker-1",
        )

    assert "test-key" not in str(error.value)


@pytest.mark.asyncio
async def test_runtime_app_is_ready_with_explicit_smoke_summarizer() -> None:
    app = create_runtime_app(
        settings=Settings(test_summarizer_enabled=True),
        redis=cast(Redis, FakeRedis()),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        readiness = await client.get("/health/ready")

    assert readiness.status_code == 200
    assert readiness.json()["checks"]["gemini"] == "test-only"


def test_worker_uses_explicit_smoke_summarizer_without_gemini() -> None:
    worker = build_worker(
        settings=Settings(test_summarizer_enabled=True),
        redis=cast(Redis, FakeRedis()),
        consumer_name="worker-1",
    )

    assert isinstance(worker, DocumentWorker)


def test_explicit_smoke_summarizer_overrides_configured_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_gemini_summarizer(**_kwargs: object) -> Never:
        raise AssertionError("Gemini summarizer must be disabled in smoke mode")

    monkeypatch.setattr(
        "document_processor.runtime.GeminiSummarizer",
        reject_gemini_summarizer,
    )

    worker = build_worker(
        settings=Settings(
            gemini_api_key=SecretStr("test-key"),
            test_summarizer_enabled=True,
        ),
        redis=cast(Redis, FakeRedis()),
        consumer_name="worker-1",
    )

    assert isinstance(worker, DocumentWorker)


def test_runtime_settings_do_not_expose_the_retired_provider() -> None:
    assert "mistral_api_key" not in Settings.model_fields
    assert "mistral_ocr_model" not in Settings.model_fields
    assert not hasattr(Settings, "mistral_available")


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size,expected", [(None, 1), (4, 2)])
async def test_runtime_worker_fetches_one_by_default_but_keeps_batch_override(
    batch_size: int | None,
    expected: int,
) -> None:
    settings = (
        Settings(test_summarizer_enabled=True, worker_read_block_ms=1)
        if batch_size is None
        else Settings(
            test_summarizer_enabled=True, worker_read_block_ms=1, worker_batch_size=batch_size
        )
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    documents = [
        NewDocument(
            id=uuid4(),
            filename=f"synthetic-{index}.pdf",
            parser=ParserKind.PYPDF,
            status=DocumentStatus.QUEUED,
            content=b"%PDF-1.7\ncorrupt",
            created_at=datetime.now(UTC),
        )
        for index in range(2)
    ]
    try:
        await RedisSubmissionGateway(redis, ttl_seconds=60).create_batch(documents)
        instance = build_worker(settings=settings, redis=redis, consumer_name="batch-test")

        assert await instance.run_once() == expected

        repository = RedisDocumentRepository(redis, ttl_seconds=60)
        records = [await repository.get(document.id) for document in documents]
        assert (
            sum(record is not None and record.status is DocumentStatus.FAILED for record in records)
            == expected
        )
        assert (
            sum(record is not None and record.status is DocumentStatus.QUEUED for record in records)
            == 2 - expected
        )
    finally:
        await redis.aclose()


def test_worker_concurrency_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(worker_concurrency=0)
