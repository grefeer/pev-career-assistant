"""Privacy-safe decision tracing contracts for autonomous PEV roles."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.app.domain.agent_runtime import AgentRole


DecisionTrace = Callable[[AgentRole, dict[str, Any], dict[str, Any] | None], None]


def decision_summary(
    *,
    action: str,
    tool_name: str | None = None,
    verification_decision: str | None = None,
) -> dict[str, str]:
    """Whitelist auditable decision metadata without retaining user/tool payloads."""
    summary = {"action": action}
    if tool_name:
        summary["tool_name"] = tool_name
    if verification_decision:
        summary["verification_decision"] = verification_decision
    return summary
