from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, get_current_user, get_object_store
from backend.app.api.profile_schemas import (
    ApplyEvidenceDecisionsRequest,
    CreateProfileVersionRequest,
    EvidenceResponse,
    LocalSensitiveReferenceRequest,
    ProfileResponse,
    ProfileVersionDetail,
    ProfileVersionSummary,
    ResumeAssetResponse,
    ResumeImportResponse,
)
from backend.app.db.models import User
from backend.app.domain.profiles import (
    LocalSensitiveReferenceError,
    UnsupportedResumeTypeError,
)
from backend.app.services.profiles import (
    MAX_RESUME_BYTES,
    EvidenceDecisionInput,
    ObjectStoreUnavailableError,
    OwnedProfileResourceNotFound,
    ProfileService,
    ProfileValidationError,
    ProfileDependencyUnavailable,
    ResumeAssetService,
    ResumeAssetStateConflict,
    ResumeImportService,
    ResumeTooLargeError,
    StaleProfileVersionError,
)
from backend.app.repositories import profiles as profile_repository
from backend.app.services.storage import EncryptedObjectStore


router = APIRouter(tags=["profiles"])


def _profile_http_error(error: Exception) -> HTTPException:
    if isinstance(error, OwnedProfileResourceNotFound):
        return HTTPException(404, detail={"code": error.error_code, "message": "资源不存在。"})
    if isinstance(error, StaleProfileVersionError):
        return HTTPException(
            409,
            detail={
                "code": error.error_code,
                "message": "档案版本已变化，请重新加载。",
            },
        )
    if isinstance(error, ResumeAssetStateConflict):
        return HTTPException(
            409,
            detail={
                "code": error.error_code,
                "message": "当前资产状态不允许该操作。",
            },
        )
    if isinstance(
        error,
        (ProfileValidationError, LocalSensitiveReferenceError, UnsupportedResumeTypeError),
    ):
        return HTTPException(
            422, detail={"code": error.error_code, "message": "档案操作不合法。"}
        )
    if isinstance(error, ProfileDependencyUnavailable):
        return HTTPException(
            503, detail={"code": error.error_code, "message": "档案依赖暂不可用。"}
        )
    raise error


def _build_evidence_response(
    db: Any,
    profile_id: str,
    evidence_rows: Any,
    latest_snapshot: dict[str, Any] | None = None,
) -> list[EvidenceResponse]:
    decisions = profile_repository.latest_decisions_by_evidence(db, profile_id)
    evidence_list = list(evidence_rows)
    diff_map = profile_repository.compute_evidence_diff(evidence_list, latest_snapshot)
    result: list[EvidenceResponse] = []
    for ev in evidence_list:
        decision = decisions.get(ev.id)
        status = "pending"
        if decision:
            status = decision.action.value if hasattr(decision.action, "value") else str(decision.action)
        result.append(
            EvidenceResponse.from_orm_model(
                ev,
                status=status,
                diff_action=diff_map.get(ev.field_path, {}).value
                if isinstance(diff_map.get(ev.field_path), object)
                else diff_map.get(ev.field_path),
            )
        )
    return result


# --- Resume Assets ---


@router.post("/resume-assets", status_code=201)
def upload_resume_asset(
    file: UploadFile,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    object_store: Annotated[EncryptedObjectStore, Depends(get_object_store)],
) -> dict[str, Any]:
    raw = file.file.read(MAX_RESUME_BYTES + 1)
    if len(raw) > MAX_RESUME_BYTES:
        raise _profile_http_error(ResumeTooLargeError("resume exceeds 10 MiB limit"))

    content_type = file.content_type or "application/octet-stream"
    service = ResumeAssetService(object_store)
    try:
        asset = service.create_pending_asset(
            db,
            user_id=current_user.id,
            filename=file.filename or "resume",
            content_type=content_type,
            raw=raw,
        )
        db.commit()
        service.write_encrypted_object(asset, raw)
        service.mark_ready(db, asset=asset)
        db.commit()
    except (ResumeTooLargeError, UnsupportedResumeTypeError, ProfileValidationError) as exc:
        db.rollback()
        raise _profile_http_error(exc) from exc
    except ObjectStoreUnavailableError as exc:
        try:
            service.mark_upload_failed(db, asset=asset, error_code="object_store_unavailable")
            db.commit()
        except Exception:
            db.rollback()
        raise _profile_http_error(exc) from exc
    except Exception:
        db.rollback()
        raise

    return ResumeAssetResponse.from_orm_model(asset).model_dump()


@router.get("/resume-assets")
def list_resume_assets(
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    assets = profile_repository.list_owned_assets(db, current_user.id)
    return {
        "assets": [
            ResumeAssetResponse.from_orm_model(a).model_dump() for a in assets
        ]
    }


@router.get("/resume-assets/{asset_id}")
def get_resume_asset(
    asset_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    asset = profile_repository.get_owned_asset(db, current_user.id, asset_id)
    if asset is None:
        raise HTTPException(404)
    return ResumeAssetResponse.from_orm_model(asset).model_dump()


@router.get("/resume-assets/{asset_id}/download")
def download_resume_asset(
    asset_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    object_store: Annotated[EncryptedObjectStore, Depends(get_object_store)],
) -> StreamingResponse:
    asset = profile_repository.get_owned_asset(db, current_user.id, asset_id)
    if asset is None:
        raise HTTPException(404)
    try:
        plaintext = object_store.get(key=asset.object_key)
    except Exception as exc:
        raise _profile_http_error(ObjectStoreUnavailableError("object store read failed")) from exc
    filename_part = asset.original_filename
    return StreamingResponse(
        iter([plaintext]),
        media_type=asset.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename_part}"',
        },
    )


@router.post("/resume-assets/{asset_id}/reconcile")
def reconcile_resume_asset(
    asset_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    object_store: Annotated[EncryptedObjectStore, Depends(get_object_store)],
) -> dict[str, Any]:
    service = ResumeAssetService(object_store)
    try:
        asset = service.reconcile(db, user_id=current_user.id, asset_id=asset_id)
        db.commit()
    except (OwnedProfileResourceNotFound, ResumeAssetStateConflict) as exc:
        db.rollback()
        raise _profile_http_error(exc) from exc
    return ResumeAssetResponse.from_orm_model(asset).model_dump()


# --- Resume Imports ---


@router.post("/resume-imports", status_code=201)
def create_resume_import(
    body: dict[str, Any],
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    object_store: Annotated[EncryptedObjectStore, Depends(get_object_store)],
) -> dict[str, Any]:
    from backend.app.api.profile_schemas import CreateResumeImportRequest

    try:
        parsed = CreateResumeImportRequest(**body)
    except Exception as exc:
        raise HTTPException(422, detail=str(exc)) from exc

    import_service = ResumeImportService(object_store)
    try:
        import_row = import_service.start(
            db, user_id=current_user.id, asset_id=parsed.asset_id
        )
        db.commit()
        import_service.process(db, user_id=current_user.id, import_id=import_row.id)
        db.commit()
    except (OwnedProfileResourceNotFound, ResumeAssetStateConflict) as exc:
        db.rollback()
        raise _profile_http_error(exc) from exc

    return ResumeImportResponse.from_orm_model(import_row).model_dump()


@router.get("/resume-imports/{import_id}")
def get_resume_import(
    import_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    import_row = profile_repository.get_owned_import(db, current_user.id, import_id)
    if import_row is None:
        raise HTTPException(404)
    return ResumeImportResponse.from_orm_model(import_row).model_dump()


# --- Profile Evidence ---


@router.get("/profiles")
def get_profile(
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    profile = profile_repository.ensure_profile(db, current_user.id)
    all_evidence = profile_repository.get_profile_evidence_with_decisions(
        db, profile.id
    )

    # Get latest version for diff
    versions = profile_repository.list_versions(db, current_user.id)
    latest_snapshot = versions[0].facts_snapshot if versions else None

    evidence = _build_evidence_response(
        db, profile.id, all_evidence, latest_snapshot
    )
    latest_version = versions[0] if versions else None
    return ProfileResponse.from_profile(
        profile, evidence=evidence, latest_version=latest_version
    ).model_dump()


@router.patch("/profiles/evidence")
def apply_evidence_decisions(
    body: dict[str, Any],
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:

    try:
        parsed = ApplyEvidenceDecisionsRequest(**body)
    except Exception as exc:
        raise HTTPException(422, detail=str(exc)) from exc

    service = ProfileService()
    try:
        decisions = tuple(
            EvidenceDecisionInput(
                evidence_id=d.evidence_id,
                action=d.action,
                corrected_value=d.corrected_value,
            )
            for d in parsed.decisions
        )
        profile = service.apply_decisions(
            db,
            user_id=current_user.id,
            expected_version=parsed.expected_version,
            decisions=decisions,
        )
        db.commit()
    except (StaleProfileVersionError, OwnedProfileResourceNotFound) as exc:
        db.rollback()
        raise _profile_http_error(exc) from exc

    return {"version": profile.version}


@router.patch("/profiles/local-sensitive-references")
def update_local_sensitive_references(
    body: dict[str, Any],
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:

    try:
        parsed = LocalSensitiveReferenceRequest(**body)
    except Exception as exc:
        raise HTTPException(422, detail=str(exc)) from exc

    service = ProfileService()
    try:
        profile = service.update_local_sensitive_reference(
            db,
            user_id=current_user.id,
            expected_version=parsed.expected_version,
            category=parsed.category,
            reference=parsed.reference,
        )
        db.commit()
    except (StaleProfileVersionError, LocalSensitiveReferenceError) as exc:
        db.rollback()
        raise _profile_http_error(exc) from exc

    return {"version": profile.version}


# --- Profile Versions ---


@router.post("/profile-versions", status_code=201)
def create_profile_version(
    body: dict[str, Any],
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:

    try:
        parsed = CreateProfileVersionRequest(**body)
    except Exception as exc:
        raise HTTPException(422, detail=str(exc)) from exc

    service = ProfileService()
    try:
        confirmed = service.create_confirmed_version(
            db,
            user_id=current_user.id,
            expected_version=parsed.expected_version,
            resume_import_id=parsed.resume_import_id,
        )
        db.commit()
    except (StaleProfileVersionError, OwnedProfileResourceNotFound, ProfileValidationError) as exc:
        db.rollback()
        raise _profile_http_error(exc) from exc

    return ProfileVersionDetail.from_orm_model(confirmed).model_dump()


@router.get("/profile-versions")
def list_profile_versions(
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    versions = profile_repository.list_versions(db, current_user.id)
    return {
        "versions": [
            ProfileVersionSummary.from_orm_model(v).model_dump()
            for v in versions
        ]
    }


@router.get("/profile-versions/{version_id}")
def get_profile_version(
    version_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    version = profile_repository.get_owned_version(db, current_user.id, version_id)
    if version is None:
        raise HTTPException(404)
    return ProfileVersionDetail.from_orm_model(version).model_dump()
