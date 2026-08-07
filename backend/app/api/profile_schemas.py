from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.domain.profiles import EvidenceDecisionAction


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --- Request schemas ---


class CreateResumeImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str = Field(min_length=36, max_length=36)


class EvidenceDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str = Field(min_length=36, max_length=36)
    action: EvidenceDecisionAction
    corrected_value: Any | None = None


class ApplyEvidenceDecisionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    decisions: list[EvidenceDecisionRequest] = Field(min_length=1, max_length=200)


class CreateProfileVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    resume_import_id: str = Field(min_length=36, max_length=36)


class LocalSensitiveReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    category: Literal["government_id", "family_member", "emergency_contact"]
    reference: str = Field(pattern=r"^lsr:v1:[0-9a-f]{64}$")


# --- Response schemas ---


class ResumeAssetResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    original_filename: str
    content_type: str
    plaintext_size: int
    encryption_version: str
    status: str
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_model(cls, obj: Any) -> ResumeAssetResponse:
        return cls(
            id=obj.id,
            original_filename=obj.original_filename,
            content_type=obj.content_type,
            plaintext_size=obj.plaintext_size,
            encryption_version=obj.encryption_version,
            status=obj.status.value if hasattr(obj.status, "value") else str(obj.status),
            error_code=obj.error_code,
            created_at=_normalize_utc(obj.created_at),
            updated_at=_normalize_utc(obj.updated_at),
        )


class ResumeImportResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    asset_id: str
    parser_version: str
    status: str
    error_code: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_model(cls, obj: Any) -> ResumeImportResponse:
        return cls(
            id=obj.id,
            asset_id=obj.asset_id,
            parser_version=obj.parser_version,
            status=obj.status.value if hasattr(obj.status, "value") else str(obj.status),
            error_code=obj.error_code,
            started_at=_normalize_utc(obj.started_at) if obj.started_at else None,
            finished_at=_normalize_utc(obj.finished_at) if obj.finished_at else None,
            created_at=_normalize_utc(obj.created_at),
            updated_at=_normalize_utc(obj.updated_at),
        )


class EvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    resume_import_id: str
    field_path: str
    candidate_value: Any
    evidence_excerpt: str
    confidence: int
    status: str = "pending"
    diff_action: str | None = None
    corrected_value: Any | None = None

    @classmethod
    def from_orm_model(
        cls,
        obj: Any,
        *,
        status: str = "pending",
        diff_action: str | None = None,
        corrected_value: Any | None = None,
    ) -> EvidenceResponse:
        return cls(
            id=obj.id,
            resume_import_id=obj.resume_import_id,
            field_path=obj.field_path,
            candidate_value=obj.candidate_value,
            evidence_excerpt=obj.evidence_excerpt,
            confidence=obj.confidence,
            status=status,
            diff_action=diff_action,
            corrected_value=corrected_value,
        )


class ProfileResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    version: int
    evidence: list[EvidenceResponse] = []
    local_sensitive_references: dict[str, Any] = {}
    latest_version: dict[str, Any] | None = None
    active_version_id: str | None = None

    @classmethod
    def from_profile(
        cls,
        profile: Any,
        *,
        evidence: list[EvidenceResponse] | None = None,
        latest_version: Any = None,
    ) -> ProfileResponse:
        return cls(
            id=profile.id,
            version=profile.version,
            evidence=evidence or [],
            local_sensitive_references=profile.local_sensitive_references,
            latest_version={
                "id": latest_version.id,
                "version_number": latest_version.version_number,
                "created_at": _normalize_utc(latest_version.created_at).isoformat(),
            }
            if latest_version
            else None,
            active_version_id=profile.active_version_id,
        )


class ProfileVersionSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    version_number: int
    aggregate_version: int
    created_at: datetime

    @classmethod
    def from_orm_model(cls, obj: Any) -> ProfileVersionSummary:
        return cls(
            id=obj.id,
            version_number=obj.version_number,
            aggregate_version=obj.aggregate_version,
            created_at=_normalize_utc(obj.created_at),
        )


class ProfileVersionDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    version_number: int
    aggregate_version: int
    facts_snapshot: dict[str, Any]
    evidence_refs: dict[str, Any]
    local_sensitive_references: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_orm_model(cls, obj: Any) -> ProfileVersionDetail:
        return cls(
            id=obj.id,
            version_number=obj.version_number,
            aggregate_version=obj.aggregate_version,
            facts_snapshot=obj.facts_snapshot,
            evidence_refs=obj.evidence_refs,
            local_sensitive_references=obj.local_sensitive_references,
            created_at=_normalize_utc(obj.created_at),
        )
