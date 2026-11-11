"""进程内令牌桶限流，与单进程 worker 架构一致。"""

from __future__ import annotations

import threading
import time


class TokenBucketLimiter:
    def __init__(self, rate_per_minute: float, burst: int):
        self.rate = max(rate_per_minute, 0.1) / 60.0
        self.burst = max(burst, 1)
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, updated = self._buckets.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - updated) * self.rate)
            if tokens < 1:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1, now)
            return True
