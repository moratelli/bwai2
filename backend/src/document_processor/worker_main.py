"""Executable entrypoint for the Redis Streams worker pool."""

import asyncio
import logging
import socket

from document_processor.config import Settings
from document_processor.runtime import build_worker, create_redis

_LOGGER = logging.getLogger(__name__)


async def run_worker_pool(settings: Settings | None = None) -> None:
    """Run the configured number of consumers until process cancellation."""
    app_settings = settings or Settings()
    redis = create_redis(app_settings)
    hostname = socket.gethostname()
    workers = tuple(
        build_worker(
            settings=app_settings,
            redis=redis,
            consumer_name=f"{hostname}-{index}",
        )
        for index in range(1, app_settings.worker_concurrency + 1)
    )
    _LOGGER.info(
        "worker_pool_started",
        extra={"event": "worker_pool_started", "worker_count": len(workers)},
    )
    try:
        await asyncio.gather(*(worker.run_forever() for worker in workers))
    finally:
        await redis.aclose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_worker_pool())


if __name__ == "__main__":
    main()
