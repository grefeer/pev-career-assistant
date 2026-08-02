"""Application lifespan wiring for the opt-in adaptive PEV runtime."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.services.agent_runtime import provider_config
from tests.conftest import settings_override


def test_lifespan_exposes_disabled_agent_service_without_constructing_live_model() -> None:
    """Disabled installations retain a service boundary but never contact an LLM."""
    app = create_app(settings_override(agent_harness_enabled=False))

    with TestClient(app):
        assert app.state.agent_run_service is not None
        assert app.state.agent_runtime is None


def test_lifespan_falls_back_when_model_key_is_missing(monkeypatch) -> None:
    """A missing provider key disables the runtime but keeps the service boundary."""
    monkeypatch.setattr(provider_config, "get_api_key", lambda: None)
    app = create_app(settings_override(agent_harness_enabled=True))

    with TestClient(app):
        assert app.state.agent_runtime is None
        assert app.state.agent_run_service is not None


def test_create_app_accepts_injected_blob_store_and_session_factory() -> None:
    """External wiring can supply its own blob store and session factory."""
    blob_store = MagicMock(name="blob_store")
    session_factory = MagicMock(name="session_factory")
    app = create_app(
        settings_override(agent_harness_enabled=False),
        blob_store=blob_store,
        session_factory=session_factory,
    )

    assert app.state.blob_store is blob_store
    assert app.state.session_factory is session_factory


def test_lifespan_reuses_pre_provisioned_infrastructure_without_reowning_it() -> None:
    """When external wiring already owns infra, lifespan reuses it and skips teardown."""
    sentinel_redis = MagicMock(name="redis")
    sentinel_session = MagicMock(name="session_factory")
    sentinel_runtime = MagicMock(name="agent_runtime")
    sentinel_service = MagicMock(name="agent_run_service")
    app = create_app(settings_override(agent_harness_enabled=False))
    app.state.redis = sentinel_redis
    app.state.session_factory = sentinel_session
    app.state.agent_runtime = sentinel_runtime
    app.state.agent_run_service = sentinel_service

    with TestClient(app):
        # Lifespan reused every pre-provisioned component instead of rebuilding it.
        assert app.state.redis is sentinel_redis
        assert app.state.session_factory is sentinel_session
        assert app.state.agent_runtime is sentinel_runtime
        assert app.state.agent_run_service is sentinel_service

    # Pre-provisioned components are not owned by lifespan, so they survive teardown.
    assert app.state.redis is sentinel_redis
    assert app.state.session_factory is sentinel_session
    assert app.state.agent_runtime is sentinel_runtime
    assert app.state.agent_run_service is sentinel_service


def test_lifespan_connects_redis_without_a_password_when_env_var_is_absent(monkeypatch) -> None:
    """A missing REDIS_PASSWORD still wires a (lazy) client without raising."""
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    app = create_app(settings_override(agent_harness_enabled=False))

    with TestClient(app):
        assert app.state.redis is not None


def test_lifespan_reuses_a_pre_provisioned_runtime_without_rebuilding_it() -> None:
    """A pre-set agent_runtime is retained; the service is built around it, not rebuilt."""
    sentinel_runtime = MagicMock(name="agent_runtime")
    app = create_app(settings_override(agent_harness_enabled=False))
    app.state.agent_runtime = sentinel_runtime

    with TestClient(app):
        assert app.state.agent_runtime is sentinel_runtime
        assert app.state.agent_run_service is not None
