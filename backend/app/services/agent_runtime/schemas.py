"""Validated data exchanged between the three autonomous PEV roles.

These schemas intentionally describe decisions and observations rather than a
pre-scripted workflow.  The harness may enforce their bounds, but only the
Agents choose a plan, a permitted Skill, and a verifier outcome.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel

_SKILL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class AgentBudget(BaseModel):
    """Hard per-run ceilings enforced by the runtime, not a business decision."""

    model_config = {"extra": "forbid"}

    max_agent_turns: int = Field(default=12, ge=1, le=100)
    max_tool_calls: int = Field(default=24, ge=1, le=200)
    max_replans: int = Field(default=2, ge=0, le=10)


class AgentTaskRequest(BaseModel):
    """User-scoped objective and the explicit Skill authority for one run."""

    model_config = {"extra": "forbid"}

    goal: str = Field(min_length=1, max_length=8_000)
    allowed_skills: list[str] = Field(min_length=1, max_length=16)
    context: dict[str, Any] = Field(default_factory=dict)
    budget: AgentBudget = Field(default_factory=AgentBudget)

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("goal must not be empty")
        return cleaned

    @field_validator("allowed_skills")
    @classmethod
    def validate_allowed_skills(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if not cleaned or any(not value for value in cleaned):
            raise ValueError("allowed_skills must contain non-empty Skill names")
        if any(not _SKILL_NAME.fullmatch(value) for value in cleaned):
            raise ValueError("allowed_skills contains an invalid Skill name")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("allowed_skills must be unique")
        return cleaned


class PlanStep(BaseModel):
    """One Planner-created outcome, not a harness-selected tool invocation."""

    model_config = {"extra": "forbid"}

    step_id: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=2_000)
    allowed_skills: list[str] = Field(min_length=1, max_length=8)
    success_criteria: list[str] = Field(default_factory=list, max_length=12)
    requires_verification: bool = False

    @field_validator("step_id", "objective")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("plan step fields must not be empty")
        return cleaned

    @field_validator("allowed_skills")
    @classmethod
    def validate_step_skills(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if not cleaned or any(not _SKILL_NAME.fullmatch(value) for value in cleaned):
            raise ValueError("plan step contains an invalid Skill name")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("plan step Skill names must be unique")
        return cleaned

    @field_validator("success_criteria")
    @classmethod
    def normalize_success_criteria(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if len(cleaned) != len(values):
            raise ValueError("success criteria must not be empty")
        return cleaned


class ExecutionPlan(BaseModel):
    """Structured Planner result constrained by the run's original authority."""

    model_config = {"extra": "forbid"}

    task: AgentTaskRequest
    created_by: AgentRole
    complexity: ComplexityLevel
    success_criteria: list[str] = Field(min_length=1, max_length=20)
    steps: list[PlanStep] = Field(min_length=1, max_length=20)

    @field_validator("success_criteria")
    @classmethod
    def validate_plan_success_criteria(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("success criteria must not be empty")
        return cleaned

    @model_validator(mode="after")
    def validate_plan_authority(self) -> "ExecutionPlan":
        if self.created_by is not AgentRole.planner:
            raise ValueError("execution plans must be created by the planner")
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("plan step_id values must be unique")
        permitted_skills = set(self.task.allowed_skills)
        for step in self.steps:
            forbidden = set(step.allowed_skills) - permitted_skills
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise ValueError(f"plan Skill is not allowed for this task: {names}")
        return self
