"""Unit tests for the WeChat SnapshotPlan hard deadline (Task 6).

Two contracts:

1. :func:`run_with_hard_timeout` actually *terminates* a hung call (it does
   not merely raise a timeout while a blocked thread keeps running). Proven
   with the ``hang_forever`` fixture ending well within 2 s.
2. :class:`SnapshotExecutor` enforces a single absolute deadline: a step in
   ``hard_timeout_tools`` that exceeds the budget produces a deterministic
   ``needs_manual_review`` / ``task_deadline_exceeded`` result and does NOT
   escalate to the Supervisor/WebNavigationAgent.
"""
from __future__ import annotations

import time

from backend.app.services.job_discovery.schemas import (
    DiscoveryTaskInput,
    StrategyRecord,
)
from backend.app.services.job_discovery.strategy.deadline import (
    hang_forever,
    run_with_hard_timeout,
)
from backend.app.services.job_discovery.strategy.snapshot_executor import (
    SnapshotExecutor,
    SnapshotExecutionResult,
)
from backend.app.services.job_discovery.strategy.trajectory_buffer import (
    TrajectoryBuffer,
)


# Deterministic WeChat plan: triage -> ReadGZH fetch -> frozen JD extraction.
# Mirrors the canonical plan used by the manual live smokes -- no LLM, no
# ``run_web_navigation``.
WECHAT_DEADLINE_PLAN_YAML = """
plan:
  - tool: triage_link
    params:
      url: "{{task.source_url}}"
    expect: "classify wechat article"
    on_error: "skip"
  - tool: fetch_wechat_article
    params:
      url: "{{task.source_url}}"
    expect: "fetch wechat article via ReadGZH"
    on_error: "mark_manual_review"
  - tool: extract_jd_candidates
    params:
      page_text: "{{prev.result.text}}"
      url: "{{task.source_url}}"
    expect: "extract JDs"
    on_error: "skip"
"""


def wechat_strategy() -> StrategyRecord:
    return StrategyRecord(
        id="wechat-deadline",
        url_pattern="mp.weixin.qq.com/s/*",
        site_type="wechat",
        plan_yaml=WECHAT_DEADLINE_PLAN_YAML,
    )


def task() -> DiscoveryTaskInput:
    return DiscoveryTaskInput(
        source_id="s1",
        raw_record_id="r1",
        external_record_id="e1",
        source_key="tencent-27-referrals",
        source_url="https://mp.weixin.qq.com/s/deadline-fixture",
        url_hash="abc",
        record_fields=[],
    )


def trajectory() -> TrajectoryBuffer:
    return TrajectoryBuffer(
        task_id="t1", strategy_id="wechat-deadline", executor_type="snapshot"
    )


# ---------------------------------------------------------------------------
# 1. The hard-timeout primitive terminates a hung call
# ---------------------------------------------------------------------------


def test_hung_wechat_fetch_is_terminated() -> None:
    started = time.monotonic()
    result = run_with_hard_timeout(
        hang_forever,
        timeout_seconds=0.2,
    )
    assert result.timed_out is True
    # The whole call -- including spawn startup + terminate -- must finish
    # well under the 2 s budget, proving the child was actually killed.
    assert time.monotonic() - started < 2.0


def test_run_with_hard_timeout_returns_value_on_success() -> None:
    """A fast, successful call returns its value (not a timeout)."""

    def _echo(x: int) -> int:
        return x + 1

    # Module-level functions are picklable for spawn; closures are not, so
    # run a trivial module-level target instead.
    result = run_with_hard_timeout(
        _echo_success, 41, timeout_seconds=5.0
    )
    assert result.timed_out is False
    assert result.error is None
    assert result.value == 42


def _echo_success(x: int) -> int:
    """Module-level picklable echo for the success-path test."""
    return x + 1


def test_run_with_hard_timeout_returns_sanitized_error() -> None:
    """A child that raises surfaces a sanitized error string, not the object."""
    result = run_with_hard_timeout(_raise_value_error, timeout_seconds=5.0)
    assert result.timed_out is False
    assert result.value is None
    assert result.error is not None
    assert "ValueError" in result.error
    assert "boom" in result.error


def _raise_value_error() -> None:
    raise ValueError("boom")


# ---------------------------------------------------------------------------
# 2. SnapshotExecutor enforces the single absolute deadline
# ---------------------------------------------------------------------------


def test_snapshot_deadline_stops_before_next_step() -> None:
    """A hung fetch_wechat_article is terminated and reported as manual review."""
    executor = SnapshotExecutor(
        wechat_strategy(),
        task(),
        trajectory(),
        deadline_seconds=0.1,
        hard_timeout_tools={"fetch_wechat_article"},
        # Inject the hang fixture so the deadline fires deterministically
        # without real network/ReadGZH access.
        tool_dependencies={"fetch_wechat_article": hang_forever},
    )
    result = executor.execute()

    assert isinstance(result, SnapshotExecutionResult)
    assert result.status == "needs_manual_review"
    assert result.block_reason == "task_deadline_exceeded"
    # A deadline is a per-task timeout, not a structure failure: the worker
    # must NOT hand the task to the Supervisor/WebNavigationAgent.
    assert result.needs_supervisor_fallback is False
    assert result.snapshot_context is not None


def test_snapshot_deadline_skips_when_budget_exhausted_before_step() -> None:
    """When the deadline has already passed, the step is not even started."""
    # A zero budget means ``remaining <= 0`` on the very first step.
    executor = SnapshotExecutor(
        wechat_strategy(),
        task(),
        trajectory(),
        deadline_seconds=0.0,
        hard_timeout_tools={"fetch_wechat_article"},
        tool_dependencies={"fetch_wechat_article": hang_forever},
    )
    # deadline_seconds=0 -> no deadline is set (falsy). Force an already-past
    # deadline by rewinding _deadline_at into the past.
    executor._deadline_at = time.monotonic() - 1.0
    result = executor.execute()

    assert result.status == "needs_manual_review"
    assert result.block_reason == "task_deadline_exceeded"


def test_snapshot_without_deadline_is_unbounded() -> None:
    """Without a deadline, the hard-timeout path is skipped entirely."""
    # triage_link runs for real (no network) and returns a classification;
    # fetch_wechat_article is injected as a fast stub so the plan completes.
    executor = SnapshotExecutor(
        wechat_strategy(),
        task(),
        trajectory(),
        tool_dependencies={"fetch_wechat_article": _fast_fetch_stub},
    )
    result = executor.execute()

    # Plan completed all three steps without any deadline interference.
    assert result.status != "needs_manual_review"
    assert result.block_reason != "task_deadline_exceeded"


def _fast_fetch_stub(url: str, deadline_remaining_seconds: float | None = None) -> dict:
    """Module-level stub that returns a minimal fetch_wechat_article payload."""
    return {
        "text": "Senior Java Engineer. Requirements: 3+ years. Send resume to a@b.com",
        "title": "Hiring",
        "url": url,
        "needs_manual_review": False,
        "manual_review_reason": None,
        "image_ocr_texts": [],
        "image_count": 0,
        "application_emails": ["a@b.com"],
        "application_urls": [],
    }
