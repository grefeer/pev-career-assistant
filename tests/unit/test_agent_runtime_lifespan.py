"""Application lifespan wiring for the opt-in adaptive PEV runtime."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.main import create_app
from tests.conftest import settings_override


def test_lifespan_exposes_disabled_agent_service_without_constructing_live_model() -> None:
    """Disabled installations retain a service boundary but never contact an LLM."""
    app = create_app(settings_override(agent_harness_enabled=False), graph=object())
    app.state.match_service = object()
    app.state.draft_service = object()
    app.state.interview_prep_service = object()
    app.state.application_tracking_service = object()

    with TestClient(app):
        assert app.state.agent_run_service is not None
        assert app.state.agent_runtime is None
