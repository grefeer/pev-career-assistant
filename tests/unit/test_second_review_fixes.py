from itertools import product

import fakeredis
import pytest

from backend.app.db.models import ApplicationTaskStatus as S, TaskActor as A
from backend.app.services.applications import ALLOWED_TRANSITION_ACTORS
from backend.app.services.rate_limit import (
    RateLimitExceededError,
    RedisFixedWindowRateLimiter,
    resolve_client_ip,
)


EXPECTED = {
    (S.CREATED, S.WAITING_FOR_DEVICE): {A.SYSTEM},
    (S.CREATED, S.CANCELLED): {A.HUMAN},
    (S.WAITING_FOR_DEVICE, S.DISPATCHED): {A.SYSTEM},
    (S.WAITING_FOR_DEVICE, S.CANCELLED): {A.HUMAN},
    (S.DISPATCHED, S.RUNNING): {A.EXECUTOR},
    (S.DISPATCHED, S.WAITING_FOR_HUMAN): {A.EXECUTOR},
    (S.DISPATCHED, S.FAILED): {A.EXECUTOR},
    (S.DISPATCHED, S.CANCELLED): {A.HUMAN},
    (S.RUNNING, S.WAITING_FOR_HUMAN): {A.EXECUTOR},
    (S.RUNNING, S.READY_FOR_REVIEW): {A.EXECUTOR},
    (S.RUNNING, S.FAILED): {A.EXECUTOR},
    (S.RUNNING, S.CANCELLED): {A.HUMAN},
    (S.WAITING_FOR_HUMAN, S.RUNNING): {A.EXECUTOR},
    (S.WAITING_FOR_HUMAN, S.READY_FOR_REVIEW): {A.EXECUTOR},
    (S.WAITING_FOR_HUMAN, S.FAILED): {A.EXECUTOR},
    (S.WAITING_FOR_HUMAN, S.CANCELLED): {A.HUMAN},
    (S.READY_FOR_REVIEW, S.OBSERVING_USER_SUBMISSION): {A.HUMAN},
    (S.READY_FOR_REVIEW, S.CANCELLED): {A.HUMAN},
    (S.OBSERVING_USER_SUBMISSION, S.SUBMITTED_SUCCESS): {A.EXECUTOR},
    (S.OBSERVING_USER_SUBMISSION, S.SUBMITTED_FAILED): {A.EXECUTOR},
    (S.OBSERVING_USER_SUBMISSION, S.RESULT_UNKNOWN): {A.EXECUTOR},
}


def test_transition_actor_matrix_is_complete_and_exact() -> None:
    assert ALLOWED_TRANSITION_ACTORS == EXPECTED
    for edge, actor in product(EXPECTED, A):
        assert (actor in ALLOWED_TRANSITION_ACTORS[edge]) is (actor in EXPECTED[edge])
    assert A.EXECUTOR not in ALLOWED_TRANSITION_ACTORS[(S.READY_FOR_REVIEW, S.CANCELLED)]


def test_rate_limit_account_bucket_does_not_lock_other_accounts() -> None:
    limiter = RedisFixedWindowRateLimiter(fakeredis.FakeRedis())
    for _ in range(8):
        limiter.check(action="login-account", identity="alice", limit=8)
    with pytest.raises(RateLimitExceededError):
        limiter.check(action="login-account", identity="alice", limit=8)
    limiter.check(action="login-account", identity="bob", limit=8)


def test_proxy_identity_only_trusts_configured_peer_and_ignores_xff() -> None:
    assert resolve_client_ip("203.0.113.9", "198.51.100.7", "172.16.0.0/12") == "203.0.113.9"
    assert resolve_client_ip("172.20.0.4", "198.51.100.7", "172.16.0.0/12") == "198.51.100.7"
    assert resolve_client_ip("172.20.0.4", "not-an-ip", "172.16.0.0/12") == "172.20.0.4"
