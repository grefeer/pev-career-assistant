from pydantic import BaseModel, Field
from typing import Literal


class CreateMatchRequest(BaseModel):
    job_id: str = Field(min_length=1)
    profile_version_id: str = Field(min_length=1)
    analysis_session_id: str | None = None


class RequirementAssessmentResponse(BaseModel):
    requirement_id: str
    requirement: str
    job_field_path: str
    profile_field_path: str | None
    verdict: Literal["satisfied", "gap", "unknown"]
    evidence_ids: list[str]
    detail: str


class ScoreComponentResponse(BaseModel):
    requirement_id: str
    weight_basis_points: int
    earned_basis_points: int


class MatchReportResponse(BaseModel):
    id: str
    analysis_session_id: str
    job_id: str
    profile_version_id: str
    status: Literal["pending", "running", "completed", "failed"]
    score: int | None
    score_components: list[ScoreComponentResponse] | None
    strengths: list[RequirementAssessmentResponse] | None
    gaps: list[RequirementAssessmentResponse] | None
    unknowns: list[RequirementAssessmentResponse] | None
    risks: list[RequirementAssessmentResponse] | None
    application_priority: str | None
    recommendation: dict | None
    error_code: str | None
    scoring_rule_version: str
    model_version: str
    prompt_version: str
    output_schema_version: str
    created_at: str
    started_at: str | None
    completed_at: str | None


class MatchReportListResponse(BaseModel):
    items: list[MatchReportResponse]
    total: int
