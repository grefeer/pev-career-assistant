"""C4 secrets layer: redaction, backoff ladder, retry, key rotation."""

from __future__ import annotations

import pytest

from backend.app.services.agent_runtime.secrets import (
    backoff_delays,
    read_key,
    redact_key,
    redact_text,
    rotate_keys,
    with_retry,
)


def test_redact_key_tail_six_and_collapses_short() -> None:
    assert redact_key("sk-abcdef123456") == "...123456"
    assert redact_key("short") == "***"
    assert redact_key("") == "<empty>"


def test_redact_text_keeps_context_ids() -> None:
    text = "run run-abc123 failed with key sk-abcdef123456 and trace t-42"
    redacted = redact_text(text, ["sk-abcdef123456"])
    assert redacted == "run run-abc123 failed with key ...123456 and trace t-42"
    assert "sk-abcdef123456" not in redacted


def test_redact_text_handles_empty_secrets() -> None:
    assert redact_text("plain text", []) == "plain text"
    assert redact_text("plain text", [None, ""]) == "plain text"


def test_backoff_ladder_is_1_2_4_capped(monkeypatch) -> None:
    assert backoff_delays(attempts=3) == [1.0, 2.0, 4.0]
    # cap applies: attempt 4 would be 8s but stays at 4s
    assert backoff_delays(attempts=4) == [1.0, 2.0, 4.0, 4.0]
    assert backoff_delays(attempts=5, base=1.0, cap=4.0)[-1] == 4.0


def test_backoff_jitter_is_additive_and_deterministic() -> None:
    delays = backoff_delays(attempts=3, jitter=lambda: 0.25)
    assert delays == [1.25, 2.25, 4.25]


def test_with_retry_succeeds_first_try() -> None:
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        return "ok"

    assert with_retry(fn, delays=[0.0, 0.0]) == "ok"
    assert calls == [1]


def test_with_retry_retries_then_succeeds() -> None:
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("transient")
        return "recovered"

    slept: list[float] = []
    result = with_retry(fn, delays=[1.0, 2.0, 0.0], sleep=slept.append)
    assert result == "recovered"
    assert calls == [1, 1, 1]
    assert slept == [1.0, 2.0]  # ladder honoured between attempts


def test_with_retry_raises_last_failure_after_exhaustion() -> None:
    def fn() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        with_retry(fn, attempts=3, delays=[0.0, 0.0, 0.0])


def test_with_retry_retryable_filter_short_circuits() -> None:
    calls: list[int] = []

    def fn() -> None:
        calls.append(1)
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        with_retry(fn, attempts=3, retryable=lambda exc: isinstance(exc, RuntimeError))
    assert calls == [1]  # no retry for non-retryable failures


def test_rotate_keys_switches_from_failed_old_key() -> None:
    keys = ["sk-old-123456", "sk-new-789012"]
    selected = rotate_keys(keys, verify=lambda key: key == "sk-new-789012")
    assert selected == "sk-new-789012"


def test_rotate_keys_falls_back_to_last_configured() -> None:
    keys = ["sk-a-111111", "sk-b-222222"]
    assert rotate_keys(keys, verify=lambda key: False) == "sk-b-222222"
    assert rotate_keys([], verify=lambda key: True) == ""


def test_read_key_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("TEST_C4_KEY", "sk-env-abcdef123456")
    assert read_key("TEST_C4_KEY") == "sk-env-abcdef123456"
    assert read_key("TEST_C4_MISSING") == ""


def test_rotate_keys_skips_empty_entries_then_verifies() -> None:
    keys = ["", "", "sk-ok-789012"]
    assert rotate_keys(keys, verify=lambda key: key == "sk-ok-789012") == "sk-ok-789012"
    # an empty key never wins even with a permissive verifier
    assert rotate_keys(["", "sk-real-123456"], verify=lambda key: True) == "sk-real-123456"


def test_rotate_keys_picks_mid_list_key_when_verify_passes() -> None:
    # a non-last key passing verify is returned from the loop; the fallback
    # only wins when every earlier key fails
    keys = ["sk-stale-111111", "sk-live-222222", "sk-backup-333333"]
    assert rotate_keys(keys, verify=lambda key: key == "sk-live-222222") == "sk-live-222222"
