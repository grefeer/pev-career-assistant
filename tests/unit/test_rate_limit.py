"""Unit tests for the auth rate limiter and trusted-proxy IP resolution."""

from __future__ import annotations

from typing import Any

import pytest

from backend.app.services.rate_limit import (
    RateLimitExceededError,
    RateLimitUnavailableError,
    RedisFixedWindowRateLimiter,
    resolve_client_ip,
)


class _FakeRedis:
    """Minimal redis stand-in exposing only pipeline()/incr/expire/execute/ping."""

    def __init__(self, *, fail: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.expires: dict[str, int] = {}
        self._fail = fail

    def pipeline(self, transaction: bool = True) -> "_FakePipeline":
        return _FakePipeline(self)

    def ping(self) -> bool:
        return True


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._ops: list[Any] = []

    def incr(self, key: str) -> None:
        self._ops.append(("incr", key))

    def expire(self, key: str, seconds: int, nx: bool = False) -> None:
        self._ops.append(("expire", key, seconds, nx))

    def execute(self) -> list[Any]:
        if self._redis._fail:
            raise RuntimeError("redis is down")
        results: list[Any] = []
        for op in self._ops:
            if op[0] == "incr":
                key = op[1]
                self._redis.counts[key] = self._redis.counts.get(key, 0) + 1
                results.append(self._redis.counts[key])
            elif op[0] == "expire":
                results.append(True)
        return results


def test_check_uses_sha256_digest_when_no_secret() -> None:
    redis = _FakeRedis()
    limiter = RedisFixedWindowRateLimiter(redis, limit=2)

    limiter.check(action="login-ip", identity="1.2.3.4")
    limiter.check(action="login-ip", identity="1.2.3.4")

    with pytest.raises(RateLimitExceededError):
        limiter.check(action="login-ip", identity="1.2.3.4")


def test_check_uses_hmac_digest_when_secret_provided() -> None:
    redis = _FakeRedis()
    limiter = RedisFixedWindowRateLimiter(redis, limit=1, secret="super-secret")

    limiter.check(action="login-ip", identity="1.2.3.4")
    with pytest.raises(RateLimitExceededError):
        limiter.check(action="login-ip", identity="1.2.3.4")

    # Different identity should not share the HMAC key bucket.
    limiter.check(action="login-ip", identity="5.6.7.8")


def test_check_respects_per_call_limit_override() -> None:
    redis = _FakeRedis()
    limiter = RedisFixedWindowRateLimiter(redis, limit=100)

    limiter.check(action="register-ip", identity="1.2.3.4", limit=1)
    with pytest.raises(RateLimitExceededError):
        limiter.check(action="register-ip", identity="1.2.3.4", limit=1)


def test_check_raises_unavailable_when_redis_fails() -> None:
    redis = _FakeRedis(fail=True)
    limiter = RedisFixedWindowRateLimiter(redis, limit=10)

    with pytest.raises(RateLimitUnavailableError):
        limiter.check(action="login-ip", identity="1.2.3.4")


@pytest.mark.parametrize(
    ("peer", "forwarded", "trusted_cidrs", "expected"),
    [
        # Untrusted peer returns peer unchanged.
        ("8.8.8.8", "10.0.0.1", "127.0.0.0/8", "8.8.8.8"),
        # Trusted proxy forwards the real client IP.
        ("127.0.0.1", "203.0.113.5", "127.0.0.0/8", "203.0.113.5"),
        # Trusted proxy but no forwarded header -> peer.
        ("127.0.0.1", None, "127.0.0.0/8", "127.0.0.1"),
        # Invalid forwarded value -> peer.
        ("127.0.0.1", "not-an-ip", "127.0.0.0/8", "127.0.0.1"),
        # Multiple CIDRs, second matches.
        ("10.0.0.1", "192.0.2.9", "127.0.0.0/8,10.0.0.0/8", "192.0.2.9"),
        # Blank entries in the CIDR list are skipped.
        ("127.0.0.1", "192.0.2.9", ",127.0.0.0/8,", "192.0.2.9"),
        # Invalid CIDR entry is skipped without error.
        ("127.0.0.1", "192.0.2.9", "not-a-cidr,127.0.0.0/8", "192.0.2.9"),
    ],
)
def test_resolve_client_ip(
    peer: str,
    forwarded: str | None,
    trusted_cidrs: str,
    expected: str,
) -> None:
    assert resolve_client_ip(peer, forwarded, trusted_cidrs) == expected


def test_resolve_client_ip_returns_peer_when_not_an_ip() -> None:
    assert resolve_client_ip("unknown", "1.2.3.4", "127.0.0.0/8") == "unknown"
