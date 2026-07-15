from __future__ import annotations

import hashlib
import ipaddress
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

    def check(self, *, action: str, identity: str, limit: int | None = None) -> None:
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
        if int(count) > (limit if limit is not None else self.limit):
            raise RateLimitExceededError("authentication rate limit exceeded")


def resolve_client_ip(
    peer: str, forwarded_real_ip: str | None, trusted_proxy_cidrs: str
) -> str:
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    trusted = False
    for raw_cidr in trusted_proxy_cidrs.split(","):
        raw_cidr = raw_cidr.strip()
        if not raw_cidr:
            continue
        try:
            if peer_ip in ipaddress.ip_network(raw_cidr, strict=False):
                trusted = True
                break
        except ValueError:
            continue
    if not trusted or not forwarded_real_ip:
        return peer
    try:
        return str(ipaddress.ip_address(forwarded_real_ip.strip()))
    except ValueError:
        return peer
