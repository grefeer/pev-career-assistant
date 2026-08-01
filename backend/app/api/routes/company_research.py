"""API routes for company research (公司岗位调研).

A synchronous MVP: ``POST /company-research`` creates a ``queued`` report,
runs the deterministic single-page browse in-process, and writes the terminal
outcome before returning.  Gated behind ``company_research_enabled`` so a
deployment must explicitly opt in before the endpoint spawns a browser.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from backend.app.api.company_research_schemas import (
    CompanyResearchListResponse,
    CompanyResearchReportResponse,
    CreateCompanyResearchRequest,
)
from backend.app.api.dependencies import (
    _get_db,
    get_company_research_service,
    get_current_user,
)
from backend.app.db.models import CompanyResearchReport, User
from backend.app.services.company_research.service import (
    CompanyResearchNotFoundError,
    CompanyResearchService,
)

router = APIRouter(tags=["company_research"])


def _to_response(report: CompanyResearchReport) -> CompanyResearchReportResponse:
    """Map a report ORM row to the API response (``*_json`` -> clean names)."""
    block_reason = (
        report.block_reason.value
        if report.block_reason is not None and hasattr(report.block_reason, "value")
        else report.block_reason
    )
    return CompanyResearchReportResponse(
        id=str(report.id),
        user_id=str(report.user_id),
        company_name=report.company_name,
        source_url=report.source_url,
        agent_version=report.agent_version,
        status=report.status.value
        if hasattr(report.status, "value")
        else report.status,
        block_reason=block_reason,
        profile=report.profile_json,
        openings=report.openings_json or [],
        evidence_refs=report.evidence_refs_json or [],
        summary=report.summary,
        last_error=report.last_error,
        created_at=report.created_at,
        updated_at=report.updated_at,
        started_at=report.started_at,
        finished_at=report.finished_at,
    )


def _ensure_enabled(request: Request) -> None:
    settings = request.app.state.settings
    if not getattr(settings, "company_research_enabled", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "company_research_disabled",
                "message": "公司调研功能未启用。",
            },
        )


@router.post(
    "/company-research",
    response_model=CompanyResearchReportResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_and_run_report(
    req: CreateCompanyResearchRequest,
    request: Request,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[CompanyResearchService, Depends(get_company_research_service)],
) -> CompanyResearchReportResponse:
    """Create a company-research report and run it synchronously."""
    _ensure_enabled(request)
    report = service.create_report(
        db,
        user=current_user,
        company_name=req.company_name,
        source_url=req.source_url,
    )
    report = service.run_report(db, str(report.id), user=current_user)
    db.commit()
    return _to_response(report)


@router.get(
    "/company-research/{report_id}",
    response_model=CompanyResearchReportResponse,
)
def get_report(
    report_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[CompanyResearchService, Depends(get_company_research_service)],
) -> CompanyResearchReportResponse:
    """Get a single company-research report (owner-scoped)."""
    try:
        report = service.get_report(db, report_id, user=current_user)
    except CompanyResearchNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found"},
        ) from None
    return _to_response(report)


@router.get(
    "/company-research",
    response_model=CompanyResearchListResponse,
)
def list_reports(
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[CompanyResearchService, Depends(get_company_research_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CompanyResearchListResponse:
    """Page through the current user's company-research reports."""
    items = service.list_reports(db, user=current_user, limit=limit, offset=offset)
    return CompanyResearchListResponse(
        items=[_to_response(r) for r in items],
        total=len(items),
    )
