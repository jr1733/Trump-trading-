"""Shared HTTP helper: timeouts, retries, exponential backoff with jitter.

Kept tiny and dependency-free so adapters share one retry policy and one
User-Agent, and so the backoff is unit-testable without network access.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

from ..config import settings

log = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 30.0, jitter: bool = True) -> float:
    """Exponential backoff with full jitter. `attempt` is 0-based."""
    raw = min(cap, base * (2**attempt))
    return random.uniform(0, raw) if jitter else raw


def with_retries(
    func: Callable[[], T],
    *,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    jitter: bool = True,
) -> T:
    """Call `func`, retrying retryable HTTP/transport errors with backoff."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return func()
        except httpx.HTTPStatusError as exc:
            last = exc
            if exc.response.status_code not in RETRYABLE_STATUS:
                raise
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last = exc
        if attempt < attempts - 1:
            delay = backoff_delay(attempt, jitter=jitter)
            log.info("retrying after %.2fs (attempt %d/%d): %s", delay, attempt + 1, attempts, last)
            sleep(delay)
    assert last is not None
    raise last


def get(url: str, *, params: dict | None = None, attempts: int = 3) -> httpx.Response:
    headers = {"User-Agent": settings.http_user_agent, "Accept": "*/*"}

    def _call() -> httpx.Response:
        with httpx.Client(timeout=settings.http_timeout_seconds, follow_redirects=True) as client:
            response = client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response

    return with_retries(_call, attempts=attempts)
