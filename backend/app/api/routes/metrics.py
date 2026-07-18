from __future__ import annotations

from fastapi import APIRouter, Request, Response


router = APIRouter()


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> Response:
    settings = request.app.state.settings
    dependencies = {
        "mysql": "unknown",
        "redis": "unknown",
        "object_store": "unknown",
    }

    try:
        from backend.app.api.routes.health import (
            _mysql_is_up,
            _object_store_is_up,
            _redis_is_up,
        )

        dependencies = {
            "mysql": "up" if _mysql_is_up(request) else "down",
            "redis": "up" if _redis_is_up(request) else "down",
            "object_store": "up" if _object_store_is_up(request) else "down",
        }
    except Exception:
        pass

    ready = 1 if all(value == "up" for value in dependencies.values()) else 0
    lines = [
        "# HELP career_assistant_app_info Application build information.",
        "# TYPE career_assistant_app_info gauge",
        (
            'career_assistant_app_info{'
            f'app_env="{_escape_label_value(settings.app_env)}",'
            f'version="{_escape_label_value(request.app.version)}"'
            "} 1"
        ),
        "# HELP career_assistant_ready Backend dependency readiness.",
        "# TYPE career_assistant_ready gauge",
        f"career_assistant_ready {ready}",
        "# HELP career_assistant_dependency_up Dependency readiness by name.",
        "# TYPE career_assistant_dependency_up gauge",
    ]
    for name, status in dependencies.items():
        lines.append(
            "career_assistant_dependency_up{"
            f'dependency="{_escape_label_value(name)}"'
            f"}} {1 if status == 'up' else 0}"
        )

    return Response(
        "\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
