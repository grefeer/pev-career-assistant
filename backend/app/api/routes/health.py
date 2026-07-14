from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text


router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


def _mysql_is_up(request: Request) -> bool:
    try:
        session_factory: Callable[[], Any] | None = getattr(
            request.app.state, "session_factory", None
        )
        if session_factory is None:
            from backend.app.db.session import SessionLocal

            session_factory = SessionLocal
        with session_factory() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _redis_is_up(request: Request) -> bool:
    try:
        return request.app.state.redis.ping() is not False
    except Exception:
        return False


def _object_store_is_up(request: Request) -> bool:
    try:
        request.app.state.blob_store.check_bucket()
        return True
    except Exception:
        return False


@router.get("/health/ready")
def ready(request: Request) -> dict[str, Any]:
    dependencies = {
        "mysql": "up" if _mysql_is_up(request) else "down",
        "redis": "up" if _redis_is_up(request) else "down",
        "object_store": "up" if _object_store_is_up(request) else "down",
    }
    if "down" in dependencies.values():
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "dependencies": dependencies},
        )
    return {"status": "ready", "dependencies": dependencies}
