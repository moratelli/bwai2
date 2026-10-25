"""Translate Redis client failures into safe application errors."""

from collections.abc import Generator
from contextlib import contextmanager

from redis.exceptions import RedisError

from document_processor.domain.errors import InfrastructureUnavailableError


@contextmanager
def redis_error_boundary(resource: str) -> Generator[None, None, None]:
    """Map Redis failures without exposing connection details."""
    try:
        yield
    except RedisError as error:
        raise InfrastructureUnavailableError(resource) from error
