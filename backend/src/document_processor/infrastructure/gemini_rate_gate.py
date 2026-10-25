"""Evenly spaced admissions for workers sharing Redis, scope, model and budget.

A permit is spent at the Redis transaction, not at HTTP response time. Waiters
reserve no future slots and always recheck after sleeping. Ambiguous commits
are never retried as successes; no cancellation path refunds a permit. State
outlives its latest deadline by sixty seconds and is not reset on startup.
"""

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self, cast

from redis.asyncio import Redis
from redis.exceptions import WatchError

from document_processor.domain.errors import InfrastructureUnavailableError
from document_processor.domain.provider import RetrySchedule
from document_processor.infrastructure.redis_errors import redis_error_boundary


class _Pipeline(Protocol):
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
    async def watch(self, *keys: str) -> None: ...
    async def time(self) -> tuple[int, int]: ...
    async def hgetall(self, key: str) -> Mapping[bytes | str, bytes | str]: ...
    def multi(self) -> None: ...
    def hset(self, key: str, *, mapping: Mapping[str, int]) -> Self: ...
    def pexpire(self, key: str, milliseconds: int) -> Self: ...
    async def execute(self) -> list[object]: ...


class _Client(Protocol):
    def pipeline(self, *, transaction: bool = True) -> _Pipeline: ...


class RedisGeminiRateGate:
    def __init__(
        self,
        redis: Redis,
        *,
        scope: str,
        model: str,
        requests_per_minute: int = 12,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if type(requests_per_minute) is not int or not 1 <= requests_per_minute <= 1000:
            raise ValueError("requests_per_minute must be an integer between 1 and 1000")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", scope) is None:
            raise ValueError("scope must be a nonsecret coordination label")
        self.key = f"gemini:{scope}:{model}:admission"
        self._redis = cast(_Client, redis)
        self._interval = math.ceil(60_000 / requests_per_minute)
        self._sleep = sleep

    async def acquire(self) -> None:
        while True:
            decision = await self._transition()
            if decision.delay_seconds == 0:
                return
            await self._sleep(decision.delay_seconds)

    async def cool_down(self, seconds: float) -> RetrySchedule:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("cooldown must be finite and nonnegative")
        return await self._transition(seconds)

    def _state(self, raw: Mapping[bytes | str, bytes | str]) -> tuple[int, int]:
        if not raw:
            return 0, 0
        try:
            values = {k.decode() if isinstance(k, bytes) else k: int(v) for k, v in raw.items()}
            if values["interval_ms"] != self._interval:
                raise ValueError("Coordination budgets differ")
            next_ms, cooldown_ms = values["next_ms"], values["cooldown_ms"]
            if min(next_ms, cooldown_ms) < 0:
                raise ValueError("Invalid admission deadline")
            return next_ms, cooldown_ms
        except (ValueError, KeyError, UnicodeError) as error:
            raise InfrastructureUnavailableError("Gemini request coordination") from error

    @staticmethod
    def _schedule(deadline_ms: int, now_ms: int) -> RetrySchedule:
        try:
            return RetrySchedule(
                datetime.fromtimestamp(deadline_ms / 1000, UTC),
                max(0, deadline_ms - now_ms) / 1000,
            )
        except (ValueError, OverflowError, OSError) as error:
            raise InfrastructureUnavailableError("Gemini request coordination") from error

    async def _transition(self, cooldown_seconds: float | None = None) -> RetrySchedule:
        with redis_error_boundary("Gemini request coordination"):
            for _ in range(5):
                async with self._redis.pipeline(transaction=True) as pipeline:
                    try:
                        await pipeline.watch(self.key)
                        next_ms, cooldown_ms = self._state(await pipeline.hgetall(self.key))
                        seconds, microseconds = await pipeline.time()
                        now_ms = seconds * 1000 + microseconds // 1000
                        ready_ms = max(next_ms, cooldown_ms)
                        if cooldown_seconds is None and ready_ms > now_ms:
                            return self._schedule(ready_ms, now_ms)
                        if cooldown_seconds is None:
                            next_ms = now_ms + self._interval
                            decision = self._schedule(now_ms, now_ms)
                        else:
                            cooldown_ms = max(
                                cooldown_ms, now_ms + math.ceil(cooldown_seconds * 1000)
                            )
                            decision = self._schedule(max(next_ms, cooldown_ms, now_ms), now_ms)
                        pipeline.multi()
                        pipeline.hset(
                            self.key,
                            mapping={
                                "interval_ms": self._interval,
                                "next_ms": next_ms,
                                "cooldown_ms": cooldown_ms,
                            },
                        )
                        pipeline.pexpire(
                            self.key, max(next_ms, cooldown_ms, now_ms) - now_ms + 60_000
                        )
                        await pipeline.execute()
                        return decision
                    except WatchError:
                        continue
                    except OverflowError as error:
                        raise InfrastructureUnavailableError(
                            "Gemini request coordination"
                        ) from error
        raise InfrastructureUnavailableError("Gemini request coordination")
