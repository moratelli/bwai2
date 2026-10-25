"""Redis-backed temporary document storage."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self, cast
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import WatchError

from document_processor.domain.models import (
    AttemptAdmission,
    AttemptsExhausted,
    DocumentRecord,
    DocumentStatus,
    PageContent,
    ParserKind,
    RetryWaiting,
    StateTransition,
)
from document_processor.domain.provider import AttemptFailure, RetrySchedule
from document_processor.infrastructure.redis_errors import redis_error_boundary
from document_processor.infrastructure.retry_metadata import (
    decode_deadline,
    decode_failure,
    encode_failure,
)

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

    def delete(self, *names: str) -> Self: ...

    async def execute(self) -> list[object]: ...

    async def watch(self, *names: str) -> None: ...

    async def unwatch(self) -> None: ...

    async def time(self) -> tuple[int, int]: ...

    async def get(self, name: str) -> bytes | str | None: ...

    async def hgetall(self, name: str) -> Mapping[bytes | str, bytes | str]: ...

    def multi(self) -> None: ...


class AsyncRedisClient(Protocol):
    def pipeline(self, *, transaction: bool = True) -> AsyncRedisPipeline: ...

    async def hgetall(self, name: str) -> Mapping[bytes | str, bytes | str]: ...

    async def get(self, name: str) -> bytes | str | None: ...

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

    def __init__(self, redis: Redis) -> None:
        self._redis = cast(AsyncRedisClient, redis)

    @staticmethod
    def document_key(document_id: UUID) -> str:
        return f"document:{document_id}"

    @staticmethod
    def source_key(document_id: UUID) -> str:
        return f"document:{document_id}:source"

    @staticmethod
    def lock_key(document_id: UUID) -> str:
        return f"document:{document_id}:lock"

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
            last_failure=decode_failure(values.get("last_failure"), int(values["attempts"])),
            retry_not_before=decode_deadline(values.get("retry_not_before")),
        )

    async def get_source(self, document_id: UUID) -> bytes | None:
        with redis_error_boundary("Document store"):
            source = await self._redis.get(self.source_key(document_id))
        if source is None:
            return None
        if isinstance(source, bytes):
            return source
        return source.encode()

    async def admit_attempt(
        self,
        document_id: UUID,
        *,
        token: str,
        max_attempts: int,
        updated_at: datetime,
    ) -> AttemptAdmission:
        with redis_error_boundary("Document store"):
            while True:
                async with self._redis.pipeline(transaction=True) as pipeline:
                    try:
                        values = await self._watch_owned_document(pipeline, document_id, token)
                        if isinstance(values, StateTransition):
                            return values
                        previous = int(values["attempts"])
                        if previous >= max_attempts:
                            return AttemptsExhausted(
                                previous, decode_failure(values.get("last_failure"), previous)
                            )
                        deadline = decode_deadline(values.get("retry_not_before"))
                        if deadline is not None:
                            seconds, micros = await pipeline.time()
                            now = datetime.fromtimestamp(seconds + micros / 1_000_000, UTC)
                            delay = (deadline - now).total_seconds()
                            if delay > 0:
                                return RetryWaiting(previous, RetrySchedule(deadline, delay))
                        attempt = previous + 1
                        pipeline.multi()
                        pipeline.hset(
                            self.document_key(document_id),
                            mapping={
                                "status": DocumentStatus.PROCESSING.value,
                                "attempts": str(attempt),
                                "updated_at": updated_at.isoformat(),
                            },
                        )
                        pipeline.hdel(self.document_key(document_id), "retry_not_before")
                        await pipeline.execute()
                        return attempt
                    except WatchError:
                        continue

    async def record_retry_failure(
        self,
        document_id: UUID,
        *,
        token: str,
        failure: AttemptFailure,
        retry_not_before: datetime | None,
        updated_at: datetime,
    ) -> StateTransition:
        """Retain safe evidence before waiting, without renewing retention or deleting source."""
        fields: dict[str, RedisValue] = {
            "last_failure": encode_failure(failure),
            "updated_at": updated_at.isoformat(),
        }
        if retry_not_before is not None:
            if retry_not_before.tzinfo is None:
                raise ValueError("Retry deadlines must include a timezone.")
            fields["retry_not_before"] = retry_not_before.isoformat()
        return await self._update_owned(
            document_id,
            token,
            fields=fields,
            expected_attempt=failure.attempt,
            delete_source=False,
            clear_fields=("retry_not_before",) if retry_not_before is None else (),
        )

    async def complete(
        self,
        document_id: UUID,
        *,
        token: str,
        pages: Sequence[PageContent],
        summary: str,
        completed_at: datetime,
        attempts: int,
    ) -> StateTransition:
        encoded_pages = json.dumps(
            [{"index": page.index, "markdown": page.markdown} for page in pages],
            separators=(",", ":"),
        )
        return await self._update_owned(
            document_id,
            token,
            fields={
                "status": DocumentStatus.COMPLETED.value,
                "pages": encoded_pages,
                "summary": summary,
                "attempts": str(attempts),
                "updated_at": completed_at.isoformat(),
                "completed_at": completed_at.isoformat(),
            },
            expected_attempt=attempts,
            delete_source=True,
            clear_fields=("error_code", "error_message", "last_failure", "retry_not_before"),
        )

    async def fail(
        self,
        document_id: UUID,
        *,
        token: str,
        error_code: str,
        error_message: str,
        completed_at: datetime,
        attempts: int,
    ) -> StateTransition:
        return await self._update_owned(
            document_id,
            token,
            fields={
                "status": DocumentStatus.FAILED.value,
                "error_code": error_code,
                "error_message": error_message,
                "attempts": str(attempts),
                "updated_at": completed_at.isoformat(),
                "completed_at": completed_at.isoformat(),
            },
            expected_attempt=attempts,
            delete_source=True,
            clear_fields=("pages", "summary", "retry_not_before")
            + (() if error_code == "retry_exhausted" else ("last_failure",)),
        )

    async def _watch_owned_document(
        self,
        pipeline: AsyncRedisPipeline,
        document_id: UUID,
        token: str,
    ) -> dict[str, str] | StateTransition:
        key, lock = self.document_key(document_id), self.lock_key(document_id)
        await pipeline.watch(key, lock)
        values = self._decode_mapping(await pipeline.hgetall(key))
        if not values:
            return StateTransition.NOT_FOUND
        status = values.get("status")
        if status in {DocumentStatus.COMPLETED, DocumentStatus.FAILED}:
            return StateTransition.ALREADY_TERMINAL
        owner = await pipeline.get(lock)
        if owner not in (token, token.encode()) or status not in {
            DocumentStatus.QUEUED,
            DocumentStatus.PROCESSING,
        }:
            return StateTransition.LOST_OWNERSHIP
        return values

    async def _update_owned(
        self,
        document_id: UUID,
        token: str,
        *,
        fields: Mapping[str, RedisValue],
        expected_attempt: int,
        delete_source: bool,
        clear_fields: tuple[str, ...] = (),
    ) -> StateTransition:
        key = self.document_key(document_id)
        with redis_error_boundary("Document store"):
            while True:
                async with self._redis.pipeline(transaction=True) as pipeline:
                    try:
                        values = await self._watch_owned_document(pipeline, document_id, token)
                        if isinstance(values, StateTransition):
                            return values
                        if (
                            values.get("status") != DocumentStatus.PROCESSING
                            or expected_attempt <= 0
                            or values.get("attempts") != str(expected_attempt)
                        ):
                            return StateTransition.LOST_OWNERSHIP
                        pipeline.multi()
                        pipeline.hset(key, mapping=fields)
                        if clear_fields:
                            pipeline.hdel(key, *clear_fields)
                        if delete_source:
                            pipeline.delete(self.source_key(document_id))
                        await pipeline.execute()
                        return StateTransition.APPLIED
                    except WatchError:
                        continue

    async def acquire_lock(self, document_id: UUID, token: str, *, ttl_seconds: int) -> bool:
        with redis_error_boundary("Document store"):
            acquired = await self._redis.set(
                self.lock_key(document_id),
                token,
                ex=ttl_seconds,
                nx=True,
            )
        return bool(acquired)

    async def renew_lock(
        self,
        document_id: UUID,
        token: str,
        *,
        ttl_seconds: int,
    ) -> StateTransition:
        with redis_error_boundary("Document store"):
            while True:
                async with self._redis.pipeline(transaction=True) as pipeline:
                    try:
                        values = await self._watch_owned_document(pipeline, document_id, token)
                        if isinstance(values, StateTransition):
                            return values
                        pipeline.multi()
                        pipeline.expire(self.lock_key(document_id), ttl_seconds)
                        await pipeline.execute()
                        return StateTransition.APPLIED
                    except WatchError:
                        continue

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
        decoded: dict[str, str] = {}
        for raw_key, raw_value in values.items():
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            errors = "replace" if key in {"last_failure", "retry_not_before"} else "strict"
            decoded[key] = (
                raw_value.decode(errors=errors) if isinstance(raw_value, bytes) else raw_value
            )
        return decoded

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
