"""Unit tests for TrajectoryBuffer."""
from __future__ import annotations

import pytest
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer


class TestTrajectoryBuffer:
    def test_init_basic(self):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        assert buf.task_id == "t1"
        assert buf.strategy_id == "s1"
        assert buf.executor_type == "snapshot"
        assert buf.steps == []
        assert buf.failed_step_index is None

    def test_record_success_step(self):
        buf = TrajectoryBuffer(task_id="t1", strategy_id=None, executor_type="adapter")
        buf.record_step("open_url", "ok", {"url": "https://x.com"}, "html content", duration_ms=150.0)
        assert len(buf.steps) == 1
        step = buf.steps[0]
        assert step["tool"] == "open_url"
        assert step["status"] == "ok"
        assert step["params"] == {"url": "https://x.com"}
        assert step["result"] == "html content"
        assert step["error"] is None
        assert step["duration_ms"] == 150.0
        assert "timestamp" in step

    def test_record_error_step_sets_failed_index(self):
        buf = TrajectoryBuffer(task_id="t1", strategy_id=None, executor_type="snapshot")
        buf.record_step("open_url", "ok", {}, "ok")
        buf.record_step("parse", "failed", {}, None, error=ValueError("bad"))
        assert buf.failed_step_index == 1

    def test_record_after_failure_goes_to_fallback(self):
        buf = TrajectoryBuffer(task_id="t1", strategy_id=None, executor_type="snapshot")
        buf.record_step("step1", "ok", {}, "ok")
        buf.record_step("step2", "failed", {}, None, error=RuntimeError("fail"))
        # After failure, further steps recorded are fallback (Supervisor takeover)
        buf.record_step("step3", "ok", {}, "fallback result")
        assert buf.failed_step_index == 1
        assert len(buf.steps) == 3
        # The fallback flag
        assert buf.steps[2]["is_fallback"] is True

    def test_to_snapshot_context(self):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        buf.record_step("open_url", "ok", {"url": "https://x.com"}, {"html": "<p>"})
        buf.record_step("parse", "failed", {"html": "<p>"}, None, error=ValueError("empty"))
        ctx = buf.to_snapshot_context()
        assert ctx["source"] == "snapshot"
        assert ctx["strategy_id"] == "s1"
        assert len(ctx["completed_steps"]) == 1
        assert ctx["completed_steps"][0]["tool"] == "open_url"
        assert ctx["failed_step"]["tool"] == "parse"
        assert "empty" in ctx["failed_step"]["error"]

    def test_to_dict(self):
        buf = TrajectoryBuffer(task_id="t1", strategy_id=None, executor_type="supervisor")
        buf.record_step("open_url", "ok", {}, "ok")
        d = buf.to_dict()
        assert d["task_id"] == "t1"
        assert d["executor_type"] == "supervisor"
        assert len(d["steps"]) == 1
