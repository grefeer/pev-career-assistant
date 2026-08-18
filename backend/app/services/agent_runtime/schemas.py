"""Validated data exchanged between the three autonomous PEV roles.

These schemas intentionally describe decisions and observations rather than a
pre-scripted workflow.  The harness may enforce their bounds, but only the
Agents choose a plan, a permitted Skill, and a verifier outcome.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, RootModel, field_validator, model_validator

from backend.app.domain.agent_runtime import (
    AgentRole,
    ComplexityLevel,
    VerificationDecision,
)

_SKILL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class ReplanReason(StrEnum):
    """Machine-readable reason for a bounded planner re-entry."""

    VERIFIER_REPLAN = "verifier_replan"
    TOOL_SCOPE_CONFLICT = "tool_scope_conflict"
    RETRY_CONTRACT_EXHAUSTED = "retry_contract_exhausted"
    NEED_USER_CONTRACT = "need_user_contract"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"


class ReplanState(BaseModel):
    """Validated control state for one run's planner re-entry decisions.

    Human-readable feedback remains in ``context.verifier_feedback``.  This
    object owns only the machine state that used to be encoded by sentinel
    strings, so prompt wording can never accidentally change control flow.
    """

    model_config = {"extra": "forbid"}

    count: int = Field(default=0, ge=0, le=10)
    source_revision: int | None = Field(default=None, ge=1)
    last_reason: ReplanReason | None = None
    last_feedback: str | None = Field(default=None, max_length=2_000)
    conversion_reasons: list[ReplanReason] = Field(default_factory=list, max_length=10)

    def conversion_used(self, reason: ReplanReason) -> bool:
        """Return whether a contract-driven conversion was already attempted."""
        return reason in self.conversion_reasons

    def requested(
        self,
        *,
        reason: ReplanReason,
        feedback: str | None,
        source_revision: int | None,
        count: int | None = None,
    ) -> "ReplanState":
        """Return the next immutable state after a replan request."""
        conversions = list(self.conversion_reasons)
        if reason in {
            ReplanReason.RETRY_CONTRACT_EXHAUSTED,
            ReplanReason.NEED_USER_CONTRACT,
        } and reason not in conversions:
            conversions.append(reason)
        return self.model_copy(
            update={
                "count": self.count + 1 if count is None else count,
                "source_revision": source_revision,
                "last_reason": reason,
                "last_feedback": feedback,
                "conversion_reasons": conversions,
            }
        )


class AgentBudget(BaseModel):
    """Hard per-run ceilings enforced by the runtime, not a business decision."""

    model_config = {"extra": "forbid"}

    max_agent_turns: int = Field(default=12, ge=1, le=100)
    max_tool_calls: int = Field(default=24, ge=1, le=200)
    max_replans: int = Field(default=2, ge=0, le=10)
    max_wall_clock_seconds: int = Field(default=300, ge=10, le=3_600)
    # Bounded automatic recovery rounds: when a run pauses as waiting_user for
    # a verifier/model-decision reason (never a source-access block), the
    # harness may resume the same run itself — with a step-up budget and a
    # relaxed stall breaker — up to this many times before handing back to
    # the human. 1 = first run + 1 automatic re-run (2 attempts total).
    max_auto_recoveries: int = Field(default=1, ge=0, le=5)
    # Physical model ceilings.  Turn count limits lifecycle decisions; these
    # limits bound provider requests and measured token consumption separately.
    max_model_requests: int = Field(default=128, ge=1, le=500)
    max_input_tokens: int = Field(default=1_000_000, ge=1_000, le=2_000_000)
    max_output_tokens: int = Field(default=200_000, ge=1_000, le=500_000)


class AgentTaskRequest(BaseModel):
    """User-scoped objective and the explicit Skill authority for one run."""

    model_config = {"extra": "forbid"}

    goal: str = Field(min_length=1, max_length=8_000)
    allowed_skills: list[str] = Field(min_length=1, max_length=16)
    context: dict[str, Any] = Field(default_factory=dict)
    private_context: dict[str, Any] = Field(default_factory=dict, exclude=True)
    # Cross-invocation executor state carried across verifier RETRY
    # re-invocations of the same step (succeeded-call dedup set + waste
    # counters). Excluded from serialization so it never enters model prompts
    # or persisted plan JSON.
    execution_state: dict[str, Any] = Field(default_factory=dict, exclude=True)
    replan_state: ReplanState = Field(default_factory=ReplanState, exclude=True)
    budget: AgentBudget = Field(default_factory=AgentBudget)

    @model_validator(mode="after")
    def load_typed_replan_state(self) -> "AgentTaskRequest":
        """Hydrate typed control state when a run is resumed from JSON context."""
        raw_state = self.context.get("replan_state")
        if raw_state is not None:
            object.__setattr__(self, "replan_state", ReplanState.model_validate(raw_state))
        return self

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


class StepInputRef(BaseModel):
    """Structured input reference; values are never copied through prose."""

    model_config = {"extra": "forbid"}

    kind: Literal["context", "artifact"]
    name: str = Field(min_length=1, max_length=100)
    from_step: str | None = Field(default=None, max_length=80)
    artifact_type: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_source(self) -> "StepInputRef":
        if self.kind == "artifact" and not self.from_step:
            raise ValueError("artifact inputs require from_step")
        if self.kind == "context" and self.from_step is not None:
            raise ValueError("context inputs must not specify from_step")
        if self.kind == "context" and self.artifact_type is not None:
            raise ValueError("context inputs must not specify artifact_type")
        return self


class StepOutputRef(BaseModel):
    """Named artifact output declared by a Planner-created step."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=100)
    artifact_type: str | None = Field(default=None, max_length=100)


class PlanStep(BaseModel):
    """One Planner-created outcome, not a harness-selected tool invocation."""

    model_config = {"extra": "forbid"}

    step_id: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=2_000)
    allowed_skills: list[str] = Field(min_length=1, max_length=8)
    success_criteria: list[str] = Field(default_factory=list, max_length=12)
    requires_verification: bool = False
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    inputs: list[StepInputRef] = Field(default_factory=list, max_length=20)
    outputs: list[StepOutputRef] = Field(default_factory=list, max_length=20)

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

    @field_validator("depends_on")
    @classmethod
    def validate_dependencies(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("depends_on must contain non-empty step IDs")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("depends_on must contain unique step IDs")
        return cleaned


# Goal phrasings over the user's already-collected jobs. Marker-keyed on
# purpose: a discovery goal for a named site/portal carries no such marker and
# stays legal as discovery-only.
_ALREADY_COLLECTED_MARKERS = ("已收集", "最近收集", "候选岗位", "这些岗位", "收集的岗位")


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
        step_id_set = set(step_ids)
        step_positions = {step_id: index for index, step_id in enumerate(step_ids)}
        for index, step in enumerate(self.steps):
            if step.step_id in step.depends_on:
                raise ValueError("a plan step cannot depend on itself")
            unknown_dependencies = set(step.depends_on) - step_id_set
            if unknown_dependencies:
                names = ", ".join(sorted(unknown_dependencies))
                raise ValueError(f"plan step depends on unknown step(s): {names}")
            if any(step_positions[dependency] >= index for dependency in step.depends_on):
                raise ValueError("plan step dependencies must refer to earlier steps")
            for input_ref in step.inputs:
                if input_ref.from_step and input_ref.from_step not in step_id_set:
                    raise ValueError(
                        f"plan input references unknown step: {input_ref.from_step}"
                    )
                if input_ref.from_step and input_ref.from_step not in step.depends_on:
                    raise ValueError(
                        "artifact input source must also appear in depends_on"
                    )
        permitted_skills = set(self.task.allowed_skills)
        for step in self.steps:
            forbidden = set(step.allowed_skills) - permitted_skills
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise ValueError(f"plan Skill is not allowed for this task: {names}")
        self._validate_collected_goal_deliverables()
        return self

    def _validate_collected_goal_deliverables(self) -> None:
        """Reject discovery-only plans for goals over user-supplied candidate URLs.

        A goal phrased over already-collected jobs (已收集/最近收集/候选岗位) with
        non-empty candidate_urls must produce at least one deliverable step: the
        Executor's open-web search is hard-blocked while candidate URLs exist, so
        a discovery-only plan can only re-capture the same pages and burn the
        turn budget without ever producing the requested ranking/tailoring/plan.
        """
        candidate_urls = self.task.context.get("candidate_urls")
        goal = self.task.goal or ""
        if not (
            isinstance(candidate_urls, list)
            and any(isinstance(url, str) and url.strip() for url in candidate_urls)
        ):
            return
        if not any(marker in goal for marker in _ALREADY_COLLECTED_MARKERS):
            return
        has_deliverable_step = any(
            "job-discovery" not in set(step.allowed_skills) for step in self.steps
        )
        if not has_deliverable_step:
            raise ValueError(
                "already-collected goals with candidate URLs require a deliverable "
                "step beyond job-discovery"
            )


class ToolObservation(BaseModel):
    """A validated tool result visible to an Agent's next autonomous turn."""

    model_config = {"extra": "forbid"}

    tool_name: str = Field(min_length=1, max_length=64)
    status: str
    output: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_outcome(self) -> "ToolObservation":
        if self.status == "succeeded" and self.output is None:
            raise ValueError("succeeded tool observations require output")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed tool observations require an error_code")
        if self.status not in {"succeeded", "failed"}:
            raise ValueError("tool observation status must be succeeded or failed")
        return self


class _DecisionAttributeProxy:
    """Keep the existing ``decision.action`` ergonomics over a RootModel."""

    def __getattr__(self, name: str) -> Any:
        try:
            root = object.__getattribute__(self, "root")
        except AttributeError as exc:  # pragma: no cover - defensive only
            raise AttributeError(name) from exc
        try:
            return getattr(root, name)
        except AttributeError as exc:
            raise AttributeError(name) from exc

    @property
    def tool_name(self) -> str | None:
        return getattr(self.root, "tool_name", None)

    @property
    def tool_input(self) -> dict[str, Any]:
        return getattr(self.root, "tool_input", {})

    @property
    def complexity(self) -> ComplexityLevel | None:
        return getattr(self.root, "complexity", None)

    @property
    def success_criteria(self) -> list[str]:
        return getattr(self.root, "success_criteria", [])

    @property
    def steps(self) -> list[PlanStep]:
        return getattr(self.root, "steps", [])

    @property
    def summary(self) -> str | None:
        return getattr(self.root, "summary", None)

    @property
    def artifact_refs(self) -> list[dict[str, Any]]:
        return getattr(self.root, "artifact_refs", [])

    @property
    def user_question(self) -> str | None:
        return getattr(self.root, "user_question", None)

    @property
    def verification_decision(self) -> VerificationDecision | None:
        return getattr(self.root, "verification_decision", None)

    @property
    def feedback(self) -> str | None:
        return getattr(self.root, "feedback", None)


class CallToolDecision(BaseModel):
    """Role-agnostic tool-call decision shared by Planner, Executor, Verifier.

    Stage 1.5 collapsed the three role-prefixed copies of this schema into a
    single canonical model. The role-prefixed names are preserved as
    type aliases so any external import keeps working.
    """

    model_config = {"extra": "forbid"}

    action: Literal["call_tool"]
    tool_name: str = Field(min_length=1)
    tool_input: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tool_name")
    @classmethod
    def normalize_tool_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("call_tool requires tool_name")
        return value


# Backward-compatible aliases (Stage 1.5). All three were byte-identical
# definitions; the canonical class is ``CallToolDecision``.
PlannerCallToolDecision = CallToolDecision
ExecutorCallToolDecision = CallToolDecision


class PlannerPlanDecision(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["plan"]
    complexity: ComplexityLevel
    success_criteria: list[str] = Field(min_length=1, max_length=20)
    steps: list[PlanStep] = Field(min_length=1, max_length=20)


class NeedUserDecision(BaseModel):
    """Role-agnostic hand-off decision shared by Planner and Executor.

    Stage 1.5 collapsed the role-prefixed copies into a single canonical
    model. The role-prefixed names are preserved as type aliases.
    """

    model_config = {"extra": "forbid"}

    action: Literal["need_user"]
    user_question: str = Field(min_length=1)

    @field_validator("user_question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("need_user requires user_question")
        return value


# Backward-compatible aliases (Stage 1.5).
PlannerNeedUserDecision = NeedUserDecision
ExecutorNeedUserDecision = NeedUserDecision


class PlannerDecision(
    _DecisionAttributeProxy,
    RootModel[
        Annotated[
            PlannerCallToolDecision | PlannerPlanDecision | PlannerNeedUserDecision,
            Field(discriminator="action"),
        ]
    ],
):
    """Discriminated Planner action union exposed as a gateway response model."""


class PlannerResult(BaseModel):
    """Planner outcome consumed by the runtime; a plan is mandatory on success."""

    model_config = {"extra": "forbid"}

    status: str
    plan: ExecutionPlan | None = None
    observations: list[ToolObservation] = Field(default_factory=list)
    user_question: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_result_shape(self) -> "PlannerResult":
        if self.status == "planned" and self.plan is None:
            raise ValueError("planned result requires a plan")
        if self.status == "needs_user" and not self.user_question:
            raise ValueError("needs_user result requires user_question")
        if self.status not in {"planned", "needs_user", "failed"}:
            raise ValueError("unknown planner result status")
        return self


class ExecutorCompleteDecision(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["complete"]
    summary: str = Field(min_length=1)
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("complete requires summary")
        return value


class ExecutorDecision(
    _DecisionAttributeProxy,
    RootModel[
        Annotated[
            ExecutorCallToolDecision
            | ExecutorCompleteDecision
            | ExecutorNeedUserDecision,
            Field(discriminator="action"),
        ]
    ],
):
    """Discriminated Executor action union exposed as a gateway response model."""


class ExecutorResult(BaseModel):
    """Step-level execution outcome and the observations that support it."""

    model_config = {"extra": "forbid"}

    status: str
    summary: str | None = None
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[ToolObservation] = Field(default_factory=list)
    user_question: str | None = None
    error_code: str | None = None
    # Cross-invocation execution state (succeeded-call dedup set + waste
    # counters) for the runtime to carry into the next verifier-RETRY
    # invocation. Excluded from serialization so the verifier never sees it.
    execution_state: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @model_validator(mode="after")
    def validate_result_shape(self) -> "ExecutorResult":
        if self.status == "succeeded" and not self.summary:
            raise ValueError("succeeded executor result requires summary")
        if self.status == "needs_user" and not self.user_question:
            raise ValueError("needs_user executor result requires user_question")
        if self.status not in {"succeeded", "needs_user", "failed"}:
            raise ValueError("unknown executor result status")
        return self
