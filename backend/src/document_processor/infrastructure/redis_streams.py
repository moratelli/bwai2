"""Redis Streams queue used by document workers."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self, cast
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ResponseError


@dataclass(frozen=True, slots=True)
class StreamMessage:
    message_id: str
    document_id: UUID
    delivery_count: int


class AsyncStreamPipeline(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def xadd(self, name: str, fields: Mapping[str, str]) -> Self: ...

    async def execute(self) -> list[object]: ...


class AsyncStreamRedis(Protocol):
    def pipeline(self, *, transaction: bool = True) -> AsyncStreamPipeline: ...

    async def xgroup_create(
        self,
        name: str,
        groupname: str,
        *,
        id: str,
        mkstream: bool,
    ) -> bool: ...

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: Mapping[str, str],
        *,
        count: int,
        block: int,
    ) -> object: ...

    async def xack(self, name: str, groupname: str, *ids: str) -> int: ...

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str,
        *,
        count: int,
    ) -> object: ...

    async def xpending_range(
        self,
        name: str,
        groupname: str,
        min: str,
        max: str,
        count: int,
    ) -> object: ...


class RedisStreamQueue:
    """Publish and consume document identifiers with at-least-once delivery."""

    def __init__(
        self,
        redis: Redis,
        *,
        stream_name: str = "documents:processing",
        group_name: str = "document-workers",
    ) -> None:
        self._redis = cast(AsyncStreamRedis, redis)
        self.stream_name = stream_name
        self.group_name = group_name

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                self.stream_name,
                self.group_name,
                id="0",
                mkstream=True,
            )
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def publish_batch(self, document_ids: Sequence[UUID]) -> tuple[str, ...]:
        if not document_ids:
            return ()
        async with self._redis.pipeline(transaction=True) as pipeline:
            for document_id in document_ids:
                pipeline.xadd(self.stream_name, {"document_id": str(document_id)})
            results = await pipeline.execute()
        return tuple(self._as_text(result) for result in results)

    async def read_new(
        self,
        consumer_name: str,
        *,
        count: int,
        block_ms: int,
    ) -> tuple[StreamMessage, ...]:
        response = await self._redis.xreadgroup(
            self.group_name,
            consumer_name,
            {self.stream_name: ">"},
            count=count,
            block=block_ms,
        )
        entries = self._entries_from_read_response(response)
        return await self._messages(entries)

    async def acknowledge(self, message_id: str) -> int:
        return await self._redis.xack(self.stream_name, self.group_name, message_id)

    async def reclaim_stale(
        self,
        consumer_name: str,
        *,
        minimum_idle_ms: int,
        count: int,
    ) -> tuple[StreamMessage, ...]:
        response = await self._redis.xautoclaim(
            self.stream_name,
            self.group_name,
            consumer_name,
            minimum_idle_ms,
            "0-0",
            count=count,
        )
        entries = self._entries_from_claim_response(response)
        return await self._messages(entries)

    async def _messages(
        self,
        entries: Sequence[tuple[str, Mapping[str, str]]],
    ) -> tuple[StreamMessage, ...]:
        messages: list[StreamMessage] = []
        for message_id, fields in entries:
            document_id = fields.get("document_id")
            if document_id is None:
                continue
            messages.append(
                StreamMessage(
                    message_id=message_id,
                    document_id=UUID(document_id),
                    delivery_count=await self._delivery_count(message_id),
                )
            )
        return tuple(messages)

    async def _delivery_count(self, message_id: str) -> int:
        response = await self._redis.xpending_range(
            self.stream_name,
            self.group_name,
            message_id,
            message_id,
            1,
        )
        items = self._as_list(response)
        if not items:
            return 1
        item = items[0]
        if not isinstance(item, dict):
            return 1
        entry = cast(dict[object, object], item)
        value = entry.get("times_delivered", entry.get(b"times_delivered", 1))
        return int(value) if isinstance(value, int | str | bytes) else 1

    @classmethod
    def _entries_from_read_response(
        cls,
        response: object,
    ) -> tuple[tuple[str, Mapping[str, str]], ...]:
        streams = cls._as_list(response)
        if not streams:
            return ()
        stream = cls._pair(streams[0])
        if stream is None:
            return ()
        return cls._parse_entries(stream[1])

    @classmethod
    def _entries_from_claim_response(
        cls,
        response: object,
    ) -> tuple[tuple[str, Mapping[str, str]], ...]:
        claimed = cls._as_list(response)
        if len(claimed) < 2:
            return ()
        return cls._parse_entries(claimed[1])

    @classmethod
    def _parse_entries(
        cls,
        response: object,
    ) -> tuple[tuple[str, Mapping[str, str]], ...]:
        parsed: list[tuple[str, Mapping[str, str]]] = []
        for raw_entry in cls._as_list(response):
            pair = cls._pair(raw_entry)
            if pair is None or not isinstance(pair[1], dict):
                continue
            raw_fields = cast(dict[object, object], pair[1])
            fields = {cls._as_text(key): cls._as_text(value) for key, value in raw_fields.items()}
            parsed.append((cls._as_text(pair[0]), fields))
        return tuple(parsed)

    @staticmethod
    def _pair(value: object) -> tuple[object, object] | None:
        pair: Sequence[object]
        if isinstance(value, list):
            pair = cast(list[object], value)
        elif isinstance(value, tuple):
            pair = cast(tuple[object, ...], value)
        else:
            return None
        if len(pair) != 2:
            return None
        return pair[0], pair[1]

    @staticmethod
    def _as_list(value: object) -> list[object]:
        if not isinstance(value, list | tuple):
            return []
        return list(cast(Sequence[object], value))

    @staticmethod
    def _as_text(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode()
        return str(value)
