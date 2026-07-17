from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, get_current_user
from backend.app.api.match_schemas import (
    CreateMatchRequest,
    MatchReportListResponse,
    MatchReportResponse,
)
from backend.app.db.models import User
from backend.app.services.match_service import MatchService

router = APIRouter(tags=["matches"])


def get_match_service(request: Request) -> MatchService:
    return request.app.state.match_service


@router.post("/matches", response_model=MatchReportResponse, status_code=201)
def create_match(
    req: CreateMatchRequest,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    match_service: Annotated[MatchService, Depends(get_match_service)],
) -> MatchReportResponse:
    try:
        report = match_service.create_match(
            db=db,
            user_id=current_user.id,
            job_id=req.job_id,
            profile_version_id=req.profile_version_id,
            idempotency_key=idempotency_key,
            analysis_session_id=req.analysis_session_id,
        )
    except ValueError as e:
        code = str(e)
        if code == "not_found":
            raise HTTPException(404, detail={"code": "not_found"})
        if code == "match_not_verified_job":
            raise HTTPException(422, detail={"code": code})
        if code == "idempotency_key_conflict":
            raise HTTPException(409, detail={"code": code})
        raise

    return _to_response(report)


@router.get("/matches/{match_id}", response_model=MatchReportResponse)
def get_match(
    match_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    match_service: Annotated[MatchService, Depends(get_match_service)],
) -> MatchReportResponse:
    report = match_service.repo.get_by_id(db, match_id, current_user.id)
    if report is None:
        raise HTTPException(404, detail={"code": "not_found"})
    return _to_response(report)


@router.get("/matches", response_model=MatchReportListResponse)
def list_matches(
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    match_service: Annotated[MatchService, Depends(get_match_service)],
    analysis_session_id: str | None = None,
) -> MatchReportListResponse:
    if analysis_session_id:
        items = match_service.repo.list_by_session(db, analysis_session_id, current_user.id)
    else:
        from backend.app.db.models import MatchReport as MR

        items = (
            db.query(MR)
            .filter(MR.user_id == current_user.id)
            .order_by(MR.created_at.desc())
            .limit(50)
            .all()
        )
    return MatchReportListResponse(
        items=[_to_response(r) for r in items],
        total=len(items),
    )


def _to_response(report) -> MatchReportResponse:
    return MatchReportResponse(
        id=report.id,
        analysis_session_id=report.analysis_session_id,
        job_id=getattr(report, "job_id", ""),
        profile_version_id=getattr(report, "profile_version_id", ""),
        status=report.status,
        score=report.score,
        score_components=report.score_components,
        strengths=report.strengths,
        gaps=report.gaps,
        unknowns=report.unknowns,
        risks=report.risks,
        application_priority=report.application_priority,
        recommendation=report.recommendation,
        error_code=report.error_code,
        scoring_rule_version=report.scoring_rule_version,
        model_version=report.model_version,
        prompt_version=report.prompt_version,
        output_schema_version=report.output_schema_version,
        created_at=str(report.created_at),
        started_at=str(report.started_at) if report.started_at else None,
        completed_at=str(report.completed_at) if report.completed_at else None,
    )
