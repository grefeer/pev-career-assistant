"""Authenticated HTTP API for user-scoped adaptive PEV runs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from backend.app.api.agent_runtime_schemas import (
    AgentArtifactListResponse,
    AgentArtifactResponse,
    AgentEventListResponse,
    AgentEventResponse,
    AgentRunCreatedResponse,
    AgentRunListResponse,
    AgentRunResponse,
    CreateAgentRunRequest,
)
from backend.app.api.dependencies import (
    _get_db,
    get_agent_run_service,
    get_current_user,
)
from backend.app.db.models import User
from backend.app.services.agent_runtime.schemas import AgentBudget, AgentTaskRequest
from backend.app.services.agent_runtime.service import (
    AgentRunNotFoundError,
    AgentRunService,
    AgentRuntimeDisabledError,
    AgentRuntimeUnavailableError,
)

router = APIRouter(prefix="/agent-runs", tags=["agent_runtime"])


def _value(value: object | None) -> str | None:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _to_run_response(run) -> AgentRunResponse:
    return AgentRunResponse(
        id=str(run.id),
        goal=run.goal,
        status=_value(run.status) or "failed",
        complexity=_value(run.complexity),
        summary=run.final_summary,
        error_code=run.error_code,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


@router.get("", response_model=AgentRunListResponse)
def list_agent_runs(
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AgentRunService, Depends(get_agent_run_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AgentRunListResponse:
    """List recent personal task summaries without exposing their private context."""
    runs = service.list_runs(db, user_id=current_user.id, limit=limit)
    return AgentRunListResponse(items=[_to_run_response(run) for run in runs])


@router.post("", response_model=AgentRunCreatedResponse, status_code=status.HTTP_201_CREATED)
def create_agent_run(
    request_body: CreateAgentRunRequest,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AgentRunService, Depends(get_agent_run_service)],
    request: Request,
) -> AgentRunCreatedResponse:
    """Start one bounded PEV run with server-enforced resource ceilings."""
    settings = request.app.state.settings
    task = AgentTaskRequest(
        goal=request_body.goal,
        allowed_skills=request_body.allowed_skills,
        context=request_body.context,
        budget=AgentBudget(
            max_agent_turns=settings.agent_harness_max_agent_turns,
            max_tool_calls=settings.agent_harness_max_tool_calls,
            max_replans=settings.agent_harness_max_replans,
        ),
    )
    try:
        result = service.create_run(db, user_id=current_user.id, task=task)
    except AgentRuntimeDisabledError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "agent_harness_disabled"},
        ) from None
    except AgentRuntimeUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "agent_harness_unavailable"},
        ) from None
    db.commit()
    return AgentRunCreatedResponse(
        id=result.run_id,
        status=result.status.value,
        summary=result.summary,
        error_code=result.error_code,
    )


@router.get("/{run_id}", response_model=AgentRunResponse)
def get_agent_run(
    run_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AgentRunService, Depends(get_agent_run_service)],
) -> AgentRunResponse:
    try:
        return _to_run_response(service.get_run(db, user_id=current_user.id, run_id=run_id))
    except AgentRunNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}) from None


@router.get("/{run_id}/events", response_model=AgentEventListResponse)
def list_agent_events(
    run_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AgentRunService, Depends(get_agent_run_service)],
) -> AgentEventListResponse:
    try:
        events = service.list_events(db, user_id=current_user.id, run_id=run_id)
    except AgentRunNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}) from None
    return AgentEventListResponse(
        items=[
            AgentEventResponse(
                sequence=event.sequence,
                event_type=event.event_type,
                payload=event.payload_json,
                created_at=event.created_at,
            )
            for event in events
        ]
    )


@router.get("/{run_id}/artifacts", response_model=AgentArtifactListResponse)
def list_agent_artifacts(
    run_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[AgentRunService, Depends(get_agent_run_service)],
) -> AgentArtifactListResponse:
    """Return the owner's evidence/result artifacts without run-private context."""
    try:
        artifacts = service.list_artifacts(db, user_id=current_user.id, run_id=run_id)
    except AgentRunNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}) from None
    return AgentArtifactListResponse(
        items=[
            AgentArtifactResponse(
                id=artifact.id,
                artifact_type=artifact.artifact_type,
                source_url=artifact.source_url,
                content_hash=artifact.content_hash,
                content=artifact.content_json,
                created_at=artifact.created_at,
            )
            for artifact in artifacts
        ]
    )
