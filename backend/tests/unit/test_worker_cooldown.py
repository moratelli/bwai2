"""Retry backoff, provider hints, and shared cooldown publication."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from pydantic import ValidationError

from document_processor.config import Settings
from document_processor.domain.errors import (
    InfrastructureUnavailableError,
    RetryableProcessingError,
)
from document_processor.domain.provider import ProviderFailure, RetrySchedule
from document_processor.worker import DocumentWorker
from tests.unit.test_worker import (
    LOCK_OWNER,
    MESSAGE,
    NOW,
    FakeProcessor,
    FakeQueue,
    FakeRepository,
)


@dataclass
class CooldownGate:
    delays: list[float] = field(default_factory=list[float])
    unavailable: bool = False

    async def acquire(self) -> None:
        return None

    async def cool_down(self, seconds: float) -> RetrySchedule:
        if self.unavailable:
            raise InfrastructureUnavailableError("Request coordination")
        self.delays.append(seconds)
        return RetrySchedule(NOW + timedelta(seconds=seconds), seconds)


def instance(
    queue: FakeQueue,
    repository: FakeRepository,
    processor: FakeProcessor,
    gate: CooldownGate,
    sleep: Callable[[float], Awaitable[None]],
) -> DocumentWorker:
    return DocumentWorker(
        queue=queue,
        repository=repository,
        processor=processor,
        consumer_name="worker-one",
        max_attempts=3,
        retry_base_seconds=15,
        retry_max_seconds=20,
        retry_jitter_seconds=5,
        reclaim_idle_ms=30000,
        read_block_ms=50,
        batch_size=10,
        lock_ttl_seconds=60,
        sleep=sleep,
        random=lambda: 0.5,
        token_factory=lambda: LOCK_OWNER,
        clock=lambda: NOW,
        provider_gate=gate,
    )


@pytest.mark.parametrize("hint,expected", [(None, [17.5, 22.5, 22.5]), (60, [62.5, 62.5, 62.5])])
async def test_capped_backoff_honors_hint_and_publishes_on_final_attempt(
    hint: float | None, expected: list[float]
) -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    error = RetryableProcessingError(
        "gemini_temporarily_unavailable",
        "Safe Gemini failure.",
        provider_info=ProviderFailure("rate_limited", "summarization", 429),
        retry_after_seconds=hint,
    )
    gate = CooldownGate()
    queue, repository = FakeQueue(new=(MESSAGE,)), FakeRepository()
    processor = FakeProcessor([error, error, error])
    await instance(queue, repository, processor, gate, sleep).run_once()
    assert gate.delays == expected
    assert sleeps == expected[:2]
    assert processor.attempts == [1, 2, 3]
    assert queue.events[-1] == "ack:1-0"


async def test_infrastructure_error_does_not_broadcast_gemini_cooldown() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    error = InfrastructureUnavailableError("Document store")
    gate = CooldownGate()
    await instance(
        FakeQueue(new=(MESSAGE,)), FakeRepository(), FakeProcessor([error] * 3), gate, sleep
    ).run_once()
    assert gate.delays == []
    assert sleeps == [17.5, 22.5]


async def test_uncertain_cooldown_leaves_work_pending_without_more_processing() -> None:
    async def no_sleep(seconds: float) -> None:
        pytest.fail("Uncertain coordination must not schedule another request.")

    error = RetryableProcessingError(
        "gemini_temporarily_unavailable",
        "Safe Gemini failure.",
        provider_info=ProviderFailure("unavailable", "summarization", 503),
    )
    queue, repository = FakeQueue(new=(MESSAGE,)), FakeRepository()
    processor = FakeProcessor([error])
    await instance(
        queue, repository, processor, CooldownGate(unavailable=True), no_sleep
    ).run_once()
    assert processor.attempts == [1]
    assert queue.events == ["group"]
    assert not any(event.startswith("fail:") for event in repository.events)
    assert repository.events[-1] == "release"


def test_runtime_retry_defaults_are_slow_and_bounded() -> None:
    settings = Settings()
    assert settings.worker_retry_base_seconds == 15
    assert settings.worker_retry_max_seconds == 60
    assert settings.worker_retry_jitter_seconds == 5


@pytest.mark.parametrize(
    "field",
    ["worker_retry_base_seconds", "worker_retry_max_seconds", "worker_retry_jitter_seconds"],
)
@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1.0])
def test_retry_settings_reject_unsafe_numbers(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: value})
