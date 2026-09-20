"""Rate limiting and failure injection for the simulated legacy ERP.

A simplistic in-process token bucket. It is not meant to be a good rate limiter; it is
meant to behave like the customer's rate limiter well enough that the migration
client's backoff, Retry-After handling and replay logic are exercised for real rather
than assumed. A single process is enough, because the demo runs one dev server.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque

from django.conf import settings

_lock = threading.Lock()
_hits: deque[float] = deque()


def rate_limit_exceeded() -> float | None:
    """Return how many seconds the caller should wait, or None if it may proceed."""
    limit = int(getattr(settings, "LEGACY_RATE_LIMIT_PER_SECOND", 0) or 0)
    if limit <= 0:
        return None

    now = time.monotonic()
    with _lock:
        while _hits and now - _hits[0] > 1.0:
            _hits.popleft()
        if len(_hits) >= limit:
            oldest = _hits[0]
            wait = max(0.05, 1.0 - (now - oldest))
            return round(wait, 3)
        _hits.append(now)
    return None


def should_fail() -> bool:
    """Intermittent 503, the way a customer's ERP behaves under load."""
    rate = float(getattr(settings, "LEGACY_FLAKE_RATE", 0.0) or 0.0)
    if rate <= 0:
        return False
    return random.random() < rate


def reset() -> None:
    """Used by tests to start from a clean bucket."""
    with _lock:
        _hits.clear()
