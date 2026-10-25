"""Shared provider failure classification."""

import httpx

_RETRYABLE_STATUS_CODES = {408, 409, 429}


def is_retryable_provider_error(error: Exception, *status_attributes: str) -> bool:
    """Return whether a provider failure is safe to retry."""
    if isinstance(error, (httpx.TransportError, TimeoutError, ConnectionError)):
        return True
    for attribute in status_attributes:
        status = getattr(error, attribute, None)
        if isinstance(status, int) and (status in _RETRYABLE_STATUS_CODES or status >= 500):
            return True
    return False
