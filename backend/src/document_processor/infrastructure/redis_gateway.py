"""Atomic Redis submission gateway."""

from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Protocol, Self, cast

from redis.asyncio import Redis

from document_processor.domain.models import NewDocument
from document_processor.infrastructure.redis_repository import RedisDocumentRepository, RedisValue


class SubmissionPipeline(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def hset(self, name: str, *, mapping: Mapping[str, RedisValue]) -> Self: ...

    def expire(self, name: str, ttl_seconds: int) -> Self: ...

    def set(self, name: str, value: RedisValue, *, ex: int) -> Self: ...

    def xadd(self, name: str, fields: Mapping[str, str]) -> Self: ...

    async def execute(self) -> list[object]: ...


class SubmissionRedis(Protocol):
    def pipeline(self, *, transaction: bool = True) -> SubmissionPipeline: ...


class RedisSubmissionGateway:
    """Persist source documents and append stream work in one Redis transaction."""

    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int,
        stream_name: str = "documents:processing",
    ) -> None:
        self._redis = cast(SubmissionRedis, redis)
        self._ttl_seconds = ttl_seconds
        self._stream_name = stream_name

    async def create_batch(self, documents: Sequence[NewDocument]) -> None:
        async with self._redis.pipeline(transaction=True) as pipeline:
            for document in documents:
                document_key = RedisDocumentRepository.document_key(document.id)
                pipeline.hset(
                    document_key,
                    mapping={
                        "id": str(document.id),
                        "filename": document.filename,
                        "parser": document.parser.value,
                        "status": document.status.value,
                        "created_at": document.created_at.isoformat(),
                        "updated_at": document.created_at.isoformat(),
                        "attempts": "0",
                    },
                )
                pipeline.expire(document_key, self._ttl_seconds)
                pipeline.set(
                    RedisDocumentRepository.source_key(document.id),
                    document.content,
                    ex=self._ttl_seconds,
                )
                pipeline.xadd(
                    self._stream_name,
                    {"document_id": str(document.id)},
                )
            await pipeline.execute()
