"""Synthetic PDF input shared by adapter and processing regressions."""

import os
from collections.abc import AsyncIterator
from typing import cast
from urllib.parse import urlparse

import pytest
from fakeredis.aioredis import FakeRedis
from redis.asyncio import Redis
from redis.exceptions import RedisError

from document_processor.ports import HealthProbe


@pytest.fixture(params=["fake", "real"])
async def queue_redis(request: pytest.FixtureRequest) -> AsyncIterator[Redis]:
    """Clients for queue regressions; each consuming fixture cleans up its own keys."""
    if request.param == "real":
        url = os.getenv("TEST_REDIS_URL")
        if url is None:
            pytest.skip("Set TEST_REDIS_URL to a disposable Redis 7 instance.")
        parsed = urlparse(url)
        client = Redis(
            host=parsed.hostname or "localhost",
            port=parsed.port or 6379,
            db=int(parsed.path.removeprefix("/") or "0"),
            password=parsed.password,
            decode_responses=False,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    else:
        client = FakeRedis(decode_responses=False)
    try:
        try:
            await cast(HealthProbe, client).ping()
        except RedisError:
            pytest.fail("The configured disposable Redis is unreachable.")
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def malformed_xref_pdf() -> bytes:
    """Declare more cross-reference entries than this tiny file can contain."""
    return (
        b"%PDF-1.4\n"
        b"xref\n0 1000\n"
        b"0000000000 65535 f \n"
        b"trailer\n<< /Size 1000 >>\n"
        b"startxref\n9\n%%EOF\n"
    )
