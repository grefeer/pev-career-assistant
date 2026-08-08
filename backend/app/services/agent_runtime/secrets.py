"""Secrets indirect layer: redaction, bounded backoff retry, key rotation (C4).

docs/findjobs-optimization-plan.zh-CN.md §7 C4.  Security gate #4: secrets
never land in repo/logs/argv.  This module owns the three supporting
mechanisms:

  - ``redact_key`` / ``redact_text`` — tail-6 redaction that keeps error
    context (IDs, request labels) intact so logs stay traceable;
  - ``with_retry`` — bounded exponential backoff (1s/2s/4s cap + jitter) for
    LLM-call paths (llm_extractor / model_gateway);
  - ``rotate_keys`` — failover to the next configured key when the current
    one fails verification.

Key access stays an indirection over environment variables (no new provider
infrastructure is assumed; callers pass the env var name).
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Backoff ladder 1s/2s/4s, capped; base doubles per attempt.
_BACKOFF_BASE_SEC = 1.0
_BACKOFF_CAP_SEC = 4.0


def read_key(env_name: str) -> str:
    """Read a secret from the environment (never from files or argv)."""
    return os.environ.get(env_name, "")


def redact_key(key: str) -> str:
    """Tail-6 redaction for logs; short or empty keys collapse entirely."""
    if not key:
        return "<empty>"
    if len(key) <= 8:
        return "***"
    return f"...{key[-6:]}"


def redact_text(text: str, secrets: list[str]) -> str:
    """Replace every full-secret occurrence in ``text`` with its redaction.

    Surrounding context (error codes, run IDs) is preserved verbatim.
    """
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, redact_key(secret))
    return redacted


def backoff_delays(
    *,
    base: float = _BACKOFF_BASE_SEC,
    cap: float = _BACKOFF_CAP_SEC,
    attempts: int = 3,
    jitter: Callable[[], float] | None = None,
) -> list[float]:
    """Deterministic 1/2/4 (capped) delay ladder, plus optional jitter."""
    jitter_fn = jitter or (lambda: 0.0)
    return [min(base * (2**i), cap) + jitter_fn() for i in range(attempts)]


def with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    retryable: Callable[[BaseException], bool] | None = None,
    delays: list[float] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "call",
) -> T:
    """Run ``fn`` with bounded backoff; the last failure propagates.

    ``retryable`` filters which exceptions deserve a retry (default: all).
    """
    ladder = delays or backoff_delays(attempts=attempts)
    last: BaseException | None = None
    for index, delay in enumerate(ladder):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - retry boundary is deliberate
            last = exc
            if retryable is not None and not retryable(exc):
                raise
            if index < len(ladder) - 1:
                logger.warning(
                    "%s attempt %d/%d failed (%s); retrying in %.1fs",
                    label, index + 1, len(ladder), type(exc).__name__, delay,
                )
                sleep(delay)
    assert last is not None
    raise last


def rotate_keys(
    keys: list[str],
    *,
    verify: Callable[[str], bool],
) -> str:
    """First key passing ``verify``; the last configured key as the fallback.

    A failed old key therefore switches to the next configured key without
    ever exposing either key in logs (verification result only).
    """
    for key in keys[:-1]:
        if key and verify(key):
            return key
    return keys[-1] if keys else ""
