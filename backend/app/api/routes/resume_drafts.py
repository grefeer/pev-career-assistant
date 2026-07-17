"""API routes for resume draft operations."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, get_current_user, get_object_store
from backend.app.api.draft_schemas import (
    ApproveDraftRequest,
    ApprovedResumeVersionResponse,
    AttachmentResponse,
    CreateDraftRequest,
    RejectDraftRequest,
    ResumeDraftListResponse,
    ResumeDraftResponse,
)
from backend.app.db.models import ApprovedResumeAttachment, JobPosting, User
from backend.app.repositories.drafts import StaleDraftVersionError
from backend.app.services.attachment_service import download_attachment
from backend.app.services.resume_draft_service import ResumeDraftService
from backend.app.services.storage import EncryptedObjectStore

router = APIRouter(tags=["resume_drafts"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_draft_service(request: Request) -> ResumeDraftService:
    return request.app.state.draft_service


# ---------------------------------------------------------------------------
# POST /api/resume-drafts
# ---------------------------------------------------------------------------


@router.post("/resume-drafts", response_model=ResumeDraftResponse, status_code=201)
def create_draft(
    req: CreateDraftRequest,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    draft_service: Annotated[ResumeDraftService, Depends(get_draft_service)],
) -> ResumeDraftResponse:
    """Create a resume draft from a completed match report.

    Requires ``Idempotency-Key`` header.
    """
    try:
        draft = draft_service.create_draft(
            db=db,
            user_id=current_user.id,
            match_report_id=req.match_report_id,
            idempotency_key=idempotency_key,
        )
    except ValueError as e:
        code = str(e)
        if code == "not_found":
            raise HTTPException(404, detail={"code": "not_found"})
        if code == "idempotency_key_conflict":
            raise HTTPException(409, detail={"code": code})
        raise HTTPException(422, detail={"code": code})

    return _to_draft_response(draft, db)


# ---------------------------------------------------------------------------
# GET /api/resume-drafts/{draft_id}
# ---------------------------------------------------------------------------


@router.get("/resume-drafts/{draft_id}", response_model=ResumeDraftResponse)
def get_draft(
    draft_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    draft_service: Annotated[ResumeDraftService, Depends(get_draft_service)],
) -> ResumeDraftResponse:
    """Get a single resume draft by id (user-scoped)."""
    draft = draft_service.repo.get_by_id(db, draft_id, current_user.id)
    if draft is None:
        raise HTTPException(404, detail={"code": "not_found"})
    return _to_draft_response(draft, db)


# ---------------------------------------------------------------------------
# GET /api/resume-drafts
# ---------------------------------------------------------------------------


@router.get("/resume-drafts", response_model=ResumeDraftListResponse)
def list_drafts(
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    draft_service: Annotated[ResumeDraftService, Depends(get_draft_service)],
) -> ResumeDraftListResponse:
    """List all resume drafts for the current user."""
    items = draft_service.repo.list_by_user(db, current_user.id)
    return ResumeDraftListResponse(
        items=[_to_draft_response(d, db) for d in items],
        total=len(items),
    )


# ---------------------------------------------------------------------------
# POST /api/resume-drafts/{draft_id}/approve
# ---------------------------------------------------------------------------


@router.post(
    "/resume-drafts/{draft_id}/approve",
    response_model=ApprovedResumeVersionResponse,
)
def approve_draft(
    draft_id: str,
    req: ApproveDraftRequest,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    draft_service: Annotated[ResumeDraftService, Depends(get_draft_service)],
    object_store: Annotated[EncryptedObjectStore, Depends(get_object_store)],
) -> ApprovedResumeVersionResponse:
    """Approve a resume draft.

    Generates and stores encrypted PDF / DOCX attachments.
    Requires ``Idempotency-Key`` header.
    """
    try:
        arv = draft_service.approve_draft(
            db=db,
            user_id=current_user.id,
            draft_id=draft_id,
            expected_version=req.expected_version,
            object_store=object_store,
            idempotency_key=idempotency_key,
        )
    except ValueError as e:
        code = str(e)
        if code == "not_found":
            raise HTTPException(404, detail={"code": "not_found"})
        raise HTTPException(422, detail={"code": code})
    except StaleDraftVersionError:
        raise HTTPException(409, detail={"code": "stale_version"})

    return _to_arv_response(arv, db)


# ---------------------------------------------------------------------------
# POST /api/resume-drafts/{draft_id}/reject
# ---------------------------------------------------------------------------


@router.post("/resume-drafts/{draft_id}/reject", response_model=ResumeDraftResponse)
def reject_draft(
    draft_id: str,
    req: RejectDraftRequest,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    draft_service: Annotated[ResumeDraftService, Depends(get_draft_service)],
) -> ResumeDraftResponse:
    """Reject a resume draft with optimistic locking."""
    try:
        draft = draft_service.reject_draft(
            db=db,
            user_id=current_user.id,
            draft_id=draft_id,
            expected_version=req.expected_version,
        )
    except ValueError as e:
        code = str(e)
        if code == "not_found":
            raise HTTPException(404, detail={"code": "not_found"})
        raise HTTPException(422, detail={"code": code})
    except StaleDraftVersionError:
        raise HTTPException(409, detail={"code": "stale_version"})

    return _to_draft_response(draft, db)


# ---------------------------------------------------------------------------
# GET /api/approved-resume-attachments/{attachment_id}/download
# ---------------------------------------------------------------------------


@router.get("/approved-resume-attachments/{attachment_id}/download")
def download_attachment_endpoint(
    attachment_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    object_store: Annotated[EncryptedObjectStore, Depends(get_object_store)],
) -> Response:
    """Download a decrypted approved resume attachment."""
    try:
        body, content_type, filename = download_attachment(
            db=db,
            attachment_id=attachment_id,
            user_id=current_user.id,
            object_store=object_store,
        )
    except FileNotFoundError:
        raise HTTPException(404, detail={"code": "not_found"})
    except PermissionError:
        raise HTTPException(403, detail={"code": "forbidden"})

    return Response(
        content=body,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _to_draft_response(
    draft,
    db: Session | None = None,
) -> ResumeDraftResponse:
    """Convert a ResumeDraft ORM instance to the API response schema."""
    job_title = ""
    company_name = ""
    if db is not None and draft.target_job_id:
        job = (
            db.query(JobPosting)
            .filter(JobPosting.id == draft.target_job_id)
            .first()
        )
        if job:
            job_title = job.title
            company_name = job.company_name

    return ResumeDraftResponse(
        id=draft.id,
        match_report_id=draft.match_report_id,
        job_title=job_title,
        company_name=company_name,
        diffs=draft.diffs if draft.diffs else None,
        status=draft.status,
        error_code=draft.error_code,
        state_version=draft.state_version,
        created_at=str(draft.created_at),
        approved_at=str(draft.approved_at) if draft.approved_at else None,
    )


def _to_arv_response(
    arv,
    db: Session | None = None,
) -> ApprovedResumeVersionResponse:
    """Convert an ApprovedResumeVersion ORM instance to the API response schema."""
    attachments: list[AttachmentResponse] = []
    if db is not None:
        att_rows = (
            db.query(ApprovedResumeAttachment)
            .filter(
                ApprovedResumeAttachment.approved_resume_version_id == arv.id,
            )
            .all()
        )
        for att in att_rows:
            attachments.append(
                AttachmentResponse(
                    id=att.id,
                    format=att.format,
                    content_type=att.content_type,
                    plaintext_size=att.plaintext_size,
                )
            )

    return ApprovedResumeVersionResponse(
        id=arv.id,
        draft_id=arv.draft_id,
        approved_at=str(arv.approved_at),
        attachments=attachments,
    )
