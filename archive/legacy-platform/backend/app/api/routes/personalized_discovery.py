"""Authenticated HTTP handlers for personalized job discovery v1.

Routes are thin: parse request -> call the service -> return a strict DTO. No
SQL, no business logic, and no user-id input anywhere - the caller is always
``current_user``. A missing or not-owned recommendation is 404 (existence is
never leaked).

This channel is PRE-REVIEW and separate from the verified-only ``/jobs``
path: it never reads or mutates ``JobPosting``, ``JobRelevanceScore``, or
``review_version``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, get_current_user
from backend.app.api.personalized_discovery_schemas import (
    AUTO_DISCOVERY_LABEL,
    EvidenceLinkResponse,
    InteractionRequest,
    PreferencesResponse,
    PreferencesUpdateRequest,
    RecommendationCardResponse,
    RecommendationListResponse,
    RunCreateRequest,
    RunResponse,
    RunSummary,
    SourceStatusListResponse,
    SourceStatusResponse,
)
from backend.app.db.models import User
from backend.app.domain.personalized_discovery import (
    RecommendationPresentationState,
)
from backend.app.services.personalized_discovery import (
    PersonalizedDiscoveryError,
    PersonalizedDiscoveryRateLimitError,
    PersonalizedDiscoveryService,
)

router = APIRouter(prefix="/personalized-discovery", tags=["personalized-discovery"])


def get_personalized_discovery_service(
    request: Request,
) -> PersonalizedDiscoveryService:
    """Injected in tests; in production a service is wired on app.state.

    Falls back to constructing one with the real batched relevance ranker so
    the endpoint works without a main.py wiring change.
    """
    injected = getattr(request.app.state, "personalized_discovery_service", None)
    if injected is not None:
        return injected
    settings = request.app.state.settings
    from backend.app.services.relevance import build_relevance_llm
    from backend.app.services.relevance.relevance_ranker import RelevanceRanker

    ranker = RelevanceRanker(
        build_relevance_llm(settings), batch_size=settings.relevance_batch_size
    )
    return PersonalizedDiscoveryService(ranker, settings=settings)


def _interaction_state(value: str) -> RecommendationPresentationState:
    try:
        return RecommendationPresentationState(value)
    except ValueError as exc:  # pragma: no cover - FastAPI enum routing guards this
        raise HTTPException(status_code=422, detail="state 不合法。") from exc


# ─── Preferences ─────────────────────────────────────────────────────────────


@router.get("/preferences")
def get_preferences(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[PersonalizedDiscoveryService, Depends(get_personalized_discovery_service)],
) -> PreferencesResponse:
    view = service.get_preferences(db, user_id=current_user.id)
    return _to_preferences_response(view)


@router.patch("/preferences")
def patch_preferences(
    payload: PreferencesUpdateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[PersonalizedDiscoveryService, Depends(get_personalized_discovery_service)],
) -> PreferencesResponse:
    try:
        view = service.update_preferences(
            db,
            user_id=current_user.id,
            desired_roles=payload.desired_roles,
            role_synonyms=payload.role_synonyms,
            excluded_roles=payload.excluded_roles,
            personalized_discovery_min_score=payload.personalized_discovery_min_score,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"偏好不合法：{exc}。"
        ) from exc
    db.commit()
    return _to_preferences_response(view)


@router.delete("/preferences", status_code=status.HTTP_204_NO_CONTENT)
def delete_preferences(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[PersonalizedDiscoveryService, Depends(get_personalized_discovery_service)],
) -> None:
    service.clear_preferences(db, user_id=current_user.id)
    db.commit()


def _to_preferences_response(view) -> PreferencesResponse:
    return PreferencesResponse(
        desired_roles=view.desired_roles,
        role_synonyms=view.role_synonyms,
        excluded_roles=view.excluded_roles,
        personalized_discovery_min_score=view.personalized_discovery_min_score,
        version=view.version,
    )


# ─── Runs ────────────────────────────────────────────────────────────────────


@router.post("/runs", status_code=status.HTTP_201_CREATED)
def create_run(
    _payload: RunCreateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[PersonalizedDiscoveryService, Depends(get_personalized_discovery_service)],
) -> RunResponse:
    try:
        run = service.run(
            db,
            user_id=current_user.id,
            now=datetime.now(timezone.utc),
        )
    except PersonalizedDiscoveryRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "rate_limited", "message": "今日发现次数已达上限。"},
        ) from exc
    except PersonalizedDiscoveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="个性化发现失败。",
        ) from exc
    db.commit()
    return _to_run_response(run)


def _to_run_response(run) -> RunResponse:
    summary = run.summary_json or {}
    return RunResponse(
        id=run.id,
        status=run.status,
        preference_version=int(run.preference_version or 0),
        started_at=run.started_at,
        finished_at=run.finished_at,
        summary=RunSummary(
            task_count=int(summary.get("task_count") or 0),
            status_count=int(summary.get("status_count") or 0),
            candidate_pool=int(summary.get("candidate_pool") or 0),
            recommendation_count=int(summary.get("recommendation_count") or 0),
            statuses=list(summary.get("statuses") or []),
        ),
    )


# ─── Recommendations ─────────────────────────────────────────────────────────


@router.get("/recommendations")
def list_recommendations(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[PersonalizedDiscoveryService, Depends(get_personalized_discovery_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RecommendationListResponse:
    views = service.list_recommendations(
        db, user_id=current_user.id, limit=limit, offset=offset
    )
    items = [_to_card_response(v) for v in views]
    return RecommendationListResponse(items=items, total=len(items))


@router.post("/recommendations/{recommendation_id}/interactions")
def record_interaction(
    recommendation_id: str,
    payload: InteractionRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[PersonalizedDiscoveryService, Depends(get_personalized_discovery_service)],
) -> RecommendationCardResponse:
    view = service.record_interaction(
        db,
        user_id=current_user.id,
        recommendation_id=recommendation_id,
        state=_interaction_state(payload.state),
    )
    if view is None:
        # Missing or not-owned: 404 without leaking existence.
        raise HTTPException(status_code=404, detail="推荐不存在。")
    db.commit()
    return _to_card_response(view)


def _to_card_response(view) -> RecommendationCardResponse:
    return RecommendationCardResponse(
        id=view.id,
        title=view.title,
        company=view.company_name,
        locations=list(view.locations),
        apply_url=view.apply_url,
        score=view.relevance_score,
        reason=view.relevance_reason,
        signals=list(view.matched_signals),
        evidence_links=[
            EvidenceLinkResponse(url=el.url, evidence_type=el.evidence_type)
            for el in view.evidence_links
        ],
        label=AUTO_DISCOVERY_LABEL,
        state=view.presentation_state,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


# ─── Source statuses ─────────────────────────────────────────────────────────


@router.get("/source-statuses")
def list_source_statuses(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[PersonalizedDiscoveryService, Depends(get_personalized_discovery_service)],
    run_id: Annotated[str, Query(min_length=1, max_length=36)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SourceStatusListResponse:
    views = service.list_source_statuses(
        db, user_id=current_user.id, run_id=run_id, limit=limit, offset=offset
    )
    items = [
        SourceStatusResponse(
            id=v.id,
            run_id=v.run_id,
            task_id=v.task_id,
            source_key=v.source_key,
            safe_source_url=v.safe_source_url,
            reason_code=v.reason_code,
            display_text=v.display_text,
            retry_guidance=v.retry_guidance,
            created_at=v.created_at,
        )
        for v in views
    ]
    return SourceStatusListResponse(items=items, total=len(items))
