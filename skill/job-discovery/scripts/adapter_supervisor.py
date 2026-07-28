#!/usr/bin/env python3
"""Adapter-aware Skill supervisor with a lossless Skill fallback.

This is intentionally an orchestration seam, not a site scraper.  A caller
supplies the normal Skill executor (browse -> per-page deep-agent extraction
-> validate/deduplicate).  When a certified backend adapter is available it
may run first; any adapter exception, including one raised after recording
partial trajectory steps, resumes the Skill executor with that trajectory as
auditable context.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from importlib import import_module
from typing import Any, Callable


@dataclass
class SkillSupervisorOutcome:
    path: str
    result: Any
    adapter_attempted: bool = False
    adapter_error: str | None = None
    trajectory_steps: list[dict[str, Any]] = field(default_factory=list)


def build_skill_deep_agent(*, model: Any, tools: list[Any], system_prompt: str) -> Any:
    """Build the Skill planner with Deep Agents; importing is deliberately lazy."""
    from deepagents import create_deep_agent

    return create_deep_agent(model=model, tools=tools, system_prompt=system_prompt)


def load_adapter(adapter_path: str | None, source_url: str) -> Any | None:
    """Load a configured backend adapter only when it accepts the public URL."""
    if not adapter_path:
        return None
    module_name, _, class_name = adapter_path.rpartition(".")
    if not module_name or not class_name:
        raise ValueError("adapter_path must be a dotted class path")
    adapter = getattr(import_module(module_name), class_name)()
    validate = getattr(adapter, "validate", None)
    if not callable(validate):
        raise TypeError("configured adapter has no validate(url) method")
    return adapter if validate(source_url) else None


def run_with_adapter_fallback(
    *,
    task: Any,
    adapter: Any | None,
    strategy: Any | None,
    skill_executor: Callable[[Any, dict[str, Any]], Any],
    trajectory: Any | None = None,
) -> SkillSupervisorOutcome:
    """Run adapter when present, otherwise (or on error) run the Skill path.

    ``skill_executor`` receives a serializable context containing every
    trajectory step available at handoff.  It owns all browser/sub-agent work;
    this function never hides an adapter failure or returns a partial adapter
    result as a successful final answer.
    """
    if adapter is None:
        return SkillSupervisorOutcome(
            path="skill_no_adapter",
            result=skill_executor(task, {"source": "no_adapter", "trajectory_steps": []}),
        )

    try:
        result = adapter.execute(task, strategy, trajectory)
        return SkillSupervisorOutcome(
            path="adapter",
            result=result,
            adapter_attempted=True,
            trajectory_steps=_trajectory_steps(trajectory),
        )
    except Exception as exc:  # Adapter failure is explicitly recoverable.
        steps = _trajectory_steps(trajectory)
        context = {
            "source": "adapter_failed",
            "adapter": type(adapter).__name__,
            "adapter_error": f"{type(exc).__name__}: {exc}",
            "trajectory_steps": steps,
        }
        return SkillSupervisorOutcome(
            path="skill_after_adapter_failure",
            result=skill_executor(task, context),
            adapter_attempted=True,
            adapter_error=context["adapter_error"],
            trajectory_steps=steps,
        )


def _trajectory_steps(trajectory: Any | None) -> list[dict[str, Any]]:
    steps = getattr(trajectory, "steps", None)
    if not isinstance(steps, list):
        return []
    serialized: list[dict[str, Any]] = []
    for step in steps:
        if isinstance(step, dict):
            serialized.append(dict(step))
        elif hasattr(step, "__dataclass_fields__"):
            serialized.append(asdict(step))
    return serialized
