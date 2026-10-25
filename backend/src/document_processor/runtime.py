"""Production dependency wiring for the API and worker processes."""

from random import random

from fastapi import FastAPI
from redis.asyncio import Redis

from document_processor.api.app import create_app
from document_processor.config import Settings
from document_processor.domain.models import ParserKind
from document_processor.infrastructure.gemini_rate_gate import RedisGeminiRateGate
from document_processor.infrastructure.parsers.gemini_parser import GeminiPdfParser
from document_processor.infrastructure.parsers.pypdf_parser import PyPdfParser
from document_processor.infrastructure.redis_gateway import RedisSubmissionGateway
from document_processor.infrastructure.redis_repository import RedisDocumentRepository
from document_processor.infrastructure.redis_streams import RedisStreamQueue
from document_processor.infrastructure.summarizers.gemini_summarizer import GeminiSummarizer
from document_processor.infrastructure.summarizers.smoke_test_summarizer import (
    SmokeTestSummarizer,
)
from document_processor.ports import DocumentParser, DocumentSummarizer, ParserRegistry
from document_processor.services.processing import ProcessingService
from document_processor.worker import DocumentWorker


class RedisHealthProbe:
    """Report whether the configured Redis server responds."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def ping(self) -> bool:
        result: object = await self._redis.ping()  # pyright: ignore[reportUnknownMemberType]
        return bool(result)


def create_redis(settings: Settings) -> Redis:
    """Create a lazy async Redis client from runtime settings."""
    client: Redis = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis_url
    )
    return client


def create_runtime_app(
    *,
    settings: Settings | None = None,
    redis: Redis | None = None,
) -> FastAPI:
    """Create the API with concrete Redis-backed dependencies."""
    app_settings = settings or Settings()
    client = redis or create_redis(app_settings)
    repository = RedisDocumentRepository(client)
    gateway = RedisSubmissionGateway(
        client,
        ttl_seconds=app_settings.document_ttl_seconds,
        stream_name=app_settings.stream_name,
    )
    return create_app(
        settings=app_settings,
        submission_gateway=gateway,
        document_reader=repository,
        health_probe=RedisHealthProbe(client),
    )


def build_worker(
    *,
    settings: Settings,
    redis: Redis,
    consumer_name: str,
) -> DocumentWorker:
    """Assemble one worker without connecting to providers at import time."""
    if settings.gemini_api_key is None and not settings.test_summarizer_enabled:
        raise RuntimeError("GEMINI_API_KEY is required by the document worker")

    provider_gate = RedisGeminiRateGate(
        redis,
        scope=settings.gemini_rate_limit_scope,
        model=settings.gemini_model,
        requests_per_minute=settings.gemini_requests_per_minute,
    )
    parsers: dict[ParserKind, DocumentParser] = {
        ParserKind.PYPDF: PyPdfParser(),
    }
    gemini_key = (
        settings.gemini_api_key.get_secret_value() if settings.gemini_api_key is not None else None
    )
    if gemini_key is not None:
        parsers[ParserKind.GEMINI] = GeminiPdfParser(
            model=settings.gemini_model,
            api_key=gemini_key,
            request_gate=provider_gate,
        )

    summarizer: DocumentSummarizer
    if settings.test_summarizer_enabled:
        summarizer = SmokeTestSummarizer()
    elif gemini_key is not None:
        summarizer = GeminiSummarizer(
            model=settings.gemini_model,
            chunk_size=settings.summary_chunk_characters,
            api_key=gemini_key,
            request_gate=provider_gate,
        )
    else:
        raise RuntimeError("GEMINI_API_KEY is required by the document worker")

    parser_registry: ParserRegistry = parsers
    repository = RedisDocumentRepository(redis)
    queue = RedisStreamQueue(
        redis,
        stream_name=settings.stream_name,
        group_name=settings.consumer_group,
    )
    processor = ProcessingService(
        repository=repository,
        parsers=parser_registry,
        summarizer=summarizer,
    )
    return DocumentWorker(
        queue=queue,
        repository=repository,
        processor=processor,
        consumer_name=consumer_name,
        provider_gate=provider_gate,
        max_attempts=settings.worker_max_attempts,
        retry_base_seconds=settings.worker_retry_base_seconds,
        retry_jitter_seconds=settings.worker_retry_jitter_seconds,
        reclaim_idle_ms=settings.worker_reclaim_idle_ms,
        read_block_ms=settings.worker_read_block_ms,
        batch_size=settings.worker_batch_size,
        lock_ttl_seconds=settings.worker_lock_ttl_seconds,
        random=random,
    )


app = create_runtime_app()
