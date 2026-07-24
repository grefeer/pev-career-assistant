"""Unit tests for SnapshotExecutor."""
from __future__ import annotations

import pytest
from unittest.mock import patch

from backend.app.services.job_discovery.schemas import (
    DiscoveryTaskInput,
    StrategyRecord,
)
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer
from backend.app.services.job_discovery.strategy.snapshot_executor import SnapshotExecutor


SIMPLE_PLAN_YAML = """
plan:
  - tool: triage_link
    params:
      url: "{{task.url}}"
    expect: "classify URL"
    on_error: "skip"
  - tool: extract_jd_candidates
    params:
      page_text: "{{prev.result.text}}"
      url: "{{task.url}}"
    expect: "extract JDs"
    on_error: "retry_then_skip"
"""


@pytest.fixture
def strategy():
    return StrategyRecord(
        id="s1",
        url_pattern="test.com/*",
        site_type="other",
        plan_yaml=SIMPLE_PLAN_YAML,
    )


@pytest.fixture
def task():
    return DiscoveryTaskInput(
        source_id="s1", raw_record_id="r1", external_record_id="e1",
        source_key="test", source_url="https://test.com/job",
        url_hash="abc", record_fields=[],
    )


class TestSnapshotExecutor:
    def test_resolves_template_variables(self, strategy, task):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)

        # Test template resolution directly
        from yaml import safe_load
        plan = safe_load(SIMPLE_PLAN_YAML)["plan"]
        resolved = executor._resolve_template(plan[0]["params"], {"task": task, "prev": None})
        assert resolved["url"] == "https://test.com/job"

    def test_resolve_prev_result(self, strategy, task):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)
        context = {
            "task": task,
            "prev": {"result": {"text": "Job description here"}},
        }
        from yaml import safe_load
        plan = safe_load(SIMPLE_PLAN_YAML)["plan"]
        resolved = executor._resolve_template(plan[1]["params"], context)
        assert resolved["page_text"] == "Job description here"

    def test_missing_field_resolves_to_none(self, strategy, task):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)
        context = {"task": task, "prev": {"result": {}}}  # no 'text' key
        from yaml import safe_load
        plan = safe_load(SIMPLE_PLAN_YAML)["plan"]
        resolved = executor._resolve_template(plan[1]["params"], context)
        assert resolved["page_text"] is None

    def test_parses_yaml_plan(self, strategy, task):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)
        steps = executor._parse_plan()
        assert len(steps) == 2
        assert steps[0]["tool"] == "triage_link"
        assert steps[1]["tool"] == "extract_jd_candidates"

    @patch("backend.app.services.job_discovery.strategy.snapshot_executor._call_tool_by_name")
    def test_execute_short_circuit_on_failure(self, mock_call, strategy, task):
        """When a step fails, SnapshotExecutor returns a result with snapshot_context,
        not a fully completed result."""
        from backend.app.services.job_discovery.strategy.snapshot_executor import SnapshotExecutionResult

        mock_call.side_effect = [
            {"site_type": "other", "notes": "ok"},  # step 1 ok
            RuntimeError("extraction failed"),       # step 2 fails
        ]

        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)
        result = executor.execute()

        assert isinstance(result, SnapshotExecutionResult)
        assert result.needs_supervisor_fallback is True
        assert result.snapshot_context is not None
        assert result.snapshot_context["source"] == "snapshot"
        assert len(result.snapshot_context["completed_steps"]) == 1

    @patch("backend.app.services.job_discovery.strategy.snapshot_executor._call_tool_by_name")
    def test_execute_all_success(self, mock_call, strategy, task):
        mock_call.side_effect = [
            {"site_type": "other", "text": "JD text here"},
            [{"title": "Engineer", "company_name": "Acme"}],  # extract_jd_candidates returns list
        ]

        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)
        result = executor.execute()

        assert result.status == "succeeded"
        assert len(result.candidates) > 0

    # -- Hard-deadline (Task 6) guards ----------------------------------------

    @patch("backend.app.services.job_discovery.strategy.snapshot_executor._call_tool_by_name")
    def test_hard_timeout_tools_without_deadline_use_normal_dispatch(
        self, mock_call, strategy, task
    ):
        """hard_timeout_tools only spawns a subprocess when a deadline is set.

        Without ``deadline_seconds`` the tool runs via the normal in-process
        dispatch (no spawn overhead) -- the worker guarantees both are set
        together, and the executor enforces that invariant here.
        """
        mock_call.side_effect = [
            {"site_type": "other", "notes": "ok"},
            [{"title": "Engineer", "company_name": "Acme"}],
        ]

        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        # triage_link is listed as a hard-timeout tool but no deadline is set,
        # so it must still go through the mocked normal dispatch.
        executor = SnapshotExecutor(
            strategy, task, buf, hard_timeout_tools={"triage_link"}
        )
        result = executor.execute()

        assert result.status == "succeeded"
        # Both steps dispatched via the normal (mocked) path, not a subprocess.
        assert mock_call.call_count == 2

    # -- Tool dispatch tests (MINOR-3) ---------------------------------------

    def test_tool_dispatch_calls_real_function(self):
        """_call_tool_by_name dispatches to the actual tool from the registry."""
        from backend.app.services.job_discovery.strategy.snapshot_executor import (
            _TOOL_REGISTRY,
            _call_tool_by_name,
        )

        # Ensure clean registry
        _TOOL_REGISTRY.clear()
        # Mock at the source module so the import inside _ensure_tool_registry picks it up
        with patch(
            "backend.app.services.job_discovery.deepagents_runner.triage_link",
            return_value={"site_type": "other", "url": "https://test.com"},
        ) as mock_triage:
            result = _call_tool_by_name("triage_link", url="https://test.com")

            assert result == {"site_type": "other", "url": "https://test.com"}
            mock_triage.assert_called_once_with(url="https://test.com")

    def test_ensure_tool_registry_does_not_import_ocr_images_from_urls(self):
        """_ensure_tool_registry() must not register the phantom ocr_images_from_urls tool."""
        from backend.app.services.job_discovery.strategy.snapshot_executor import (
            _TOOL_REGISTRY,
            _ensure_tool_registry,
        )

        _TOOL_REGISTRY.clear()
        _ensure_tool_registry()

        # The phantom tool must not be in the registry
        assert "ocr_images_from_urls" not in _TOOL_REGISTRY
        # The 9 real tools should all be present (except run_web_navigation which is runtime-injected)
        assert "triage_link" in _TOOL_REGISTRY
        assert "parse_wechat_article" in _TOOL_REGISTRY
        assert "run_ocr" in _TOOL_REGISTRY
        assert "extract_jd_candidates" in _TOOL_REGISTRY
        assert "standardize_from_record_fields" in _TOOL_REGISTRY
        assert "verify_evidence" in _TOOL_REGISTRY
        assert "package_candidates" in _TOOL_REGISTRY
        assert "finish_with_manual_review" in _TOOL_REGISTRY
        # run_web_navigation is None in the static registry (runtime-injected)
        assert _TOOL_REGISTRY.get("run_web_navigation") is None
