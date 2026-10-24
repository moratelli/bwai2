"""Redis-backed temporary document storage."""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self, cast
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import WatchError

from document_processor.domain.models import (
    DocumentRecord,
    DocumentStatus,
    NewDocument,
    PageContent,
    ParserKind,
)
from document_processor.infrastructure.redis_errors import redis_error_boundary

type RedisValue = bytes | str | int | float


class AsyncRedisPipeline(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def hset(self, name: str, *, mapping: Mapping[str, RedisValue]) -> Self: ...

    def hdel(self, name: str, *keys: str) -> Self: ...

    def expire(self, name: str, ttl_seconds: int) -> Self: ...

    def set(self, name: str, value: RedisValue, *, ex: int) -> Self: ...

    def delete(self, *names: str) -> Self: ...

    async def execute(self) -> list[object]: ...

    async def watch(self, *names: str) -> None: ...

    async def unwatch(self) -> None: ...

    async def get(self, name: str) -> bytes | str | None: ...

    def multi(self) -> None: ...


class AsyncRedisClient(Protocol):
    def pipeline(self, *, transaction: bool = True) -> AsyncRedisPipeline: ...

    async def hgetall(self, name: str) -> Mapping[bytes | str, bytes | str]: ...

    async def get(self, name: str) -> bytes | str | None: ...

    async def exists(self, name: str) -> int: ...

    async def hset(self, name: str, *, mapping: Mapping[str, RedisValue]) -> int: ...

    async def set(
        self,
        name: str,
        value: RedisValue,
        *,
        ex: int,
        nx: bool,
    ) -> bool | None: ...


class RedisDocumentRepository:
    """Persist document state and transient PDF bytes with bounded retention."""

    def __init__(self, redis: Redis, *, ttl_seconds: int) -> None:
        self._redis = cast(AsyncRedisClient, redis)
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def document_key(document_id: UUID) -> str:
        return f"document:{document_id}"

    @staticmethod
    def source_key(document_id: UUID) -> str:
        return f"document:{document_id}:source"

    @staticmethod
    def lock_key(document_id: UUID) -> str:
        return f"document:{document_id}:lock"

    async def save_queued(self, documents: Sequence[NewDocument]) -> None:
        with redis_error_boundary("Document store"):
            async with self._redis.pipeline(transaction=True) as pipeline:
                for document in documents:
                    key = self.document_key(document.id)
                    pipeline.hset(
                        key,
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
                    pipeline.expire(key, self._ttl_seconds)
                    pipeline.set(
                        self.source_key(document.id),
                        document.content,
                        ex=self._ttl_seconds,
                    )
                await pipeline.execute()

    async def get(self, document_id: UUID) -> DocumentRecord | None:
        with redis_error_boundary("Document store"):
            raw = await self._redis.hgetall(self.document_key(document_id))
        if not raw:
            return None
        values = self._decode_mapping(raw)
        pages = self._decode_pages(values["pages"]) if "pages" in values else None
        completed_at = values.get("completed_at")
        return DocumentRecord(
            id=UUID(values["id"]),
            filename=values["filename"],
            parser=ParserKind(values["parser"]),
            status=DocumentStatus(values["status"]),
            created_at=datetime.fromisoformat(values["created_at"]),
            updated_at=datetime.fromisoformat(values["updated_at"]),
            attempts=int(values["attempts"]),
            pages=pages,
            summary=values.get("summary"),
            error_code=values.get("error_code"),
            error_message=values.get("error_message"),
            completed_at=datetime.fromisoformat(completed_at) if completed_at else None,
        )

    async def get_source(self, document_id: UUID) -> bytes | None:
        with redis_error_boundary("Document store"):
            source = await self._redis.get(self.source_key(document_id))
        if source is None:
            return None
        if isinstance(source, bytes):
            return source
        return source.encode()

    async def mark_processing(
        self,
        document_id: UUID,
        *,
        attempt: int,
        updated_at: datetime,
    ) -> bool:
        key = self.document_key(document_id)
        with redis_error_boundary("Document store"):
            if not await self._redis.exists(key):
                return False
            await self._redis.hset(
                key,
                mapping={
                    "status": DocumentStatus.PROCESSING.value,
                    "attempts": str(attempt),
                    "updated_at": updated_at.isoformat(),
                },
            )
        return True

    async def complete(
        self,
        document_id: UUID,
        *,
        pages: Sequence[PageContent],
        summary: str,
        completed_at: datetime,
        attempts: int,
    ) -> None:
        encoded_pages = json.dumps(
            [{"index": page.index, "markdown": page.markdown} for page in pages],
            separators=(",", ":"),
        )
        with redis_error_boundary("Document store"):
            async with self._redis.pipeline(transaction=True) as pipeline:
                pipeline.hset(
                    self.document_key(document_id),
                    mapping={
                        "status": DocumentStatus.COMPLETED.value,
                        "pages": encoded_pages,
                        "summary": summary,
                        "attempts": str(attempts),
                        "updated_at": completed_at.isoformat(),
                        "completed_at": completed_at.isoformat(),
                    },
                )
                pipeline.hdel(
                    self.document_key(document_id),
                    "error_code",
                    "error_message",
                )
                pipeline.delete(self.source_key(document_id))
                await pipeline.execute()

    async def fail(
        self,
        document_id: UUID,
        *,
        error_code: str,
        error_message: str,
        completed_at: datetime,
        attempts: int,
    ) -> None:
        with redis_error_boundary("Document store"):
            async with self._redis.pipeline(transaction=True) as pipeline:
                pipeline.hset(
                    self.document_key(document_id),
                    mapping={
                        "status": DocumentStatus.FAILED.value,
                        "error_code": error_code,
                        "error_message": error_message,
                        "attempts": str(attempts),
                        "updated_at": completed_at.isoformat(),
                        "completed_at": completed_at.isoformat(),
                    },
                )
                pipeline.hdel(self.document_key(document_id), "pages", "summary")
                pipeline.delete(self.source_key(document_id))
                await pipeline.execute()

    async def acquire_lock(self, document_id: UUID, token: str, *, ttl_seconds: int) -> bool:
        with redis_error_boundary("Document store"):
            acquired = await self._redis.set(
                self.lock_key(document_id),
                token,
                ex=ttl_seconds,
                nx=True,
            )
        return bool(acquired)

    async def release_lock(self, document_id: UUID, token: str) -> bool:
        key = self.lock_key(document_id)
        with redis_error_boundary("Document store"):
            while True:
                async with self._redis.pipeline(transaction=True) as pipeline:
                    try:
                        await pipeline.watch(key)
                        current = await pipeline.get(key)
                        expected = token.encode()
                        if current not in (token, expected):
                            await pipeline.unwatch()
                            return False
                        pipeline.multi()
                        pipeline.delete(key)
                        await pipeline.execute()
                        return True
                    except WatchError:
                        continue

    @staticmethod
    def _decode_mapping(values: Mapping[bytes | str, bytes | str]) -> dict[str, str]:
        return {
            key.decode() if isinstance(key, bytes) else key: (
                value.decode() if isinstance(value, bytes) else value
            )
            for key, value in values.items()
        }

    @staticmethod
    def _decode_pages(encoded: str) -> tuple[PageContent, ...]:
        try:
            decoded = cast(object, json.loads(encoded))
        except json.JSONDecodeError as error:
            raise ValueError("Stored page data is not valid JSON.") from error
        if not isinstance(decoded, list):
            raise ValueError("Stored page data must be a list.")

        pages: list[PageContent] = []
        items = cast(list[object], decoded)
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Stored page entry must be an object.")
            entry = cast(dict[object, object], item)
            index = entry.get("index")
            markdown = entry.get("markdown")
            if not isinstance(index, int) or not isinstance(markdown, str):
                raise ValueError("Stored page entry has invalid fields.")
            pages.append(PageContent(index=index, markdown=markdown))
        return tuple(pages)
