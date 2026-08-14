"""Autonomous Planner role for the adaptive PEV runtime."""

from __future__ import annotations

import time

from pydantic import ValidationError

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.model_gateway import AgentModelGateway
from backend.app.services.agent_runtime.model_budget import (
    ModelCallBudget,
    estimate_input_tokens,
)
from backend.app.services.agent_runtime.prompt_rules import (
    COMMON_RUNTIME_RULES,
    PLANNER_RUNTIME_RULES,
)
from backend.app.services.agent_runtime.observation_projection import (
    record_observation,
    summarize_observations,
)
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    PlannerDecision,
    PlannerResult,
    PlanStep,
    StepInputRef,
    StepOutputRef,
    ToolObservation,
)
from backend.app.services.agent_runtime.skill_definition import SkillRegistry
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_registry import ToolRegistry
from backend.app.services.agent_runtime.context_manifest import (
    build_context_manifest,
    compute_evidence_chars,
)
from backend.app.services.agent_runtime.tracing import DecisionTrace, decision_summary
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget

# Generic runtime prompt. Domain policy is loaded from the activated canonical
# Skill package through ``SkillRegistry.prompt_policy``.
_PLANNER_INSTRUCTION = (
    "## 角色\n"
    "You are the Planner role in a generic Planner-Executor-Verifier runtime.\n"
    "## 行为规则\n"
    "Inspect only supplied state, choose permitted low-risk tools when needed, "
    "decompose independent deliverables into steps, keep each step within its "
    "declared Skill authority, and declare typed inputs, outputs, and dependencies.\n"
    "## 流程\n"
    "A non-empty confirmed_profile_fact_fields list means the server already has "
    "those confirmed private facts; do not ask the user to upload, paste, or repeat "
    "their values. Field names are intentionally all the Planner needs; the scoped "
    "Executor can use the values for an activated Skill. If a preceding step is "
    "blocked, ask for the input that unblocks that step, never for unrelated private "
    "fields that are already present. "
    "The Executor receives only the activated Skill's least-privilege projection. "
    "If an activated Skill requires an artifact that an allowed preceding Skill can "
    "obtain from public or otherwise permitted evidence, plan that preceding step "
    "instead of asking the user for a duplicate artifact. Ask only for information "
    "that cannot be obtained through permitted tools or activated Skill instructions. "
    "Do not ask a bundle of optional questions: missing preferences are not blockers. "
    "Before asking, check context, private context, and every activated Skill; if a "
    "permitted path can make useful progress, plan that path. Ask at most one concrete "
    "question, and only when one missing input blocks every permitted path. "
    "Never invent evidence, tool capability, or a completed deliverable.\n"
    "## 输出契约\n"
    "Return one outcome-based plan with explicit success criteria, or one concrete "
    "user question."
    "\n\n"
    + COMMON_RUNTIME_RULES
    + PLANNER_RUNTIME_RULES
)

_MAX_INVALID_PLAN_RETRIES = 3


class PlannerAgent:
    """Goal-oriented Planning loop; it is not a one-shot prompt template."""

    def __init__(
        self,
        *,
        gateway: AgentModelGateway,
        tools: ToolRegistry,
        skills: SkillRegistry | None = None,
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._skills = skills

    def run(
        self,
        *,
        task: AgentTaskRequest,
        context: ToolContext,
        trace: DecisionTrace | None = None,
        tool_budget: ToolCallBudget | None = None,
        turn_budget: AgentTurnBudget | None = None,
        model_budget: ModelCallBudget | None = None,
        deadline: float | None = None,
    ) -> PlannerResult:
        """Sense context and form a bounded execution plan for every request."""
        observations: list[ToolObservation] = []
        observations_for_decision: list[dict[str, object]] = []
        confirmed_facts = task.private_context.get("confirmed_profile_facts")
        fact_fields = (
            sorted(field for field in confirmed_facts if isinstance(field, str))
            if isinstance(confirmed_facts, dict)
            else []
        )
        # The plan/allowed-skills are immutable for this run, so the tool
        # catalog is a loop-invariant projection (also memoized in ToolRegistry).
        allowed_skills = frozenset(task.allowed_skills)
        available_tools = self._tools.tool_catalog(
            role=AgentRole.planner, allowed_skills=allowed_skills
        )
        available_executor_tools = self._tools.tool_catalog(
            role=AgentRole.executor, allowed_skills=allowed_skills
        )
        premature_need_user_retries = 0
        invalid_plan_retries = 0
        runtime_feedback: str | None = None
        gateway_manages_model_budget = bool(
            getattr(self._gateway, "manages_model_budget", False)
        )
        for _turn in range(task.budget.max_agent_turns):
            if deadline is not None and time.monotonic() >= deadline:
                return PlannerResult(
                    status="failed",
                    observations=observations,
                    error_code="wall_clock_budget_exhausted",
                )
            if turn_budget is not None and not turn_budget.try_consume():
                return PlannerResult(
                    status="failed",
                    observations=observations,
                    error_code="agent_turn_budget_exhausted",
                )
            # Bound the observation list the model sees: keep the most-recent
            # projections full and collapse older ones to identifier-only summary
            # lines when the accumulated list exceeds the character budget.
            summarized_observations = summarize_observations(observations_for_decision)
            decision_state = {
                "goal": task.goal,
                "allowed_skills": task.allowed_skills,
                "skill_policy": (
                    self._skills.prompt_policy(task.allowed_skills)
                    if self._skills is not None
                    else ""
                ),
                "available_tools": available_tools,
                "available_executor_tools": available_executor_tools,
                "context": task.context,
                "confirmed_profile_fact_fields": fact_fields,
                "remaining_tool_calls": (
                    tool_budget.remaining
                    if tool_budget is not None
                    else task.budget.max_tool_calls - len(observations)
                ),
                "remaining_agent_turns": (
                    turn_budget.remaining
                    if turn_budget is not None
                    else task.budget.max_agent_turns - _turn - 1
                ),
                "observations": summarized_observations,
                "replan_state": task.replan_state.model_dump(mode="json"),
            }
            if runtime_feedback:
                decision_state["runtime_feedback"] = runtime_feedback
            if (
                model_budget is not None
                and not gateway_manages_model_budget
                and not model_budget.try_reserve(
                    estimate_input_tokens(_PLANNER_INSTRUCTION, decision_state)
                )
            ):
                return PlannerResult(
                    status="failed",
                    observations=observations,
                    error_code="model_budget_exhausted",
                )
            if gateway_manages_model_budget:
                decision = self._gateway.decide(
                    role=AgentRole.planner,
                    instruction=_PLANNER_INSTRUCTION,
                    state=decision_state,
                    response_model=PlannerDecision,
                    model_budget=model_budget,
                )
            else:
                decision = self._gateway.decide(
                    role=AgentRole.planner,
                    instruction=_PLANNER_INSTRUCTION,
                    state=decision_state,
                    response_model=PlannerDecision,
                )
            if (
                model_budget is not None
                and not gateway_manages_model_budget
                and not model_budget.record(self._gateway.last_usage)
            ):
                return PlannerResult(
                    status="failed",
                    observations=observations,
                    error_code="model_budget_exhausted",
                )
            if trace is not None:
                usage = self._gateway.last_usage
                if isinstance(usage, dict):
                    usage["context_manifest"] = build_context_manifest(
                        instruction=_PLANNER_INSTRUCTION,
                        available_tools=available_tools,
                        observations_for_decision=summarized_observations,
                        evidence_chars=compute_evidence_chars(
                            context.metadata.get("observed_public_evidence")
                        ),
                        model_name=usage.get("model_name"),
                    )
                trace(
                    AgentRole.planner,
                    decision_summary(
                        action=decision.action, tool_name=decision.tool_name
                    ),
                    usage,
                )
            if decision.action == "call_tool":
                if tool_budget is not None and not tool_budget.try_consume():
                    return PlannerResult(
                        status="failed",
                        observations=observations,
                        error_code="tool_budget_exhausted",
                    )
                record_observation(
                    observations,
                    observations_for_decision,
                    self._tools.invoke(
                        role=AgentRole.planner,
                        name=decision.tool_name or "",
                        context=context,
                        payload=decision.tool_input,
                    ),
                )
                continue
            if decision.action == "plan":
                try:
                    plan = ExecutionPlan(
                        task=task,
                        created_by=AgentRole.planner,
                        complexity=decision.complexity,
                        success_criteria=decision.success_criteria,
                        steps=decision.steps,
                    )
                except ValidationError as error:
                    if (
                        invalid_plan_retries < _MAX_INVALID_PLAN_RETRIES
                        and _turn < task.budget.max_agent_turns - 1
                    ):
                        invalid_plan_retries += 1
                        details = []
                        for item in error.errors()[:4]:
                            location = ".".join(str(part) for part in item.get("loc", ()))
                            message = str(item.get("msg") or "invalid value")
                            details.append(f"{location or 'plan'}: {message}")
                        runtime_feedback = (
                            "ExecutionPlan 校验失败，请只修正以下结构问题后重新输出计划："
                            + "; ".join(details)[:500]
                        )
                        continue
                    fallback = self._build_seeded_career_fallback(task)
                    if fallback is not None:
                        return PlannerResult(
                            status="planned",
                            plan=fallback,
                            observations=observations,
                        )
                    return PlannerResult(
                        status="needs_user",
                        observations=observations,
                        user_question=(
                            "模型生成的执行计划不符合运行约束，请重试或补充必要信息。"
                        ),
                        error_code="invalid_execution_plan",
                    )
                if self._skills is not None:
                    plan = plan.model_copy(
                        update={
                            "steps": [
                                self._skills.normalize_step_ports(step)
                                for step in plan.steps
                            ]
                        }
                    )
                    port_error = next(
                        (
                            error
                            for step in plan.steps
                            if (error := self._skills.validate_step_ports(step))
                        ),
                        None,
                    )
                    if port_error:
                        if (
                            invalid_plan_retries < _MAX_INVALID_PLAN_RETRIES
                            and _turn < task.budget.max_agent_turns - 1
                        ):
                            invalid_plan_retries += 1
                            runtime_feedback = (
                                "ExecutionPlan 端口校验失败，请使用 Skill policy 中的规范 artifact 类型："
                                + port_error[:500]
                            )
                            continue
                        fallback = self._build_seeded_career_fallback(task)
                        if fallback is not None:
                            return PlannerResult(
                                status="planned",
                                plan=fallback,
                                observations=observations,
                            )
                        return PlannerResult(
                            status="needs_user",
                            observations=observations,
                            user_question=(
                                "模型生成的执行计划包含不兼容的 artifact 类型，请重试。"
                            ),
                            error_code="invalid_execution_plan",
                        )
                plan = self._trim_unrequested_trailing_steps(task, plan)
                return PlannerResult(
                    status="planned", plan=plan, observations=observations
                )
            if (
                available_executor_tools
                and premature_need_user_retries < 1
                and _turn < task.budget.max_agent_turns - 1
            ):
                premature_need_user_retries += 1
                runtime_feedback = (
                    "Policy correction: permitted tools are still available. "
                    "Do not ask the user yet. Re-check context and Skill policy; "
                    "return a plan if any permitted path can make progress."
                )
                continue
            return PlannerResult(
                status="needs_user",
                observations=observations,
                user_question=decision.user_question,
            )
        return PlannerResult(
            status="failed",
            observations=observations,
            user_question="Planner turn budget exhausted before a safe plan was formed.",
        )

    def _build_seeded_career_fallback(
        self, task: AgentTaskRequest
    ) -> ExecutionPlan | None:
        """Build a narrow deterministic plan when seeded career planning is malformed.

        This is only enabled for the production career registry and explicit
        candidate URLs.  It repairs model schema/dependency noise without
        inventing a source or bypassing a site boundary; the Executor still
        has to fetch and validate every public page.
        """
        if self._skills is None:
            return None
        candidate_urls = task.context.get("candidate_urls")
        if not (
            isinstance(candidate_urls, list)
            and any(isinstance(url, str) and url.strip() for url in candidate_urls)
        ):
            return None
        permitted = set(task.allowed_skills)
        if "job-discovery" not in permitted:
            return None

        steps: list[PlanStep] = [
            PlanStep(
                step_id="discover_jobs",
                objective="从候选公开 URL 抓取并规范化可追溯 JD 证据。",
                allowed_skills=["job-discovery"],
                success_criteria=["至少产出一个有效结构化 JD artifact"],
                inputs=[
                    StepInputRef(kind="context", name="candidate_urls")
                ],
                outputs=[
                    StepOutputRef(
                        name="structured_job_details",
                        artifact_type="structured_job_details",
                    )
                ],
            )
        ]
        previous_step = "discover_jobs"
        previous_artifact = "structured_job_details"

        goal = task.goal
        needs_matching = (
            "job-matching" in permitted
            and any(marker in goal for marker in ("匹配", "筛选", "最适合", "最匹配"))
        )
        needs_tailoring = (
            "resume-tailoring" in permitted
            and any(marker in goal for marker in ("简历", "定制", "修改建议"))
        )
        needs_planning = (
            "career-planning" in permitted
            and any(marker in goal for marker in ("面试", "准备", "计划", "回答要点"))
        )
        if needs_matching:
            steps.append(
                PlanStep(
                    step_id="match_jobs",
                    objective="基于已确认简历事实对有效 JD 做透明匹配排序。",
                    allowed_skills=["job-matching"],
                    success_criteria=["产出带证据引用的岗位匹配报告"],
                    depends_on=[previous_step],
                    inputs=[
                        StepInputRef(
                            kind="artifact",
                            name=previous_artifact,
                            from_step=previous_step,
                            artifact_type=previous_artifact,
                        )
                    ],
                    outputs=[
                        StepOutputRef(
                            name="job_matching_report",
                            artifact_type="job_matching_report",
                        )
                    ],
                )
            )
            previous_step = "match_jobs"
            previous_artifact = "job_matching_report"
        if needs_tailoring:
            steps.append(
                PlanStep(
                    step_id="tailor_resume",
                    objective="针对最匹配岗位生成基于已确认事实的简历修改建议。",
                    allowed_skills=["resume-tailoring"],
                    success_criteria=["产出可审阅的简历定制 brief"],
                    depends_on=[previous_step],
                    inputs=[
                        StepInputRef(
                            kind="artifact",
                            name=previous_artifact,
                            from_step=previous_step,
                            artifact_type=previous_artifact,
                        )
                    ],
                    outputs=[
                        StepOutputRef(
                            name="resume_tailoring_brief",
                            artifact_type="resume_tailoring_brief",
                        )
                    ],
                )
            )
            previous_step = "tailor_resume"
            previous_artifact = "resume_tailoring_brief"
        if needs_planning:
            steps.append(
                PlanStep(
                    step_id="prepare_interview",
                    objective="基于有效 JD 生成岗位相关的面试准备计划。",
                    allowed_skills=["career-planning"],
                    success_criteria=["产出带岗位证据的准备计划"],
                    depends_on=[previous_step],
                    inputs=[
                        StepInputRef(
                            kind="artifact",
                            name=previous_artifact,
                            from_step=previous_step,
                            artifact_type=previous_artifact,
                        )
                    ],
                    outputs=[
                        StepOutputRef(
                            name="career_preparation_plan",
                            artifact_type="career_preparation_plan",
                        )
                    ],
                )
            )
        try:
            return ExecutionPlan(
                task=task,
                created_by=AgentRole.planner,
                complexity="L3" if len(steps) >= 3 else "L2",
                success_criteria=["完成请求的职业辅助交付物"],
                steps=steps,
            )
        except Exception:
            return None

    def build_seeded_fallback(self, task: AgentTaskRequest) -> ExecutionPlan | None:
        """Expose the narrow URL-seeded fallback to the runtime harness.

        The fallback is deliberately kept on the Planner so the runtime does
        not duplicate plan-construction policy.  It is safe to use after a
        malformed model response because it only consumes user- or
        chain-provided URLs and still routes every fetch through the normal
        public-evidence tools.
        """
        return self._build_seeded_career_fallback(task)

    @staticmethod
    def _trim_unrequested_trailing_steps(
        task: AgentTaskRequest, plan: ExecutionPlan
    ) -> ExecutionPlan:
        """Drop trailing deliverables that the user's goal never requested.

        A valid schema is not sufficient when a Planner appends a second
        career deliverable (for example resume tailoring after a pure matching
        question). Such a step has no target contract and commonly ends in a
        false no-progress hand-off. Only trailing steps are removed, so this
        repair never rewrites dependencies for an earlier requested step.
        """
        goal = task.goal.lower()
        wants_tailoring = any(
            marker in goal for marker in ("简历", "定制", "修改建议", "resume", "tailor")
        )
        wants_planning = any(
            marker in goal
            for marker in ("面试", "准备", "计划", "回答要点", "career preparation", "interview")
        )
        steps = list(plan.steps)
        while steps:
            skills = set(steps[-1].allowed_skills)
            if "resume-tailoring" in skills and not wants_tailoring:
                steps.pop()
                continue
            if "career-planning" in skills and not wants_planning:
                steps.pop()
                continue
            break
        if len(steps) == len(plan.steps):
            return plan
        return plan.model_copy(update={"steps": steps})
