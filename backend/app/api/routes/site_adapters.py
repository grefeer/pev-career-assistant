"""Admin API for site adapter management.

Provides endpoints to list adapters, inspect their status, and reset
circuit breakers.  All endpoints require admin authentication.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, require_admin
from backend.app.db.models import SiteAdapter as SiteAdapterModel

router = APIRouter(prefix="/admin/site-adapters", tags=["admin-site-adapters"])


# ── Response DTOs ──────────────────────────────────────────────────────────────


class SiteAdapterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    adapter_id: str
    version: str
    supported_domains: list[str]
    status: str
    error_count: int
    circuit_breaker_open: bool
    last_error_at: datetime | None = None
    last_error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class SiteAdapterListResponse(BaseModel):
    adapters: list[SiteAdapterResponse]


class CircuitBreakerResetRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.get("")
def list_adapters(
    db: Annotated[Session, Depends(_get_db)],
    _admin=Depends(require_admin),
) -> SiteAdapterListResponse:
    """List all registered site adapters."""
    adapters = (
        db.query(SiteAdapterModel)
        .order_by(SiteAdapterModel.adapter_id)
        .all()
    )
    return SiteAdapterListResponse(
        adapters=[SiteAdapterResponse.model_validate(a) for a in adapters]
    )


@router.get("/{adapter_id}")
def get_adapter(
    adapter_id: str,
    db: Annotated[Session, Depends(_get_db)],
    _admin=Depends(require_admin),
) -> SiteAdapterResponse:
    """Get a single site adapter by its adapter_id."""
    adapter = (
        db.query(SiteAdapterModel)
        .filter(SiteAdapterModel.adapter_id == adapter_id)
        .first()
    )
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="site_adapter_not_found",
        )
    return SiteAdapterResponse.model_validate(adapter)


@router.post("/{adapter_id}/circuit-breaker/reset")
def reset_circuit_breaker(
    adapter_id: str,
    req: CircuitBreakerResetRequest,
    db: Annotated[Session, Depends(_get_db)],
    _admin=Depends(require_admin),
) -> SiteAdapterResponse:
    """Reset the circuit breaker for a site adapter."""
    adapter = (
        db.query(SiteAdapterModel)
        .filter(SiteAdapterModel.adapter_id == adapter_id)
        .first()
    )
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="site_adapter_not_found",
        )

    adapter.circuit_breaker_open = False
    adapter.error_count = 0
    adapter.last_error_at = None
    adapter.last_error_code = None
    adapter.updated_at = datetime.now(timezone.utc)
    db.commit()

    return SiteAdapterResponse.model_validate(adapter)
