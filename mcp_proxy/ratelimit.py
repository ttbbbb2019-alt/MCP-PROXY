from __future__ import annotations

import time
from typing import Dict, Optional


class RateLimiter:
    """Naive token bucket that is easy to replace with a distributed implementation."""

    def __init__(self, max_per_minute: Optional[int] = None) -> None:
        self._max = max_per_minute
        self._buckets: Dict[str, Dict[str, float]] = {}

    def is_configured(self) -> bool:
        return bool(self._max)

    def allow(self, key: str) -> bool:
        if not self._max:
            return True
        bucket = self._buckets.setdefault(key, {"tokens": float(self._max), "updated": time.time()})
        now = time.time()
        elapsed = now - bucket["updated"]
        bucket["updated"] = now
        bucket["tokens"] = min(self._max, bucket["tokens"] + elapsed * (self._max / 60.0))
        if bucket["tokens"] >= 1:
            bucket["tokens"] -= 1
            return True
        return False
