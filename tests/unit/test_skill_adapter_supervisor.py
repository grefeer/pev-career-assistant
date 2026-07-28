from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[2] / "skill" / "job-discovery" / "scripts" / "adapter_supervisor.py"
_SPEC = importlib.util.spec_from_file_location("skill_adapter_supervisor", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


@dataclass
class FakeTrajectory:
    steps: list[dict] = field(default_factory=list)


class SuccessfulAdapter:
    def execute(self, task, strategy, trajectory):
        trajectory.steps.append({"tool": "adapter", "status": "ok"})
        return {"status": "succeeded", "task": task}


class HalfFailedAdapter:
    def execute(self, task, strategy, trajectory):
        trajectory.steps.append({"tool": "list", "status": "ok"})
        trajectory.steps.append({"tool": "detail", "status": "failed"})
        raise RuntimeError("detail request failed")


def test_skill_path_runs_without_adapter() -> None:
    calls = []
    outcome = _MODULE.run_with_adapter_fallback(
        task="url", adapter=None, strategy=None,
        skill_executor=lambda task, context: calls.append((task, context)) or {"ok": True},
    )
    assert outcome.path == "skill_no_adapter"
    assert outcome.result == {"ok": True}
    assert calls[0][1]["trajectory_steps"] == []


def test_successful_adapter_keeps_adapter_result() -> None:
    trajectory = FakeTrajectory()
    outcome = _MODULE.run_with_adapter_fallback(
        task="url", adapter=SuccessfulAdapter(), strategy=None, trajectory=trajectory,
        skill_executor=lambda *_: (_ for _ in ()).throw(AssertionError("must not fallback")),
    )
    assert outcome.path == "adapter"
    assert outcome.adapter_attempted is True
    assert outcome.trajectory_steps == [{"tool": "adapter", "status": "ok"}]


def test_half_failed_adapter_falls_back_with_full_trajectory() -> None:
    trajectory = FakeTrajectory()
    received = {}
    outcome = _MODULE.run_with_adapter_fallback(
        task="url", adapter=HalfFailedAdapter(), strategy=None, trajectory=trajectory,
        skill_executor=lambda task, context: received.update(context) or {"rescued": task},
    )
    assert outcome.path == "skill_after_adapter_failure"
    assert outcome.result == {"rescued": "url"}
    assert outcome.adapter_error == "RuntimeError: detail request failed"
    assert received["trajectory_steps"] == [
        {"tool": "list", "status": "ok"}, {"tool": "detail", "status": "failed"},
    ]


def test_build_skill_deep_agent_uses_create_deep_agent(monkeypatch) -> None:
    calls = {}

    class FakeDeepAgents:
        @staticmethod
        def create_deep_agent(**kwargs):
            calls.update(kwargs)
            return "agent"

    monkeypatch.setitem(sys.modules, "deepagents", FakeDeepAgents)

    assert _MODULE.build_skill_deep_agent(
        model="model", tools=["browse"], system_prompt="skill workflow"
    ) == "agent"
    assert calls == {
        "model": "model", "tools": ["browse"], "system_prompt": "skill workflow"
    }
