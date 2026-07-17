from pydantic import BaseModel, Field
from typing import Literal, Optional


class RequirementAssessment(BaseModel):
    requirement_id: str = Field(description="Stable requirement ID from preprocessor")
    requirement: str = Field(description="Original job requirement text")
    job_field_path: str = Field(description="Field path in VerifiedJobSnapshot")
    profile_field_path: Optional[str] = Field(default=None, description="Field path in ConfirmedProfileSnapshot, null for gap/unknown")
    verdict: Literal["satisfied", "gap", "unknown"]
    evidence_ids: list[str] = Field(default_factory=list)
    detail: str = Field(description="Assessment explanation")


class ReferencedRecommendation(BaseModel):
    text: str
    requirement_ids: list[str] = Field(default_factory=list)


class ReferencedRisk(BaseModel):
    requirement_ids: list[str] = Field(
        default_factory=list, description="Risk references to requirement IDs"
    )
    requirement: str = Field(description="Risk description text")
    detail: str = Field(default="", description="Risk detail explanation")


class MatchComputationOutput(BaseModel):
    strengths: list[RequirementAssessment] = Field(default_factory=list)
    gaps: list[RequirementAssessment] = Field(default_factory=list)
    unknowns: list[RequirementAssessment] = Field(default_factory=list)
    risks: list[ReferencedRisk] = Field(default_factory=list)
    recommendation: ReferencedRecommendation


class EvidenceMatchingState(BaseModel):
    job_snapshot: dict = Field(default_factory=dict)
    profile_snapshot: dict = Field(default_factory=dict)
    job_requirements: list[dict] = Field(default_factory=list)
    assessments: list[dict] = Field(default_factory=list)
    result: Optional[MatchComputationOutput] = None
    next_step: str = "extract_requirements"
    error: Optional[str] = None
