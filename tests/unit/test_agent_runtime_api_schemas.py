"""API-layer DTO validation for agent-run request bodies."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.api.agent_runtime_schemas import (
    CreateAgentRunRequest,
    RecoverAgentRunRequest,
    ResumeAgentRunRequest,
)


def test_create_run_rejects_whitespace_only_goal() -> None:
    """A whitespace-only goal is normalized to empty and rejected, not stored."""
    with pytest.raises(ValidationError, match="empty"):
        CreateAgentRunRequest(goal="   ", allowed_skills=["job-discovery"])


def test_resume_run_rejects_whitespace_only_response() -> None:
    """A whitespace-only human reply cannot resume a paused run."""
    with pytest.raises(ValidationError, match="empty"):
        ResumeAgentRunRequest(user_response="   ")


def test_create_run_forbids_extra_context_keys() -> None:
    """extra=forbid keeps the public request surface auditable."""
    with pytest.raises(ValidationError):
        CreateAgentRunRequest(
            goal="找岗位",
            allowed_skills=["job-discovery"],
            injected_secret="should-not-be-allowed",  # type: ignore[call-arg]
        )


def test_recover_run_forbids_any_body_field() -> None:
    """Recovery never accepts browser-supplied context, so the body must be empty."""
    with pytest.raises(ValidationError):
        RecoverAgentRunRequest(forced_context="should-not-be-allowed")  # type: ignore[call-arg]
