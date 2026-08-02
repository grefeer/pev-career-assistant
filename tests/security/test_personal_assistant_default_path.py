"""Guard the personal-assistant production entrypoint against retired runtimes."""

from __future__ import annotations

from pathlib import Path

from backend.app.api.router import api_router


_ROOT = Path(__file__).resolve().parents[2]


def test_default_api_exposes_only_personal_assistant_routes() -> None:
    """Campus operations, device control and legacy graph endpoints are not public APIs."""
    paths = {route.path for route in api_router.routes}

    assert "/agent-runs" in paths
    assert "/profiles" in paths
    assert all(
        not path.startswith(prefix)
        for prefix in (
            "/jobs",
            "/job-submissions",
            "/devices",
            "/executor",
            "/sessions",
            "/matches",
            "/resume-drafts",
            "/job-discovery",
            "/company-research",
            "/interview-prep",
        )
        for path in paths
    )


def test_default_app_and_dependency_manifest_do_not_import_retired_graph_frameworks() -> None:
    """The custom PEV harness, rather than a hidden LangGraph fallback, owns production."""
    main_source = (_ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    gateway_source = (
        _ROOT / "backend" / "app" / "services" / "agent_runtime" / "model_gateway.py"
    ).read_text(encoding="utf-8")
    requirements = (_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()

    assert "src.graph" not in main_source
    assert "src.checkpointing" not in main_source
    assert "build_graph" not in main_source
    assert "from src." not in main_source
    assert "from src." not in gateway_source
    assert "langgraph" not in requirements
    assert "deepagents" not in requirements
