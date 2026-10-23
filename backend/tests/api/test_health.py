"""Health endpoint contract tests."""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from document_processor.api.app import create_app
from document_processor.config import Settings
from document_processor.domain.models import NewDocument


class NoopGateway:
    async def create_batch(self, documents: Sequence[NewDocument]) -> None:
        del documents


class FakeHealthProbe:
    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy
        self.calls = 0

    async def ping(self) -> bool:
        self.calls += 1
        return self.healthy


@asynccontextmanager
async def api_client(
    *,
    redis_healthy: bool,
    gemini_key: str | None = "configured-secret",
) -> AsyncGenerator[tuple[AsyncClient, FakeHealthProbe]]:
    probe = FakeHealthProbe(redis_healthy)
    app = create_app(
        settings=Settings(gemini_api_key=SecretStr(gemini_key) if gemini_key is not None else None),
        submission_gateway=NoopGateway(),
        health_probe=probe,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client, probe


@pytest.mark.asyncio
@pytest.mark.parametrize("redis_healthy", [True, False])
async def test_liveness_only_reports_that_api_process_is_running(redis_healthy: bool) -> None:
    async with api_client(redis_healthy=redis_healthy, gemini_key=None) as (client, probe):
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert probe.calls == 0


@pytest.mark.asyncio
async def test_readiness_succeeds_when_redis_and_gemini_are_available() -> None:
    async with api_client(redis_healthy=True) as (client, probe):
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"redis": "available", "gemini": "configured"},
    }
    assert probe.calls == 1


@pytest.mark.asyncio
async def test_readiness_fails_safely_when_redis_is_unavailable() -> None:
    async with api_client(redis_healthy=False) as (client, _probe):
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "checks": {"redis": "unavailable", "gemini": "configured"},
    }


@pytest.mark.asyncio
async def test_readiness_fails_without_gemini_and_never_returns_secret_values() -> None:
    async with api_client(redis_healthy=True, gemini_key=None) as (client, _probe):
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "checks": {"redis": "available", "gemini": "missing"},
    }
    assert "configured-secret" not in response.text
