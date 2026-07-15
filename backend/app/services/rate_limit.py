from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


class RateLimitExceededError(RuntimeError):
    pass


class RateLimitUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class RedisFixedWindowRateLimiter:
    redis: Any
    limit: int = 10
    window_seconds: int = 60

    def check(self, *, action: str, identity: str) -> None:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        key = f"auth-rate:{action}:{digest}"
        try:
            pipe = self.redis.pipeline(transaction=True)
            pipe.incr(key)
            pipe.expire(key, self.window_seconds, nx=True)
            count, _ = pipe.execute()
        except Exception as exc:
            raise RateLimitUnavailableError(
                "authentication protection unavailable"
            ) from exc
        if int(count) > self.limit:
            raise RateLimitExceededError("authentication rate limit exceeded")
