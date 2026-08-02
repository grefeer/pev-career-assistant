from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, get_current_user
from backend.app.db.models import AnalysisSession, User
from backend.app.repositories.sessions import (
    activate,
    create_for_user,
    get_owned,
    list_for_user,
)
from backend.app.schemas import (
    ActivateSessionResponse,
    HistoryItem,
    SessionListResponse,
    SessionStateResponse,
    SessionSummary,
)
from src.session_service import build_config, get_session_values, get_state_history_rows


router = APIRouter(prefix="/sessions", tags=["sessions"])


def summarize_session(values: dict[str, Any]) -> SessionSummary:
    return SessionSummary(
        user_goal=values.get("user_goal", ""),
        jobs_count=len(values.get("jobs", [])),
        analyses_count=len(values.get("analyses", [])),
        matches_count=len(values.get("matches", [])),
        optimization_round=values.get("optimization_round", 0),
        has_final_report=bool(values.get("final_report")),
        shortlist=values.get("shortlist", []),
        revision_notes=values.get("revision_notes", []),
    )


def serialize_session(item: AnalysisSession) -> dict[str, str]:
    return {
        "thread_id": item.thread_id,
        "label": item.label,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def require_owned(db: Session, current_user: User, thread_id: str) -> AnalysisSession:
    item = get_owned(db, current_user.id, thread_id)
    if item is None:
        raise HTTPException(status_code=404, detail="当前会话不存在或不属于你。")
    return item


@router.get("", response_model=SessionListResponse)
def list_sessions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> SessionListResponse:
    sessions = list_for_user(db, current_user.id)
    return SessionListResponse(
        active_thread_id=sessions[0].thread_id if sessions else "",
        sessions=[serialize_session(item) for item in sessions],
    )


@router.post("", response_model=ActivateSessionResponse)
def create_session(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> ActivateSessionResponse:
    item = create_for_user(db, current_user.id)
    activate(db, item)
    db.commit()
    return ActivateSessionResponse(ok=True, active_thread_id=item.thread_id)


@router.post("/{thread_id}/activate", response_model=ActivateSessionResponse)
def activate_session(
    thread_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> ActivateSessionResponse:
    item = require_owned(db, current_user, thread_id)
    activate(db, item)
    db.commit()
    return ActivateSessionResponse(ok=True, active_thread_id=thread_id)


@router.get("/{thread_id}", response_model=SessionStateResponse)
def session_state(
    thread_id: str,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> SessionStateResponse:
    require_owned(db, current_user, thread_id)
    values = get_session_values(request.app.state.graph, build_config(thread_id))
    return SessionStateResponse(
        thread_id=thread_id,
        values=values,
        summary=summarize_session(values),
    )


@router.get("/{thread_id}/history", response_model=list[HistoryItem])
def session_history(
    thread_id: str,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    limit: int = 10,
) -> list[HistoryItem]:
    require_owned(db, current_user, thread_id)
    rows = get_state_history_rows(
        request.app.state.graph, build_config(thread_id), limit=limit
    )
    return [HistoryItem(**row) for row in rows]


@router.get("/{thread_id}/label")
def session_label(
    thread_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> dict[str, str]:
    item = require_owned(db, current_user, thread_id)
    return {"label": item.label}
