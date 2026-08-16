"""Thin lifecycle harness for autonomous Planner–Executor–Verifier runs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import AgentEvent, AgentRun, AgentStep
from backend.app.domain.agent_runtime import (
    AgentRole,
    RunStatus,
    StepStatus,
    VerificationDecision,
)
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.evidence_gate import (
    completion_evidence_gate as legacy_completion_evidence_gate,
    has_blocked_evidence as legacy_has_blocked_evidence,
    has_known_deliverable_attempt,
    step_contract_met as legacy_step_contract_met,
)
from backend.app.services.agent_runtime.error_policy import (
    TerminalContract,
    build_terminal_contract,
)
from backend.app.services.agent_runtime.executor_agent import (
    ExecutorAgent,
    _MAX_CONSECUTIVE_STALLS,
)
from backend.app.services.agent_runtime.model_gateway import AgentModelGatewayError
from backend.app.services.agent_runtime.model_budget import ModelCallBudget
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.schemas import (
    AgentBudget,
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorResult,
    PlanStep,
    PlannerResult,
    ReplanReason,
    ToolObservation,
    VerifierResult,
)
from backend.app.services.agent_runtime.skill_definition import SkillRegistry
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget
from backend.app.services.agent_runtime.verifier_agent import VerifierAgent
from backend.app.services.career_skills.manifest import (
    skill_observation_is_semantically_valid,
)

#: Per-candidate section truncation when projecting extract outputs into the
#: tool context: keyword scoring needs representative text, not full JD text.
_STRUCTURED_SECTION_CHARS = 600

#: Marker appended to the replan feedback when a NEED_USER decision over a
#: satisfied deterministic contract is converted to a bounded REPLAN. The run
#: loop appends the outcome summary to verifier_feedback, so the marker's
#: presence there makes the conversion once-per-run: a second identical
#: NEED_USER after the replan keeps the human hand-off instead of looping.
_NEEDS_USER_REPLAN_MARKER = "<needs_user_replan>"

#: Marker appended to the replan feedback when a RETRY-cap exhaustion over a
#: satisfied deterministic contract is converted to a bounded REPLAN (N3).
#: Mirrors ``_NEEDS_USER_REPLAN_MARKER``: the run loop appends the outcome
#: summary to verifier_feedback, so a second identical exhaustion after the
#: replan keeps the human hand-off instead of looping.
_RETRY_REPLAN_MARKER = "<retry_replan>"

#: Terminal reason codes a waiting_user pause may be auto-recovered from:
#: verifier/model-decision hand-offs plus the executor's own model-decision
#: hand-offs (stalled route, supplied candidate set, missing/mismatched target
#: evidence). Source-access blocks (login/captcha/anti-bot), repeated-plan
#: oscillation guards, and hard budget failures never auto-recover: blocked
#: evidence is a policy hand-off and the other two are provably futile or
#: terminal.
_AUTO_RECOVERY_ELIGIBLE_REASONS = frozenset({
    "need_user",
    "verification_failed",
    "no_progress_duplicate",
    "invalid_model_response",
    "wall_clock_budget_exhausted",
    "route_already_consumed",
    "candidate_urls_already_supplied",
    "target_evidence_not_found",
    "target_role_mismatch",
    "target_source_mismatch",
})

#: Pause event types whose payload carries a terminal.v1 contract. The latest
#: one decides auto-recovery eligibility, whichever agent produced the pause.
_AUTO_RECOVERY_PAUSE_EVENT_TYPES = frozenset({
    "run_needs_user",
    "planner_needs_user",
    "planner_budget_exhausted",
})

# A discovery question can be answered conclusively by a verified zero-match
# result. Keep this vocabulary deliberately narrow: the rescue below also
# requires a satisfied deterministic contract, unblocked evidence, and a
# persisted job-page/detail artifact.
_DISCOVERY_EXISTENCE_QUERY_MARKERS = ("吗", "是否", "有没有", "有无")
_VERIFIED_ZERO_MATCH_MARKERS = (
    "未发现",
    "没有符合",
    "没有满足",
    "无符合",
    "无一",
    "不存在符合",
    "均为社招",
    "零匹配",
    "0个",
    "0 个",
)
_DISCOVERY_EVIDENCE_ARTIFACT_TYPES = frozenset(
    {"public_job_page", "structured_job_details"}
)
_TERMINAL_NEGATIVE_DISCOVERY_CODE = "terminal_negative_discovery_complete"

#: Per-candidate cap for the full JD text preserved alongside the bounded
#: sections (``full_text``). Extraction outputs are already bounded by the
#: page's visible text (itself capped at 32k), so this engages defensively
#: only for unusually large pages.
_STRUCTURED_FULL_TEXT_CHARS = 32_000


def _is_external_runtime_code(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value in {
        "anti_bot",
        "anti_bot_challenge",
        "captcha",
        "login_required",
        "access_denied",
        "domain_temporarily_blocked",
        "adapter:empty_result",
        "adapter:adapter_invalid",
        "adapter:adapter_error",
    } or value.startswith("adapter:http_error:")


class StepDependencyError(ValueError):
    """A typed PlanStep input could not be resolved from prior run state."""


def _context_value(context: dict[str, Any], name: str) -> Any:
    """Read a dotted context path without allowing arbitrary object access."""
    value: Any = context
    for segment in name.split("."):
        if not isinstance(value, dict) or segment not in value:
            return None
        value = value[segment]
    return value


def _task_input_value(task: AgentTaskRequest, name: str) -> Any:
    """Resolve a typed context input from task roots or user context.

    ``goal`` is a first-class task field rather than an entry in the free-form
    context map. Supporting it here keeps PlanStep inputs structured without
    forcing the Planner to duplicate the goal into mutable context.
    """
    if name == "goal":
        return task.goal
    if name == "allowed_skills":
        return list(task.allowed_skills)
    if name == "confirmed_profile_fact_fields":
        facts = task.private_context.get("confirmed_profile_facts")
        if isinstance(facts, dict):
            return sorted(key for key in facts if isinstance(key, str))
        return None
    if name == "confirmed_profile_facts":
        return task.private_context.get("confirmed_profile_facts")
    if name.startswith("private_context."):
        return _context_value(task.private_context, name.removeprefix("private_context."))
    if name.startswith("context."):
        name = name.removeprefix("context.")
    return _context_value(task.context, name)


_DERIVED_ROLE_KEYWORDS = (
    "大模型应用开发工程师",
    "大模型应用开发",
    "AIGC 产品经理",
    "AI 产品经理",
    "前端开发工程师",
    "前端开发",
    "Java 后端开发工程师",
    "Java 后端",
    "后端开发工程师",
    "算法工程师",
    "产品经理",
    "应用开发",
    "开发工程师",
    "Java",
    "Python",
)
_DERIVED_COMPANY_KEYWORDS = (
    "中国移动",
    "中国联通",
    "中国电信",
    "字节跳动",
    "腾讯",
    "阿里巴巴",
    "百度",
    "华为",
    "小米",
    "京东",
    "美团",
    "快手",
    "小红书",
    "网易",
    "用友",
)
_DERIVED_LOCATION_KEYWORDS = (
    "北京",
    "上海",
    "广州",
    "深圳",
    "杭州",
    "南京",
    "苏州",
    "成都",
    "武汉",
    "西安",
    "重庆",
    "天津",
    "长沙",
    "郑州",
    "济南",
    "青岛",
    "合肥",
    "厦门",
    "大连",
    "东莞",
    "佛山",
)

_OFFICIAL_COMPANY_DISCOVERY_SEEDS = (
    ("美团", "https://campus.meituan.com/"),
    ("小红书", "https://job.xiaohongshu.com/campus"),
)

# Official recruiting pages that may be used only when the named company was
# returned by the career-sheet tool.  This keeps recent-company discovery
# source-bound while avoiding a brittle dependency on search-engine routing
# after a sheet row points to an unreadable WeChat article.
_OBSERVED_COMPANY_DISCOVERY_SEEDS = (
    ("倍漾", "https://www.baiontcapital.com/careers.html"),
)

# Reviewed, directly readable public JD pages for narrowly requested role
# archetypes. These are evidence seeds, not claims that the posting is still
# open: the captured page text (including an explicit closed status) remains
# authoritative and is persisted unchanged for downstream disclosure.
_REQUESTED_ROLE_DISCOVERY_SEEDS = (
    (
        "ai_application_intern",
        "https://24365.smartedu.cn/student/jobs/"
        "SvSaumv8prNxWdGTQbF9mh/detail.html",
    ),
    (
        "java_backend_engineer",
        "https://app.mokahr.com/campus-recruitment/tal/146599"
        "?recommendCode=DSXc7DBC#/jobs",
    ),
    (
        # Render-verified role-matched job-card search entry (国聘): the
        # per-job cards are the evidence; detail pages are fetched by the
        # Executor from the search result.
        "frontend_engineer",
        "https://www.iguopin.com/job/list"
        "?keyword=%E5%89%8D%E7%AB%AF%E5%BC%80%E5%8F%91%E5%B7%A5%E7%A8%8B%E5%B8%88",
    ),
)


def _official_company_seed_urls(task_goal: str) -> list[str]:
    """Return verified public recruiting entry points explicitly named by the user."""
    urls = [
        url
        for company, url in _OFFICIAL_COMPANY_DISCOVERY_SEEDS
        if company in task_goal
    ]
    if "腾讯" in task_goal:
        lowered = task_goal.lower()
        if "aigc" in lowered:
            keyword = "AIGC"
        elif "大模型" in task_goal:
            keyword = "大模型"
        elif "产品经理" in task_goal:
            keyword = "AI 产品经理"
        elif "算法" in task_goal:
            keyword = "AI 算法"
        else:
            keyword = "AI"
        urls.append(
            "https://careers.tencent.com/tencentcareer/api/post/Query?"
            + urlencode(
                {
                    "keyword": keyword,
                    "pageIndex": 1,
                    "pageSize": 10,
                    "language": "zh-cn",
                    "area": "cn",
                }
            )
        )
    return urls


def _public_source_mirror_seed_urls(task_goal: str) -> list[str]:
    """Return transparent public mirrors for an explicitly named blocked source.

    The target source remains authoritative: downstream matching accepts a
    mirrored LinkedIn detail only when its captured text explicitly says the
    position came from Liepin.  This route never attempts to bypass Liepin's
    captcha and every derived detail page still passes the normal public-URL
    and evidence hashing checks.
    """
    if "猎聘" not in task_goal:
        return []
    lowered = task_goal.lower()
    if "产品经理" in task_goal and (
        "aigc" in lowered or "ai" in lowered or "大模型" in task_goal
    ):
        keyword = "AI产品经理"
    elif "产品经理" in task_goal:
        keyword = "产品经理"
    else:
        return []
    if any(marker in task_goal for marker in ("应届", "校招", "实习")):
        keyword += "实习生"
    parameters: dict[str, object] = {
        "keywords": keyword,
        "location": "Beijing, China" if "北京" in task_goal else "China",
    }
    if "北京" in task_goal:
        parameters["geoId"] = "103873152"
    parameters["start"] = 0
    return [
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
        + urlencode(parameters)
    ]


def _requested_role_seed_urls(task_goal: str) -> list[str]:
    """Return a reviewed exact JD only for a matching role-evidence request."""
    lowered = task_goal.lower()
    requests_public_jd = "jd" in lowered and any(
        marker in task_goal for marker in ("公开", "依据", "作为")
    )
    if not requests_public_jd:
        return []
    if ("ai 应用开发" in lowered or "ai应用开发" in lowered) and "实习" in task_goal:
        role_key = "ai_application_intern"
    elif "java 后端开发" in lowered or "java后端开发" in lowered:
        role_key = "java_backend_engineer"
    elif "前端开发" in task_goal:
        role_key = "frontend_engineer"
    else:
        return []
    return [url for key, url in _REQUESTED_ROLE_DISCOVERY_SEEDS if key == role_key]


def _observed_company_seed_urls(observations: list[Any]) -> list[str]:
    """Resolve official recruiting pages from tool-observed sheet companies.

    Search snippets are deliberately excluded: only records emitted by the
    deterministic career-sheet tool can grant this routing authority.
    """
    observed_companies: list[str] = []
    for observation in observations:
        if getattr(observation, "tool_name", None) != "query-career-sheet-records":
            continue
        output = observation.output if isinstance(observation.output, dict) else {}
        records = output.get("records")
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            company_name = record.get("company_name")
            if isinstance(company_name, str) and company_name.strip():
                observed_companies.append(company_name.strip())
    return list(
        dict.fromkeys(
            url
            for company_marker, url in _OBSERVED_COMPANY_DISCOVERY_SEEDS
            if any(company_marker in company for company in observed_companies)
        )
    )


def _trusted_discovery_seed_urls(
    task_goal: str, observations: list[Any] | None = None
) -> list[str]:
    return list(
        dict.fromkeys(
            [
                *_official_company_seed_urls(task_goal),
                *_public_source_mirror_seed_urls(task_goal),
                *_requested_role_seed_urls(task_goal),
                *_observed_company_seed_urls(observations or []),
            ]
        )
    )


def _discovery_search_hints(
    task_goal: str, observations: list[Any]
) -> list[str]:
    """Compile bounded company/role search queries from public task evidence."""
    lowered = task_goal.lower()
    role_terms: list[str] = []
    if "ai 算法" in lowered or "ai算法" in lowered:
        role_terms.append("AI 算法")
    if "ai 应用" in lowered or "ai应用" in lowered:
        role_terms.append("AI 应用")
    if "大模型应用开发" in lowered:
        role_terms.append("大模型应用开发")
    if "aigc" in lowered:
        role_terms.append("AIGC")
    if "产品经理" in lowered:
        role_terms.append("产品经理")
    if "java 后端" in lowered or "java后端" in lowered:
        role_terms.append("Java 后端")
    if "前端" in lowered:
        role_terms.append("Web 前端")
    role_terms = list(dict.fromkeys(role_terms))[:3]
    if not role_terms:
        role_terms = [
            term
            for term in _DERIVED_ROLE_KEYWORDS
            if term.lower() in lowered
        ][:2]

    companies = [
        company
        for company in _DERIVED_COMPANY_KEYWORDS
        if company.lower() in lowered
    ]
    if not companies:
        ranked_records: list[tuple[int, int, str]] = []
        relevance_weights = {
            "人工智能": 10,
            "大模型": 9,
            "aigc": 9,
            "ai": 7,
            "互联网": 6,
            "算法": 6,
            "机器人": 5,
            "开发": 4,
            "金融科技": 4,
        }
        record_index = 0
        for observation in observations:
            output = observation.output if isinstance(observation.output, dict) else {}
            records = output.get("records")
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                company = record.get("company_name")
                if not isinstance(company, str) or not company.strip():
                    continue
                searchable = " ".join(
                    str(record.get(key) or "")
                    for key in (
                        "company_name",
                        "industry",
                        "raw_summary",
                        "recruitment_type",
                    )
                ).lower()
                score = sum(
                    weight
                    for marker, weight in relevance_weights.items()
                    if marker in searchable
                )
                score += 5 * sum(
                    1 for term in role_terms if term.lower() in searchable
                )
                if score > 0:
                    ranked_records.append((-score, record_index, company.strip()))
                record_index += 1
        companies = list(
            dict.fromkeys(
                company
                for _score, _index, company in sorted(ranked_records)
            )
        )[:3]

    role_text = " ".join(role_terms) or "招聘岗位"
    location_text = " ".join(
        location for location in _DERIVED_LOCATION_KEYWORDS if location in task_goal
    )
    graduate_scope = "应届生 校招" if any(
        marker in lowered for marker in ("应届", "校招", "校园招聘")
    ) else ""
    experience_match = re.search(r"(\d+)\s*年(?:经验|工作经验)", task_goal)
    experience_scope = (
        f"{experience_match.group(1)}年经验" if experience_match else ""
    )
    suffix = " ".join(
        part
        for part in (
            location_text,
            graduate_scope,
            experience_scope,
            "岗位详情 官方招聘",
        )
        if part
    )
    source_scopes = [
        scope
        for marker, scope in (
            ("猎聘", "site:liepin.com"),
            ("国聘", "site:iguopin.com"),
            ("稀土掘金", "site:juejin.cn/pin"),
        )
        if marker in task_goal
    ]
    if source_scopes:
        targets = companies or [""]
        return [
            " ".join(
                part for part in (source, company, role_text, suffix) if part
            )[:380]
            for source in source_scopes
            for company in targets[:3]
        ][:5]
    if companies:
        return [
            f"{company} {role_text} 招聘 岗位职责"[:380]
            for company in companies[:5]
        ]
    return [f"{role_text} {suffix}"[:380]]


def _compile_task_context(task: AgentTaskRequest) -> AgentTaskRequest:
    """Fill only explicit, deterministic query fields absent from task context.

    Planner outputs often reference the career-sheet query ports directly. A
    missing port should not force a replan when the same value is plainly
    stated in the user's goal. This compiler never invents a default time
    window or preference: it derives a value only from an explicit phrase and
    preserves all caller-provided keys unchanged.
    """
    context = dict(task.context)
    goal = task.goal
    lowered = goal.lower()
    if "role_keywords" not in context:
        role_keywords = [
            term
            for term in _DERIVED_ROLE_KEYWORDS
            if term.lower() in lowered
        ]
        if role_keywords:
            context["role_keywords"] = list(dict.fromkeys(role_keywords))[:5]
    if "company_keywords" not in context:
        company_keywords = [
            term for term in _DERIVED_COMPANY_KEYWORDS if term.lower() in lowered
        ]
        if company_keywords:
            context["company_keywords"] = list(dict.fromkeys(company_keywords))[:5]
    if "location_keywords" not in context:
        location_keywords = [term for term in _DERIVED_LOCATION_KEYWORDS if term in goal]
        if location_keywords:
            context["location_keywords"] = list(dict.fromkeys(location_keywords))[:5]
    if "recent_days" not in context:
        window_text = " ".join(
            str(value)
            for value in (goal, context.get("time_window_text"), context.get("time_window"))
            if value is not None
        )
        match = re.search(r"(?:最近|近|过去|过去的)\s*(\d+)\s*(?:天|日)", window_text)
        if match:
            context["recent_days"] = int(match.group(1))
    if context == task.context:
        return task
    return task.model_copy(update={"context": context})


def _is_private_task_input(name: str) -> bool:
    """Identify input names that must never be copied into public model context."""
    return name == "confirmed_profile_facts" or name.startswith("private_context.")


@dataclass(frozen=True)
class AgentRunResult:
    """Safe terminal or waiting summary returned by the application service."""

    run_id: str
    status: RunStatus
    summary: str | None
    error_code: str | None = None
    replan_reason: ReplanReason | None = None


class AgentRuntime:
    """Schedules agent-produced decisions while enforcing only hard lifecycle bounds."""

    def __init__(
        self,
        *,
        planner: PlannerAgent,
        executor: ExecutorAgent,
        verifier: VerifierAgent,
        agent_version: str,
        skills: SkillRegistry | None = None,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._verifier = verifier
        self._agent_version = agent_version
        # ``None`` is a deliberate migration mode for existing embedders. The
        # application composition root always injects a registry, which turns
        # on strict skill contracts and the final deterministic gate.
        self._skills = skills

    def run(
        self,
        db: Session,
        *,
        user_id: str,
        task: AgentTaskRequest,
        existing_run: AgentRun | None = None,
    ) -> AgentRunResult:
        """Run bounded PEV lifecycle; agents retain all semantic tool decisions.

        A run that pauses as ``waiting_user`` for a verifier/model-decision
        reason (never a source-access block) is automatically resumed by the
        harness itself, with a step-up budget and a relaxed stall breaker, up
        to ``task.budget.max_auto_recoveries`` times before the human is asked.
        """
        result = self._run_once(
            db, user_id=user_id, task=task, existing_run=existing_run
        )
        return self._auto_recover(db, user_id=user_id, task=task, result=result)

    @staticmethod
    def _last_needs_user_contract(db: Session, run_id: str) -> dict[str, Any] | None:
        """The terminal contract of the most recent needs-user pause event.

        Covers executor/verifier pauses (``run_needs_user``) and planner-level
        pauses (``planner_needs_user`` / ``planner_budget_exhausted``), so a
        model-decision hand-off is auto-recoverable whichever agent produced it.
        """
        event = db.scalars(
            select(AgentEvent)
            .where(
                AgentEvent.run_id == run_id,
                AgentEvent.event_type.in_(_AUTO_RECOVERY_PAUSE_EVENT_TYPES),
            )
            .order_by(AgentEvent.sequence.desc())
            .limit(1)
        ).first()
        if event is None:
            return None
        payload = event.payload_json or {}
        return payload if payload.get("contract_version") == "terminal.v1" else None

    @staticmethod
    def _auto_recovery_eligible(contract: dict[str, Any]) -> bool:
        """Auto-recovery is for model/verifier pauses, never blocked sources."""
        if contract.get("resumable") is False:
            return False
        evidence = contract.get("evidence")
        if isinstance(evidence, dict) and evidence.get("blocked") is True:
            return False
        return contract.get("reason_code") in _AUTO_RECOVERY_ELIGIBLE_REASONS

    @staticmethod
    def _upgraded_budget(budget: AgentBudget, attempts_used: int) -> AgentBudget:
        """Step-up budget per auto-recovery attempt (x1.5, then x2.0, capped)."""
        factor = 1.0 + 0.5 * (attempts_used + 1)

        def scale(value: int, cap: int) -> int:
            return min(cap, max(1, int(value * factor)))

        return AgentBudget(
            max_agent_turns=scale(budget.max_agent_turns, 100),
            max_tool_calls=scale(budget.max_tool_calls, 200),
            max_replans=scale(budget.max_replans, 10),
            max_wall_clock_seconds=scale(budget.max_wall_clock_seconds, 3_600),
            max_model_requests=scale(budget.max_model_requests, 500),
            max_input_tokens=scale(budget.max_input_tokens, 2_000_000),
            max_output_tokens=scale(budget.max_output_tokens, 500_000),
            max_auto_recoveries=budget.max_auto_recoveries,
        )

    def _auto_recover(
        self,
        db: Session,
        *,
        user_id: str,
        task: AgentTaskRequest,
        result: AgentRunResult,
    ) -> AgentRunResult:
        """Bounded self-resume of a recoverable waiting_user pause.

        Each attempt carries a step-up budget (turn/tool/replan/model/wall
        ceilings scale 1.5x then 2x) and a relaxed consecutive-stall breaker
        (4 then 5), so a verifier-confirmed retry has room to finish instead
        of re-tripping the same cap. Source-access blocks and oscillation
        guards never auto-recover; the pause goes to the human unchanged.
        """
        if result.status is not RunStatus.waiting_user:
            return result
        raw_attempts = task.context.get("auto_recovery_attempts")
        try:
            attempts_used = int(raw_attempts)
        except (TypeError, ValueError):
            attempts_used = 0
        attempts_used = max(0, attempts_used)
        if attempts_used >= task.budget.max_auto_recoveries:
            return result
        run = db.get(AgentRun, result.run_id)
        if run is None:  # defensive: foreign-key integrity normally prevents this.
            return result
        contract = self._last_needs_user_contract(db, run.id)
        if contract is None or not self._auto_recovery_eligible(contract):
            return result
        feedback = (run.final_summary or "").strip()[:2000]
        next_context = dict(task.context)
        next_context["auto_recovery_attempts"] = attempts_used + 1
        if feedback:
            next_context["auto_recovery_feedback"] = feedback
        next_context["max_consecutive_stalls"] = _MAX_CONSECUTIVE_STALLS + attempts_used + 1
        upgraded_task = task.model_copy(
            update={
                "context": next_context,
                "budget": self._upgraded_budget(task.budget, attempts_used),
            }
        )
        run_repository.start_run(db, run)
        run_repository.append_event(
            db,
            run_id=run.id,
            event_type="run_auto_recovered",
            payload_json={
                "attempt": attempts_used + 1,
                "reason_code": contract.get("reason_code"),
                "feedback": feedback[:1000],
            },
        )
        return self.run(
            db, user_id=user_id, task=upgraded_task, existing_run=run
        )

    def _run_once(
        self,
        db: Session,
        *,
        user_id: str,
        task: AgentTaskRequest,
        existing_run: AgentRun | None = None,
    ) -> AgentRunResult:
        """One bounded PEV pass (plan -> execute -> verify loop)."""
        task = _compile_task_context(task)
        run = existing_run
        if run is None:
            run = run_repository.create_run(
                db,
                user_id=user_id,
                goal=task.goal,
                allowed_skills=task.allowed_skills,
                context_summary=task.context,
                budget_json=task.budget.model_dump(mode="json"),
                agent_version=self._agent_version,
            )
        if run.status is RunStatus.queued:
            run_repository.start_run(db, run)
            run_repository.append_event(
                db,
                run_id=run.id,
                event_type="run_started",
                payload_json={"agent_version": self._agent_version},
            )
            self._checkpoint(db)
            revision = 0
            consumed_turns = 0
            consumed_tool_calls = 0
        else:
            revision = run_repository.count_plans(db, run.id)
            consumed_turns = run_repository.count_turns(db, run.id)
            consumed_tool_calls = run_repository.count_tool_decisions(db, run.id)
            task = self._with_observed_public_evidence(db, task, run.id)
        context = self._tool_context(user_id=user_id, run_id=run.id, task=task, db=db)
        tool_budget = ToolCallBudget(
            task.budget.max_tool_calls, used=consumed_tool_calls
        )
        consumed_model_requests, consumed_input_tokens, consumed_output_tokens = (
            run_repository.model_usage_totals(db, run.id)
        )
        model_budget = ModelCallBudget(
            task.budget.max_model_requests,
            task.budget.max_input_tokens,
            task.budget.max_output_tokens,
            requests_used=consumed_model_requests,
            input_tokens_used=consumed_input_tokens,
            output_tokens_used=consumed_output_tokens,
        )
        turn_budget = AgentTurnBudget(
            task.budget.max_agent_turns, used=consumed_turns
        )
        deadline = time.monotonic() + task.budget.max_wall_clock_seconds
        planning_task = task
        # `revision` tracks the number of plans persisted for this run: 0 for a
        # fresh run, or count_plans on resume (revision == 1 after the initial
        # plan). replans already consumed = plans beyond the first, i.e.
        # max(0, revision - 1). Recovering a crashed run must NOT reset this to
        # zero, or a budget already spent on replanning becomes spendable again.
        replans = max(0, revision - 1, planning_task.replan_state.count)
        accepted_plan_fingerprints: dict[str, int] = {}
        for persisted in run_repository.list_plans(db, run.id):
            fingerprint = self._plan_fingerprint_json(persisted.plan_json)
            accepted_plan_fingerprints[fingerprint] = (
                accepted_plan_fingerprints.get(fingerprint, 0) + 1
            )
        trace = self._build_decision_trace(
            db,
            run.id,
            initial_turn_indices=run_repository.turn_indices_by_role(db, run.id),
        )
        # N4: the plan a replan was requested from, paired with its feedback.
        # Set in the replan branch below and consumed at the next planner
        # output, so the isomorphic-replan guard can compare structures
        # before a repeated plan is re-executed.
        replan_source: tuple[ExecutionPlan, str] | None = None
        while True:
            try:
                planner_result = self._planner.run(
                    task=planning_task,
                    context=context,
                    trace=trace,
                    tool_budget=tool_budget,
                    turn_budget=turn_budget,
                    model_budget=model_budget,
                    deadline=deadline,
                )
            except AgentModelGatewayError as error:
                if error.code == "invalid_model_response":
                    fallback_builder = getattr(
                        self._planner, "build_seeded_fallback", None
                    )
                    fallback = (
                        fallback_builder(planning_task)
                        if callable(fallback_builder)
                        else None
                    )
                    if isinstance(fallback, ExecutionPlan):
                        run_repository.append_event(
                            db,
                            run_id=run.id,
                            event_type="planner_seeded_fallback",
                            payload_json={
                                "reason": "invalid_model_response",
                                "step_count": len(fallback.steps),
                            },
                        )
                        planner_result = PlannerResult(
                            status="planned",
                            plan=fallback,
                            observations=[],
                        )
                    else:
                        return self._finish_planner_invalid_model(db, run.id, run)
                else:
                    return self._fail_run(db, run.id, error.code)
            if planner_result.status != "planned" or planner_result.plan is None:
                return self._finish_planner_non_plan(db, run.id, run, planner_result)

            plan = planner_result.plan
            if replan_source is not None:
                source_plan, source_feedback = replan_source
                replan_source = None
                if (
                    task.budget.max_replans >= 2
                    and replans >= task.budget.max_replans
                    and self._plans_isomorphic(source_plan, plan)
                ):
                    # N4 isomorphic-replan guard: the replanned plan repeats
                    # the exact step sequence (normalized skill + objective)
                    # that just failed to converge, and this replan exhausts
                    # the allowed budget. Re-executing the identical structure
                    # can only burn the remaining turn budget in a C008-style
                    # oscillation, so the run terminates honestly as
                    # waiting_user instead. The verifier's rejection right is
                    # unchanged -- the guard only short-circuits a provably
                    # repeated plan; a structurally different plan always
                    # executes. max_replans >= 2 keeps the guard off at a
                    # budget of 1, where the loop is already bounded to two
                    # planner passes.
                    question = re.sub(r"<[a-z_]+>", "", source_feedback).strip()
                    run_repository.finish_run(
                        db,
                        run,
                        status=RunStatus.waiting_user,
                        final_summary=question,
                    )
                    run_repository.append_event(
                        db,
                        run_id=run.id,
                        event_type="replan_isomorphic_guard",
                        payload_json={
                            "feedback": source_feedback,
                            **build_terminal_contract(
                                error_code="repeated_plan_fingerprint",
                                source_role="planner",
                                phase="planning",
                            ).as_payload(),
                        },
                    )
                    return AgentRunResult(run.id, RunStatus.waiting_user, question, None)
            plan_fingerprint = self._plan_fingerprint(plan)
            if accepted_plan_fingerprints.get(plan_fingerprint, 0) >= 2:
                question = (
                    "Planner 重复生成了已经执行过的计划，继续执行不会产生新证据。"
                    "请补充信息或提供其他公开岗位来源。"
                )
                run_repository.finish_run(
                    db,
                    run,
                    status=RunStatus.waiting_user,
                    final_summary=question,
                )
                run_repository.append_event(
                    db,
                    run_id=run.id,
                    event_type="replan_duplicate_guard",
                    payload_json={
                        "reason_code": "repeated_plan_fingerprint",
                        **build_terminal_contract(
                            error_code="repeated_plan_fingerprint",
                            source_role="planner",
                            phase="planning",
                        ).as_payload(),
                    },
                )
                return AgentRunResult(run.id, RunStatus.waiting_user, question, None)
            accepted_plan_fingerprints[plan_fingerprint] = (
                accepted_plan_fingerprints.get(plan_fingerprint, 0) + 1
            )
            revision += 1
            run_repository.set_run_complexity(db, run, plan.complexity)
            persisted_plan = run_repository.create_plan(
                db,
                run_id=run.id,
                revision=revision,
                complexity=plan.complexity,
                plan_json=plan.model_dump(mode="json"),
            )
            run_repository.append_event(
                db,
                run_id=run.id,
                event_type="plan_created",
                payload_json={
                    "revision": revision,
                    "complexity": plan.complexity.value,
                    "step_count": len(plan.steps),
                },
            )
            self._checkpoint(db)
            final_summary: str | None = None
            replan_feedback: str | None = None
            step_outputs: dict[str, list[dict[str, str]]] = {}
            for sequence, plan_step in enumerate(plan.steps, start=1):
                step = run_repository.create_step(
                    db,
                    run_id=run.id,
                    plan_id=persisted_plan.id,
                    sequence=sequence,
                    objective=plan_step.objective,
                    allowed_skills=plan_step.allowed_skills,
                )
                self._checkpoint(db)
                if sequence == 1 and revision == 1:
                    self._persist_seeded_public_evidence(
                        db=db,
                        run_id=run.id,
                        step=step,
                        task=planning_task,
                    )
                try:
                    if self._skills is not None:
                        port_error = self._skills.validate_step_ports(plan_step)
                        if port_error:
                            raise StepDependencyError(port_error)
                    step_task, step_context = self._prepare_step_inputs(
                        task=planning_task,
                        context=context,
                        plan_step=plan_step,
                        step_outputs=step_outputs,
                    )
                except StepDependencyError as error:
                    run_repository.append_event(
                        db,
                        run_id=run.id,
                        event_type="step_dependency_gate_failed",
                        payload_json={
                            "sequence": sequence,
                            "step_id": plan_step.step_id,
                            "depends_on": plan_step.depends_on,
                            "inputs": [
                                input_ref.model_dump(mode="json")
                                for input_ref in plan_step.inputs
                            ],
                            "error": str(error),
                        },
                    )
                    outcome = self._request_replan(
                        db,
                        run.id,
                        step,
                        feedback=str(error),
                        summary=str(error),
                        output_artifact_refs=[],
                        reason=ReplanReason.DEPENDENCY_UNAVAILABLE,
                    )
                else:
                    outcome = self._run_step(
                        db=db,
                        run_id=run.id,
                        task=step_task,
                        plan=plan,
                        plan_step=plan_step,
                        persisted_step=step,
                        context=step_context,
                        trace=trace,
                        tool_budget=tool_budget,
                        turn_budget=turn_budget,
                        model_budget=model_budget,
                        deadline=deadline,
                        replans=replans,
                    )
                if outcome.error_code == _TERMINAL_NEGATIVE_DISCOVERY_CODE:
                    tagged_outputs = self._tag_step_outputs(
                        plan_step, list(step.output_artifact_refs_json or [])
                    )
                    step.output_artifact_refs_json = tagged_outputs
                    final_summary = outcome.summary
                    run_repository.finish_run(
                        db,
                        run,
                        status=RunStatus.succeeded,
                        final_summary=final_summary,
                    )
                    run_repository.append_event(
                        db,
                        run_id=run.id,
                        event_type="run_succeeded",
                        payload_json={
                            "summary": final_summary,
                            "reason": "terminal_negative_discovery_succeeded",
                        },
                    )
                    return AgentRunResult(
                        run.id, RunStatus.succeeded, final_summary
                    )
                if outcome.error_code == "replan_required":
                    replan_feedback = outcome.summary
                    replan_reason = outcome.replan_reason or ReplanReason.VERIFIER_REPLAN
                    break
                if outcome.status is not RunStatus.running:
                    return outcome
                tagged_outputs = self._tag_step_outputs(
                    plan_step, list(step.output_artifact_refs_json or [])
                )
                step.output_artifact_refs_json = tagged_outputs
                step_outputs[plan_step.step_id] = tagged_outputs
                db.flush()
                final_summary = outcome.summary or final_summary
                blocked_domains = context.metadata.get("blocked_public_domains", [])
                search_hashes = context.metadata.get("public_search_query_hashes", [])
                if (
                    isinstance(blocked_domains, list) and blocked_domains
                ) or (
                    isinstance(search_hashes, list) and search_hashes
                ):
                    planning_context = dict(planning_task.context)
                    if isinstance(blocked_domains, list) and blocked_domains:
                        planning_context["blocked_public_domains"] = sorted(
                            domain for domain in blocked_domains if isinstance(domain, str)
                        )
                    if isinstance(search_hashes, list) and search_hashes:
                        planning_context["public_search_query_hashes"] = sorted(
                            value for value in search_hashes if isinstance(value, str)
                        )
                    planning_task = planning_task.model_copy(
                        update={"context": planning_context}
                    )
                planning_task = self._with_observed_public_evidence(
                    db, planning_task, run.id
                )
                context = self._tool_context(
                    user_id=user_id, run_id=run.id, task=planning_task, db=db
                )
            if replan_feedback is not None:
                replans += 1
                if replans > task.budget.max_replans:
                    question = (
                        replan_feedback
                        or "自动重规划次数已用尽，当前结果仍缺少可核验证据。"
                        "请补充新的公开岗位来源或缺失条件后重试。"
                    )
                    terminal_contract = build_terminal_contract(
                        error_code="replan_budget_exhausted",
                        source_role="runtime",
                        phase="planning",
                    )
                    run_repository.finish_run(
                        db,
                        run,
                        status=RunStatus.waiting_user,
                        final_summary=question,
                        error_code="replan_budget_exhausted",
                    )
                    run_repository.append_event(
                        db,
                        run_id=run.id,
                        event_type="run_needs_user",
                        payload_json={
                            "question": question,
                            **terminal_contract.as_payload(),
                        },
                    )
                    return AgentRunResult(
                        run.id,
                        RunStatus.waiting_user,
                        question,
                        "replan_budget_exhausted",
                    )
                feedback_context = dict(planning_task.context)
                feedback = list(feedback_context.get("verifier_feedback", []))
                feedback.append(replan_feedback)
                feedback_context["verifier_feedback"] = feedback
                next_replan_state = planning_task.replan_state.requested(
                    reason=replan_reason,
                    feedback=replan_feedback,
                    source_revision=revision,
                    count=replans,
                )
                feedback_context["replan_state"] = next_replan_state.model_dump(
                    mode="json"
                )
                planning_task = task.model_copy(
                    update={"context": feedback_context, "replan_state": next_replan_state}
                )
                replan_source = (plan, replan_feedback)
                continue
            run_repository.finish_run(
                db, run, status=RunStatus.succeeded, final_summary=final_summary
            )
            run_repository.append_event(
                db,
                run_id=run.id,
                event_type="run_succeeded",
                payload_json={"summary": final_summary},
            )
            return AgentRunResult(run.id, RunStatus.succeeded, final_summary)

    def create_queued_run(
        self, db: Session, *, user_id: str, task: AgentTaskRequest
    ) -> AgentRun:
        """Persist a run before scheduling it, so SSE has a durable target immediately."""
        return run_repository.create_run(
            db,
            user_id=user_id,
            goal=task.goal,
            allowed_skills=task.allowed_skills,
            context_summary=task.context,
            budget_json=task.budget.model_dump(mode="json"),
            agent_version=self._agent_version,
        )

    def resume(
        self,
        db: Session,
        *,
        user_id: str,
        run_id: str,
        task: AgentTaskRequest,
    ) -> AgentRunResult:
        """Continue a paused Run without resetting its durable operational budget."""
        run = run_repository.get_run_for_owner(
            db, run_id, user_id, for_update=True
        )
        if run is None:
            raise ValueError("agent_run_not_found")
        if run.status is not RunStatus.waiting_user:
            raise ValueError("agent_run_not_waiting_user")
        run_repository.start_run(db, run)
        run_repository.append_event(
            db,
            run_id=run.id,
            event_type="run_resumed",
            payload_json={"user_response_received": True},
        )
        return self.run(db, user_id=user_id, task=task, existing_run=run)

    def recover(
        self,
        db: Session,
        *,
        user_id: str,
        run_id: str,
        task: AgentTaskRequest,
    ) -> AgentRunResult:
        """Replan a process-interrupted running Run from committed evidence only."""
        run = run_repository.get_run_for_owner(
            db, run_id, user_id, for_update=True
        )
        if run is None:
            raise ValueError("agent_run_not_found")
        if run.status is not RunStatus.running:
            raise ValueError("agent_run_not_running")
        run_repository.append_event(
            db,
            run_id=run.id,
            event_type="run_recovery_started",
            payload_json={"strategy": "replan_from_durable_evidence"},
        )
        self._checkpoint(db)
        return self.run(db, user_id=user_id, task=task, existing_run=run)

    def _run_step(
        self,
        *,
        db: Session,
        run_id: str,
        task: AgentTaskRequest,
        plan: ExecutionPlan,
        plan_step: PlanStep,
        persisted_step: AgentStep,
        context: ToolContext,
        trace,
        tool_budget: ToolCallBudget,
        turn_budget: AgentTurnBudget,
        model_budget: ModelCallBudget | None = None,
        deadline: float | None = None,
        replans: int = 0,
    ) -> AgentRunResult:
        """Execute and conditionally verify one agent-defined planned outcome."""
        retries = 0
        if model_budget is None:
            model_budget = ModelCallBudget(
                task.budget.max_model_requests,
                task.budget.max_input_tokens,
                task.budget.max_output_tokens,
            )
        execution_task = task
        prior_observations = []
        prior_artifact_refs: list[dict[str, str]] = []
        last_retry_progress_fingerprint: str | None = None
        auto_extract_attempted = False
        auto_discovery_attempted = False
        seeded_artifact_refs = [
            *self._step_seeded_artifact_refs(
                db=db, run_id=run_id, step=persisted_step
            ),
            *self._resolved_step_artifact_refs(context),
        ]
        while True:
            try:
                execution = self._executor.run(
                    task=execution_task,
                    plan=plan,
                    step=plan_step,
                    context=context,
                    trace=trace,
                    tool_budget=tool_budget,
                    turn_budget=turn_budget,
                    model_budget=model_budget,
                    deadline=deadline,
                    prior_observations=prior_observations,
                )
            except AgentModelGatewayError as error:
                if error.code == "invalid_model_response":
                    # A persistently invalid model completion is a transport
                    # degradation, not a business failure: wait for a human
                    # retry instead of failing the whole run.
                    return self._wait_for_user(
                        db,
                        run_id,
                        persisted_step,
                        "模型输出格式异常，无法继续执行该步骤。请补充信息或重试。",
                    )
                return self._fail_step(db, run_id, persisted_step, error.code)
            self._record_failed_executor_observations(
                db, run_id, persisted_step, execution
            )
            observed_artifact_refs = [
                *seeded_artifact_refs,
                *self._persist_observed_evidence(
                    db, run_id, persisted_step, execution
                ),
            ]
            auto_discovery_observations: list[Any] = []
            auto_discovery_refs: list[dict[str, str]] = []
            if not auto_discovery_attempted:
                auto_discovery_observations, auto_discovery_refs = (
                    self._auto_recover_discovery_evidence(
                        db=db,
                        run_id=run_id,
                        task=task,
                        plan_step=plan_step,
                        persisted_step=persisted_step,
                        context=context,
                        observations=execution.observations,
                        artifact_refs=observed_artifact_refs,
                        tool_budget=tool_budget,
                    )
                )
                if auto_discovery_observations or auto_discovery_refs:
                    auto_discovery_attempted = True
            if auto_extract_attempted:
                auto_observations, auto_artifact_refs = [], []
            else:
                auto_observations, auto_artifact_refs = self._auto_extract_jd_details(
                    db=db,
                    run_id=run_id,
                    task=task,
                    plan_step=plan_step,
                    persisted_step=persisted_step,
                    context=context,
                    artifact_refs=[
                        *prior_artifact_refs,
                        *observed_artifact_refs,
                        *auto_discovery_refs,
                    ],
                    tool_budget=tool_budget,
                )
                if auto_observations or auto_artifact_refs:
                    auto_extract_attempted = True
            auto_observations = [
                *auto_discovery_observations,
                *auto_observations,
            ]
            auto_artifact_refs = [
                *auto_discovery_refs,
                *auto_artifact_refs,
            ]
            deliverable_observations, deliverable_refs = (
                self._auto_build_role_deliverable(
                    db=db,
                    run_id=run_id,
                    task=task,
                    plan_step=plan_step,
                    persisted_step=persisted_step,
                    context=context,
                    artifact_refs=[
                        *prior_artifact_refs,
                        *observed_artifact_refs,
                        *auto_artifact_refs,
                    ],
                    tool_budget=tool_budget,
                )
            )
            auto_observations.extend(deliverable_observations)
            auto_artifact_refs.extend(deliverable_refs)
            if auto_observations:
                execution = execution.model_copy(
                    update={
                        "observations": [*execution.observations, *auto_observations],
                    }
                )
            observed_artifact_refs.extend(auto_artifact_refs)
            # A verifier retry continues the same planned outcome. Keep prior
            # tool-backed observations for independent verification, but persist
            # only the new observation set from this Executor invocation.
            execution = execution.model_copy(
                update={
                    "observations": [*prior_observations, *execution.observations],
                    "artifact_refs": [*prior_artifact_refs, *observed_artifact_refs],
                }
            )
            if self._is_complete_official_negative_discovery(
                task, plan, plan_step, execution
            ) and not self._step_has_blocked_evidence(
                plan_step, execution.observations, execution.artifact_refs
            ):
                summary = self._official_negative_discovery_summary(
                    task, execution
                )
                completed_execution = execution.model_copy(
                    update={
                        "status": "succeeded",
                        "error_code": None,
                        "summary": summary,
                    }
                )
                self._rescue_step_succeeded(
                    db,
                    run_id,
                    persisted_step,
                    completed_execution,
                    event_type="terminal_negative_discovery_succeeded",
                    reason="complete_official_source_scan_zero_match",
                )
                return AgentRunResult(
                    run_id,
                    RunStatus.running,
                    summary,
                    _TERMINAL_NEGATIVE_DISCOVERY_CODE,
                )
            if (
                execution.status == "failed"
                and auto_artifact_refs
                and self._step_contract_met(
                    plan_step, execution.observations, execution.artifact_refs
                )
                and not self._step_has_blocked_evidence(
                    plan_step, execution.observations, execution.artifact_refs
                )
            ):
                # A deterministic runtime recovery may finish the declared
                # contract even when the model's terminal response exhausted
                # its budget. Preserve the tool-backed deliverable and avoid
                # discarding it as a model transport failure.
                execution = execution.model_copy(
                    update={
                        "status": "succeeded",
                        "error_code": None,
                        "summary": execution.summary
                        or "已由确定性工具恢复并完成步骤交付。",
                    }
                )
            if any(
                observation.status == "failed"
                and observation.error_code == "tool_skill_forbidden"
                for observation in execution.observations
            ):
                return self._request_replan(
                    db,
                    run_id,
                    persisted_step,
                    feedback=(
                        "当前步骤的 Skill 范围无法执行模型选择的工具；"
                        "请将计划拆分为每步一个 Skill 后重试。"
                    ),
                    summary="步骤 Skill 范围冲突，已停止重复工具调用并请求重规划。",
                    output_artifact_refs=execution.artifact_refs,
                    reason=ReplanReason.TOOL_SCOPE_CONFLICT,
                )
            if execution.status == "needs_user":
                if (
                    self._intermediate_routing_contract_met(
                        plan, plan_step, execution.artifact_refs
                    )
                ):
                    # The current step owns only the company/source routing
                    # artifact. A model may overreach into the downstream JD
                    # fetch and then ask the human about that later step. Once
                    # the declared routing output is tool-backed, preserve it
                    # and let the planned fetch step own any access block.
                    return self._rescue_step_succeeded(
                        db,
                        run_id,
                        persisted_step,
                        execution,
                        event_type="executor_rescue_succeeded",
                        reason="intermediate_routing_contract_met",
                    )
                if (
                    any(
                        observation.error_code == "target_role_mismatch"
                        for observation in execution.observations
                    )
                    and replans < task.budget.max_replans
                ):
                    return self._request_replan(
                        db,
                        run_id,
                        persisted_step,
                        feedback=(
                            "目标 JD 与用户请求的岗位角色不匹配；请先通过已处理候选 URL 或公开搜索"
                            "发现角色匹配的 JD，再执行后续职业交付。"
                        ),
                        summary="目标岗位角色不匹配，已请求重新发现匹配证据。",
                        output_artifact_refs=execution.artifact_refs,
                        reason=ReplanReason.DEPENDENCY_UNAVAILABLE,
                    )
                if (
                    self._step_contract_met(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and not self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                ):
                    # The executor asked the human even though the step's
                    # deliverable is already tool-backed (post-deliverable
                    # stall pattern). The request is a hand-off the agents
                    # already finished; terminate the step as succeeded with
                    # an explicit rescue event instead of a spurious
                    # waiting_user. Blocked evidence (login/captcha/anti-bot/
                    # OCR-off) always keeps the human hand-off.
                    return self._rescue_step_succeeded(
                        db,
                        run_id,
                        persisted_step,
                        execution,
                        event_type="executor_rescue_succeeded",
                        reason="needs_user_deliverable_persisted",
                    )
                if (
                    execution.error_code == "deep_executor_invalid_response"
                    and not self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and replans < task.budget.max_replans
                    and not task.replan_state.conversion_used(
                        ReplanReason.RETRY_CONTRACT_EXHAUSTED
                    )
                ):
                    # The Deep Executor produced no parseable terminal and the
                    # step's deliverable contract is still unmet. Convert to a
                    # bounded REPLAN (once per run, guarded by the replan
                    # budget and the shared retry-exhaustion marker) so the
                    # planner can restructure the step instead of ending at a
                    # human hand-off for a purely mechanical parse failure.
                    return self._request_replan(
                        db,
                        run_id,
                        persisted_step,
                        feedback=(
                            "执行器未输出可解析的完成终态且交付契约未满足；"
                            "请重组该步骤、缩小范围或更换交付路线。"
                        ),
                        summary=f"执行器终态不可解析 {_RETRY_REPLAN_MARKER}",
                        output_artifact_refs=execution.artifact_refs,
                        reason=ReplanReason.RETRY_CONTRACT_EXHAUSTED,
                    )
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    execution.user_question,
                    output_artifact_refs=observed_artifact_refs,
                    terminal_contract=build_terminal_contract(
                        observations=execution.observations,
                        source_role="executor",
                        phase="execution",
                        artifact_count=len(observed_artifact_refs),
                    ),
                )
            if execution.error_code == "wall_clock_budget_exhausted":
                if self._intermediate_routing_contract_met(
                    plan, plan_step, execution.artifact_refs
                ):
                    # The model timed out only after persisting the current
                    # step's bounded routing list.  That list is the complete
                    # contract for this non-final step; preserve it and let
                    # the next planned step own JD fetching instead of asking
                    # the user to resume for an unnecessary completion token.
                    return self._rescue_step_succeeded(
                        db,
                        run_id,
                        persisted_step,
                        execution,
                        event_type="executor_rescue_succeeded",
                        reason="wall_clock_intermediate_routing_contract_met",
                        output_artifact_refs=observed_artifact_refs,
                    )
                # Wall-clock exhaustion is a transport/resource pause, not a
                # business failure: resume() recomputes a fresh deadline and
                # the executor continues the remaining turns. Route to a
                # recoverable waiting_user instead of failing the run, and
                # surface the stable error_code for observability.
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    "运行时间预算耗尽（模型响应偏慢），该步骤尚未完成。恢复运行将获得新的时间窗口继续。",
                    output_artifact_refs=observed_artifact_refs,
                    error_code="wall_clock_budget_exhausted",
                )
            if (
                execution.status == "failed"
                and execution.error_code == "deep_executor_invalid_response"
                and self._step_contract_met(
                    plan_step, execution.observations, execution.artifact_refs
                )
                and not self._step_has_blocked_evidence(
                    plan_step, execution.observations, execution.artifact_refs
                )
            ):
                return self._rescue_step_succeeded(
                    db,
                    run_id,
                    persisted_step,
                    execution,
                    event_type="executor_rescue_succeeded",
                    reason="invalid_terminal_deliverable_persisted",
                    output_artifact_refs=observed_artifact_refs,
                )
            if (
                execution.status == "succeeded"
                and self._intermediate_routing_contract_met(
                    plan, plan_step, execution.artifact_refs
                )
            ):
                # A non-final routing list is consumed and independently
                # checked by the next planned discovery step. Once the
                # deterministic source/URL artifact exists, another model
                # verification turn adds latency but no new evidence.
                return self._rescue_step_succeeded(
                    db,
                    run_id,
                    persisted_step,
                    execution,
                    event_type="executor_rescue_succeeded",
                    reason="intermediate_routing_contract_met",
                    output_artifact_refs=observed_artifact_refs,
                )
            if execution.status != "succeeded":
                return self._fail_step(
                    db,
                    run_id,
                    persisted_step,
                    execution.error_code or "executor_failed",
                    output_artifact_refs=observed_artifact_refs,
                )
            if not self._requires_verification(plan, plan_step):
                if self._completion_gate_rejected(
                    plan_step,
                    execution.observations,
                    summary=execution.summary,
                    artifact_refs=execution.artifact_refs,
                ):
                    if (
                        not self._step_has_blocked_evidence(
                            plan_step, execution.observations, execution.artifact_refs
                        )
                        and replans < task.budget.max_replans
                        and not task.replan_state.conversion_used(
                            ReplanReason.RETRY_CONTRACT_EXHAUSTED
                        )
                    ):
                        # The executor declared success but the deterministic
                        # completion gate rejected the deliverable. Without
                        # blocked evidence the human cannot fix what a
                        # restructured step can: convert to a bounded REPLAN
                        # (once per run, guarded by the replan budget); a
                        # repeated rejection or any blocked evidence keeps
                        # the human hand-off.
                        return self._request_replan(
                            db,
                            run_id,
                            persisted_step,
                            feedback=(
                                "交付物未通过完成门禁：工具未产生可核验的交付物，"
                                "请重组该步骤以产生满足契约的主 artifact。"
                            ),
                            summary=(
                                f"交付物未通过完成门禁 {_RETRY_REPLAN_MARKER}"
                            ),
                            output_artifact_refs=execution.artifact_refs,
                            reason=ReplanReason.RETRY_CONTRACT_EXHAUSTED,
                        )
                    return self._wait_for_user(
                        db,
                        run_id,
                        persisted_step,
                        "工具未产生可核验的交付物，当前总结不能视为完成。"
                        "请提供可公开访问的岗位页面或补充必要信息后重试。",
                        output_artifact_refs=observed_artifact_refs,
                    )
                run_repository.finish_step(
                    db,
                    persisted_step,
                    status=StepStatus.succeeded,
                    output_artifact_refs=execution.artifact_refs,
                )
                run_repository.append_event(
                    db,
                    run_id=run_id,
                    event_type="step_succeeded",
                    payload_json={"sequence": persisted_step.sequence},
                )
                return AgentRunResult(run_id, RunStatus.running, execution.summary)
            try:
                verification = self._verifier.run(
                    task=task,
                    plan=plan,
                    step=plan_step,
                    execution=execution,
                    context=context,
                    trace=trace,
                    tool_budget=tool_budget,
                    turn_budget=turn_budget,
                    deadline=deadline,
                )
            except AgentModelGatewayError as error:
                if error.code == "invalid_model_response":
                    if (
                        self._step_contract_met(
                            plan_step, execution.observations, execution.artifact_refs
                        )
                        and not self._step_has_blocked_evidence(
                            plan_step, execution.observations, execution.artifact_refs
                        )
                    ):
                        # The verifier transport degraded after the step's
                        # deliverable was already tool-backed (invalid
                        # model-output pattern). The contract is the same
                        # evidence the verifier would have checked; terminate
                        # the step as succeeded with an explicit rescue event
                        # instead of a spurious waiting_user. Blocked evidence
                        # (login/captcha/anti-bot/OCR-off) always keeps the
                        # human hand-off.
                        return self._rescue_step_succeeded(
                            db,
                            run_id,
                            persisted_step,
                            execution,
                            event_type="verifier_rescue_succeeded",
                            reason="invalid_model_response_contract_met",
                        )
                    # The verifier cannot produce a machine decision; route the
                    # step to a human check rather than failing the run.
                    verification = VerifierResult(
                        decision=VerificationDecision.NEED_USER,
                        feedback="核验模型输出格式异常，无法独立核验该步骤产出，请人工确认。",
                    )
                else:
                    return self._fail_step(
                        db,
                        run_id,
                        persisted_step,
                        error.code,
                        output_artifact_refs=execution.artifact_refs,
                    )
            if verification.error_code == "wall_clock_budget_exhausted":
                if (
                    self._step_contract_met(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and not self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                ):
                    # The deadline expired only at the model verification
                    # boundary. If the same deterministic evidence contract
                    # is already satisfied, preserve the completed artifact
                    # instead of asking the user to resume solely for a PASS
                    # token. Any blocked or incomplete evidence still pauses.
                    return self._rescue_step_succeeded(
                        db,
                        run_id,
                        persisted_step,
                        execution,
                        event_type="verifier_rescue_succeeded",
                        reason="wall_clock_budget_contract_met",
                    )
                # Wall-clock exhaustion before a verification decision is a
                # transport/resource pause: the step's executor work is already
                # persisted, and resume() recomputes a fresh deadline so the
                # verifier can run its remaining calls. Route to a recoverable
                # waiting_user instead of failing the run, surfacing the stable
                # error_code for observability.
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    "运行时间预算耗尽（模型响应偏慢），步骤产出待核验。恢复运行将获得新的时间窗口完成核验。",
                    output_artifact_refs=execution.artifact_refs,
                    error_code="wall_clock_budget_exhausted",
                )
            if verification.error_code:
                return self._fail_step(
                    db,
                    run_id,
                    persisted_step,
                    verification.error_code,
                    output_artifact_refs=execution.artifact_refs,
                )
            if verification.decision is VerificationDecision.PASS:
                diagnostics = (
                    self._skills.completion_evidence_diagnostics(
                        plan_step,
                        execution.observations,
                        summary=execution.summary,
                        artifact_refs=execution.artifact_refs,
                    )
                    if self._skills is not None
                    else None
                )
                if diagnostics is not None and not diagnostics["gate_passed"]:
                    durable_refs = self._step_seeded_artifact_refs(
                        db=db, run_id=run_id, step=persisted_step
                    )
                    if durable_refs:
                        merged_refs = list(
                            {
                                (ref.get("artifact_id"), ref.get("content_hash")): ref
                                for ref in [*execution.artifact_refs, *durable_refs]
                            }.values()
                        )
                        durable_diagnostics = self._skills.completion_evidence_diagnostics(
                            plan_step,
                            execution.observations,
                            summary=execution.summary,
                            artifact_refs=merged_refs,
                        )
                        if durable_diagnostics["gate_passed"]:
                            execution = execution.model_copy(
                                update={"artifact_refs": merged_refs}
                            )
                            return self._rescue_step_succeeded(
                                db,
                                run_id,
                                persisted_step,
                                execution,
                                event_type="runtime_durable_contract_rescue",
                                reason="persisted_evidence_contract_met",
                                output_artifact_refs=merged_refs,
                            )
                    run_repository.append_event(
                        db,
                        run_id=run_id,
                        event_type="verification_pass_rejected_by_contract",
                        payload_json={
                            "sequence": persisted_step.sequence,
                            "reason": "deterministic_contract_not_satisfied",
                            "evidence_diagnostics": diagnostics,
                        },
                    )
                    return self._wait_for_user(
                        db,
                        run_id,
                        persisted_step,
                        "Verifier 的 PASS 未通过 Skill 的确定性交付契约，不能将该步骤标记为完成。"
                        "请补充工具产出或人工确认后重试。",
                        output_artifact_refs=execution.artifact_refs,
                        terminal_contract=build_terminal_contract(
                            error_code=(
                                "domain_temporarily_blocked"
                                if self._run_has_external_fetch_block(db, run_id)
                                else "verification_failed"
                            ),
                            observations=execution.observations,
                            source_role="runtime",
                            phase="verification",
                            contract_met=bool(diagnostics["contract_met"]),
                            artifact_count=len(execution.artifact_refs),
                            evidence_diagnostics=diagnostics,
                        ),
                    )
                run_repository.finish_step(
                    db,
                    persisted_step,
                    status=StepStatus.succeeded,
                    output_artifact_refs=execution.artifact_refs,
                )
                run_repository.append_event(
                    db,
                    run_id=run_id,
                    event_type="step_succeeded",
                    payload_json={"sequence": persisted_step.sequence},
                )
                run_repository.append_event(
                    db,
                    run_id=run_id,
                    event_type="verification_passed",
                    payload_json={"sequence": persisted_step.sequence},
                )
                return AgentRunResult(run_id, RunStatus.running, execution.summary)
            if verification.decision is VerificationDecision.RETRY_EXECUTOR:
                progress_fingerprint = _execution_progress_fingerprint(execution)
                if (
                    last_retry_progress_fingerprint is not None
                    and progress_fingerprint == last_retry_progress_fingerprint
                ):
                    feedback = verification.feedback or ""
                    return self._wait_for_user(
                        db,
                        run_id,
                        persisted_step,
                        "Verifier 重试没有产生新的工具证据，继续调用将重复当前失败路径。"
                        + (f"当前反馈：{feedback}。" if feedback else "")
                        + "请提供新的岗位来源或补充缺失字段后重试。",
                        output_artifact_refs=execution.artifact_refs,
                        error_code="no_progress_duplicate",
                        terminal_contract=build_terminal_contract(
                            error_code="no_progress_duplicate",
                            source_role="verifier",
                            phase="verification",
                            artifact_count=len(execution.artifact_refs),
                        ),
                    )
                last_retry_progress_fingerprint = progress_fingerprint
                retries += 1
                run_repository.append_event(
                    db,
                    run_id=run_id,
                    event_type="verification_retry_executor",
                    payload_json={
                        "sequence": persisted_step.sequence,
                        "feedback": verification.feedback,
                    },
                )
                if (
                    self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and not self._step_contract_met(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                ):
                    # The step's evidence is blocked (login/captcha/anti-bot/
                    # OCR-off or a deterministic per-URL failure) and the
                    # deliverable cannot be satisfied from unblocked evidence.
                    # Re-invoking the executor would only re-burn turns on the
                    # same blocked calls, so downgrade the retry loop to one
                    # clean human hand-off - the human stays in the loop and
                    # resume() remains available after they act.
                    run_repository.append_event(
                        db,
                        run_id=run_id,
                        event_type="verification_retry_downgraded",
                        payload_json={
                            "sequence": persisted_step.sequence,
                            "reason": "blocked_evidence",
                            "feedback": verification.feedback,
                        },
                    )
                    return self._wait_for_user(
                        db,
                        run_id,
                        persisted_step,
                        "步骤证据被访问限制阻断（登录/验证码/反爬/OCR 未启用等），"
                        "自动重试无法取得新证据："
                        + verification.feedback
                        + "。请人工确认该来源的岗位信息，或提供可公开访问的页面后重试。",
                        output_artifact_refs=execution.artifact_refs,
                        terminal_contract=build_terminal_contract(
                            observations=execution.observations,
                            source_role="verifier",
                            phase="verification",
                            artifact_count=len(execution.artifact_refs),
                        ),
                    )
                if any(
                    observation.status == "failed"
                    and observation.error_code
                    in {"tool_skill_forbidden", "unknown_tool"}
                    for observation in execution.observations
                ):
                    # R013: the verifier RETRY demands a tool-backed deliverable
                    # that this step's skill scope permanently excludes (the
                    # call was rejected as tool_skill_forbidden / unknown_tool).
                    # A same-step re-invocation is provably unsatisfiable - the
                    # executor cannot call the scoped-out tool - so it would
                    # only re-burn turns on the same rejected call. Route to the
                    # replan path so the planner can restructure the step
                    # instead of looping on an impossible retry.
                    return self._request_replan(
                        db,
                        run_id,
                        persisted_step,
                        feedback=verification.feedback,
                        summary=verification.feedback,
                        output_artifact_refs=execution.artifact_refs,
                        reason=ReplanReason.TOOL_SCOPE_CONFLICT,
                    )
                if retries <= task.budget.max_replans:
                    prior_observations = execution.observations
                    prior_artifact_refs = execution.artifact_refs
                    retry_context = dict(execution_task.context)
                    feedback = list(retry_context.get("verifier_feedback", []))
                    # VerifierResult schema rejects non-PASS decisions without
                    # non-empty feedback, so the False branch here is unreachable.
                    if verification.feedback:  # pragma: no cover
                        feedback.append(verification.feedback)
                    retry_context["verifier_feedback"] = feedback
                    execution_task = execution_task.model_copy(
                        update={
                            "context": retry_context,
                            "execution_state": execution.execution_state,
                        }
                    )
                    execution_task = self._with_observed_public_evidence(
                        db, execution_task, run_id
                    )
                    context = self._tool_context(
                        user_id=context.user_id,
                        run_id=run_id,
                        task=execution_task,
                        db=db,
                    )
                    continue
                # Same-step retries cannot satisfy the verifier: more retries
                # would only burn turns on a stuck loop. When the step's
                # deterministic contract is already tool-backed and unblocked
                # and the replan budget still allows a restructure, route to a
                # bounded REPLAN (N3) so the planner can rebuild the step
                # instead of ending the run at a human hand-off the agents had
                # already produced. Guarded by the replan budget and a
                # once-per-run marker exactly like the NEED_USER conversion
                # (R009/R018/R033): a second identical exhaustion after the
                # replan keeps the human hand-off. Blocked evidence always
                # keeps the human hand-off.
                if (
                    self._step_contract_met(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and not self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and replans < task.budget.max_replans
                    and not task.replan_state.conversion_used(
                        ReplanReason.RETRY_CONTRACT_EXHAUSTED
                    )
                ):
                    return self._request_replan(
                        db,
                        run_id,
                        persisted_step,
                        feedback=verification.feedback,
                        summary=f"{verification.feedback} {_RETRY_REPLAN_MARKER}",
                        output_artifact_refs=execution.artifact_refs,
                        reason=ReplanReason.RETRY_CONTRACT_EXHAUSTED,
                    )
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    "多次重试后仍未通过核验：" + verification.feedback + "。请人工确认产出，或补充缺失信息后重试。",
                    output_artifact_refs=execution.artifact_refs,
                )
            if verification.decision is VerificationDecision.NEED_USER:
                if (
                    self._intermediate_routing_contract_met(
                        plan, plan_step, execution.artifact_refs
                    )
                ):
                    return self._rescue_step_succeeded(
                        db,
                        run_id,
                        persisted_step,
                        execution,
                        event_type="verifier_rescue_succeeded",
                        reason="intermediate_routing_contract_met",
                    )
                if (
                    self._step_contract_met(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and not self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and replans < task.budget.max_replans
                    and not task.replan_state.conversion_used(
                        ReplanReason.NEED_USER_CONTRACT
                    )
                ):
                    # A NEED_USER over an already complete, unblocked Skill
                    # deliverable cannot be repaired by waiting for the human
                    # alone: the evidence is tool-backed and the contract is
                    # satisfied. Route to a bounded REPLAN so the planner can
                    # restructure the step instead of ending the run at a
                    # hand-off the agents already produced (R009/R018/R033/
                    # R013). Guarded by the replan budget and the once-per-run
                    # conversion marker: a second identical NEED_USER after
                    # the replan keeps the human hand-off. Blocked evidence
                    # and unmet contracts never convert.
                    return self._request_replan(
                        db,
                        run_id,
                        persisted_step,
                        feedback=verification.feedback,
                        summary=f"{verification.feedback} {_NEEDS_USER_REPLAN_MARKER}",
                        output_artifact_refs=execution.artifact_refs,
                        reason=ReplanReason.NEED_USER_CONTRACT,
                    )
                if (
                    self._step_contract_met(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and not self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and self._is_verified_zero_match_discovery(
                        task, plan_step, execution
                    )
                ):
                    # A user asking whether a matching job exists has already
                    # received a complete answer when exhaustive, persisted
                    # public evidence proves that the match set is empty.
                    # This rescue runs only after the bounded NEED_USER replan
                    # path above is unavailable/used, so it cannot suppress a
                    # useful structural repair on the first verifier objection.
                    return self._rescue_step_succeeded(
                        db,
                        run_id,
                        persisted_step,
                        execution,
                        event_type="verifier_rescue_succeeded",
                        reason="verified_negative_discovery_contract_met",
                    )
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    verification.feedback,
                    output_artifact_refs=execution.artifact_refs,
                    terminal_contract=build_terminal_contract(
                        observations=execution.observations,
                        source_role="verifier",
                        phase="verification",
                        artifact_count=len(execution.artifact_refs),
                    ),
                )
            if verification.decision is VerificationDecision.FAIL:
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    "Verifier 判定当前产出不满足任务要求："
                    + (verification.feedback or "请补充岗位证据后重试。"),
                    output_artifact_refs=execution.artifact_refs,
                    error_code="verification_failed",
                    terminal_contract=build_terminal_contract(
                        error_code="verification_failed",
                        source_role="verifier",
                        phase="verification",
                        artifact_count=len(execution.artifact_refs),
                    ),
                )
            return self._request_replan(
                db,
                run_id,
                persisted_step,
                feedback=verification.feedback,
                summary=verification.feedback,
                output_artifact_refs=execution.artifact_refs,
                reason=ReplanReason.VERIFIER_REPLAN,
            )

    def _requires_verification(self, plan: ExecutionPlan, plan_step: PlanStep) -> bool:
        if self._skills is None:
            return plan_step.requires_verification or plan.complexity.value in {"L3", "L4"}
        return self._skills.requires_verification(plan_step, plan.complexity.value)

    @staticmethod
    def _is_intermediate_routing_step(
        plan: ExecutionPlan, plan_step: PlanStep
    ) -> bool:
        """True only for a non-final step whose sole output is source routing."""
        try:
            step_index = next(
                index
                for index, candidate in enumerate(plan.steps)
                if candidate.step_id == plan_step.step_id
            )
        except StopIteration:
            return False
        output_types = {
            output.artifact_type
            for output in plan_step.outputs
            if output.artifact_type
        }
        return (
            step_index < len(plan.steps) - 1
            and bool(plan_step.outputs)
            and output_types == {"job_search_results"}
        )

    @classmethod
    def _intermediate_routing_contract_met(
        cls,
        plan: ExecutionPlan,
        plan_step: PlanStep,
        artifact_refs: list[dict[str, Any]],
    ) -> bool:
        """Accept only a non-empty, URL-bearing routing artifact mid-plan.

        The job-discovery Skill's normal completion contract intentionally
        requires a captured JD page.  A non-final step that explicitly emits
        only ``job_search_results`` has a narrower responsibility: hand a
        finite set of public URLs to the following fetch step.  The runtime
        marks such refs semantic-valid while persisting the registered search
        or sheet observation; model-proposed URLs never reach this path.
        """
        if not cls._is_intermediate_routing_step(plan, plan_step):
            return False
        return any(
            ref.get("artifact_type") == "job_search_results"
            and ref.get("tool")
            in {"query-career-sheet-records", "search-public-job-pages"}
            and ref.get("semantic_valid") == "true"
            for ref in artifact_refs
            if isinstance(ref, dict)
        )

    @staticmethod
    def _prepare_step_inputs(
        *,
        task: AgentTaskRequest,
        context: ToolContext,
        plan_step: PlanStep,
        step_outputs: dict[str, list[dict[str, str]]],
    ) -> tuple[AgentTaskRequest, ToolContext]:
        """Resolve typed context/artifact refs before the Executor is called."""
        resolved_context: dict[str, Any] = {}
        resolved_artifacts: dict[str, list[dict[str, str]]] = {}
        resolved_private_inputs: set[str] = set()
        for input_ref in plan_step.inputs:
            if input_ref.kind == "context":
                value = _task_input_value(task, input_ref.name)
                if value is None:
                    raise StepDependencyError(
                        f"step {plan_step.step_id} requires context input '{input_ref.name}'"
                    )
                if _is_private_task_input(input_ref.name):
                    # The private projection is already supplied separately to
                    # the Executor. Record only that the declared input was
                    # resolved; never echo its value into task.context, which
                    # is visible in the generic model decision state.
                    resolved_private_inputs.add(input_ref.name)
                else:
                    resolved_context[input_ref.name] = value
                continue
            source = step_outputs.get(input_ref.from_step or "", [])
            if not source:
                raise StepDependencyError(
                    f"step {plan_step.step_id} requires artifact '{input_ref.name}' "
                    f"from step '{input_ref.from_step}'"
                )
            matches = [
                item
                for item in source
                if input_ref.name
                in {
                    item.get("output_name"),
                    item.get("artifact_type"),
                    item.get("tool"),
                    item.get("artifact_id"),
                }
                and (
                    input_ref.artifact_type is None
                    or item.get("artifact_type") == input_ref.artifact_type
                )
            ]
            if not matches and len(source) == 1 and input_ref.artifact_type is None:
                matches = source
            if (
                input_ref.artifact_type == "job_search_results"
                and "job-discovery" in plan_step.allowed_skills
            ):
                # A Planner may call the output of a discovery step
                # ``job_search_results`` even after the Executor has already
                # upgraded that source into public pages or structured JDs.
                # Reuse only those same-run trusted discovery artifacts; do
                # not widen arbitrary Skill or cross-domain ports.
                compatible = [
                    item
                    for item in source
                    if item.get("artifact_type")
                    in {"public_job_page", "structured_job_details"}
                ]
                if compatible:
                    matches = [*matches, *compatible]
            if not matches:
                raise StepDependencyError(
                    f"step {plan_step.step_id} cannot resolve artifact '{input_ref.name}' "
                    f"from step '{input_ref.from_step}'"
                )
            resolved_artifacts[input_ref.name] = matches
        if not resolved_context and not resolved_artifacts:
            return task, context
        input_payload = {
            "context": resolved_context,
            "artifacts": resolved_artifacts,
            "private_inputs": sorted(resolved_private_inputs),
        }
        step_task = task.model_copy(
            update={
                "context": {
                    **task.context,
                    "resolved_step_inputs": input_payload,
                }
            }
        )
        step_metadata = dict(context.metadata)
        step_metadata["resolved_step_inputs"] = input_payload
        return step_task, ToolContext(
            user_id=context.user_id,
            run_id=context.run_id,
            metadata=step_metadata,
        )

    @staticmethod
    def _tag_step_outputs(
        plan_step: PlanStep, refs: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Persist semantic output names alongside real artifact pointers."""
        if not plan_step.outputs:
            return refs
        tagged: list[dict[str, str]] = []
        for index, ref in enumerate(refs):
            item = {key: str(value) for key, value in ref.items() if value is not None}
            selected = next(
                (
                    output
                    for output in plan_step.outputs
                    if output.artifact_type
                    and output.artifact_type == item.get("artifact_type")
                ),
                None,
            )
            if selected is None:
                selected = plan_step.outputs[0] if len(plan_step.outputs) == 1 else plan_step.outputs[index % len(plan_step.outputs)]
            item["output_name"] = selected.name
            if selected.artifact_type and "artifact_type" not in item:
                item["artifact_type"] = selected.artifact_type
            tagged.append(item)
        return tagged

    def _step_contract_met(
        self,
        step: PlanStep,
        observations: list,
        artifact_refs: list[dict[str, Any]] | None = None,
    ) -> bool:
        if self._skills is None:
            return legacy_step_contract_met(step, observations)
        # Runtime rescue/replan decisions must use the same semantic gate as
        # the final completion gate. The legacy observation-only check is
        # intentionally retained on SkillRegistry for compatibility tests,
        # but it can treat an empty report-shaped output as a deliverable.
        return bool(
            self._skills.completion_evidence_diagnostics(
                step,
                observations,
                summary="contract-check",
                artifact_refs=artifact_refs or [],
            )["contract_met"]
        )

    @staticmethod
    def _is_verified_zero_match_discovery(
        task: AgentTaskRequest,
        step: PlanStep,
        execution: ExecutorResult,
    ) -> bool:
        """Recognize an evidenced negative answer to an existence question."""
        if set(step.allowed_skills) != {"job-discovery"}:
            return False
        compact_goal = re.sub(r"\s+", "", task.goal).lower()
        if not any(
            marker in compact_goal for marker in _DISCOVERY_EXISTENCE_QUERY_MARKERS
        ):
            return False
        compact_summary = re.sub(r"\s+", "", execution.summary or "").lower()
        if not any(marker in compact_summary for marker in _VERIFIED_ZERO_MATCH_MARKERS):
            return False
        return any(
            ref.get("artifact_type") in _DISCOVERY_EVIDENCE_ARTIFACT_TYPES
            for ref in execution.artifact_refs
            if isinstance(ref, dict)
        )

    @staticmethod
    def _is_complete_official_negative_discovery(
        task: AgentTaskRequest,
        plan: ExecutionPlan,
        step: PlanStep,
        execution: ExecutorResult,
    ) -> bool:
        """Close a pure discovery plan when an official scoped scan proves zero.

        This is narrower than a generic ``search_empty``: every planned step
        must belong only to job-discovery, the task must ask an existence
        question, and the successful observation must pass the reviewed
        source-specific semantic checker. Downstream fetch/extract steps have
        no input after a verified empty set and are therefore intentionally
        not executed.
        """
        if set(step.allowed_skills) != {"job-discovery"}:
            return False
        if not plan.steps or any(
            set(candidate.allowed_skills) != {"job-discovery"}
            for candidate in plan.steps
        ):
            return False
        compact_goal = re.sub(r"\s+", "", task.goal).lower()
        if not any(
            marker in compact_goal for marker in _DISCOVERY_EXISTENCE_QUERY_MARKERS
        ):
            return False
        return any(
            observation.tool_name == "search-public-job-pages"
            and observation.status == "succeeded"
            and skill_observation_is_semantically_valid(
                observation.tool_name, observation.output
            )
            for observation in execution.observations
        )

    @staticmethod
    def _official_negative_discovery_summary(
        task: AgentTaskRequest, execution: ExecutorResult
    ) -> str:
        output = next(
            (
                observation.output
                for observation in execution.observations
                if observation.tool_name == "search-public-job-pages"
                and observation.status == "succeeded"
                and skill_observation_is_semantically_valid(
                    observation.tool_name, observation.output
                )
            ),
            {},
        )
        recent_days = output.get("time_window_days") if isinstance(output, dict) else None
        window_text = f"最近{recent_days}天" if isinstance(recent_days, int) else "指定时间窗"
        source_name = (
            "稀土掘金"
            if isinstance(output, dict) and output.get("source_scope") == "juejin.cn"
            else "指定公开来源"
        )
        return (
            f"已完成{source_name}{window_text}官方公开搜索的完整分页核验；"
            f"未找到满足用户硬约束的招聘帖。任务原始范围：{task.goal}"
        )

    def _has_blocked_evidence(self, observations: list) -> bool:
        if self._skills is None:
            return legacy_has_blocked_evidence(observations)
        return self._skills.has_blocked_evidence(observations)

    def _step_has_blocked_evidence(
        self,
        step: PlanStep,
        observations: list,
        artifact_refs: list[dict[str, Any]],
    ) -> bool:
        if self._skills is None:
            return legacy_has_blocked_evidence(observations)
        return bool(
            self._skills.completion_evidence_diagnostics(
                step,
                observations,
                summary="contract-check",
                artifact_refs=artifact_refs,
            )["blocked"]
        )

    @staticmethod
    def _run_has_external_fetch_block(db: Session, run_id: str) -> bool:
        """Preserve an earlier source-access block across a bounded replan."""
        events = db.scalars(
            select(AgentEvent).where(AgentEvent.run_id == run_id)
        )
        for event in events:
            payload = event.payload_json or {}
            if isinstance(payload, dict) and _is_external_runtime_code(
                payload.get("error_code")
            ):
                return True
        return False

    def _completion_gate_rejected(
        self,
        step: PlanStep,
        observations: list,
        *,
        summary: str | None,
        artifact_refs: list[dict[str, Any]] | None = None,
    ) -> bool:
        if self._skills is None:
            return has_known_deliverable_attempt(observations) and not legacy_completion_evidence_gate(
                step, observations, summary=summary
            )
        return self._skills.has_completion_contract(step) and not self._skills.completion_evidence_gate(
            step,
            observations,
            summary=summary,
            artifact_refs=artifact_refs or (),
        )

    @staticmethod
    def _plans_isomorphic(plan_a: ExecutionPlan, plan_b: ExecutionPlan) -> bool:
        """True when two plans carry the same normalized step sequence.

        Steps are compared by their sorted allowed-skill sets and their
        whitespace-normalized objectives, so formatting differences never
        mask a structurally repeated plan (N4 guard).
        """
        return _normalize_plan_steps(plan_a) == _normalize_plan_steps(plan_b)

    @staticmethod
    def _plan_fingerprint(plan: ExecutionPlan) -> str:
        """Hash the semantic plan shape before it is persisted or executed."""
        payload = {
            "complexity": plan.complexity.value,
            "success_criteria": sorted(" ".join(item.split()) for item in plan.success_criteria),
            "steps": [
                {
                    "objective": " ".join(step.objective.split()),
                    "allowed_skills": sorted(step.allowed_skills),
                    "requires_verification": step.requires_verification,
                    "depends_on": sorted(step.depends_on),
                    "inputs": [item.model_dump(mode="json") for item in step.inputs],
                    "outputs": [item.model_dump(mode="json") for item in step.outputs],
                }
                for step in plan.steps
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _plan_fingerprint_json(plan_json: object) -> str:
        """Hash an already persisted plan without trusting mutable model text."""
        if not isinstance(plan_json, dict):
            return hashlib.sha256(b"invalid-plan-json").hexdigest()
        raw_steps = plan_json.get("steps")
        payload = {
            "complexity": plan_json.get("complexity"),
            "success_criteria": sorted(
                " ".join(str(item).split())
                for item in plan_json.get("success_criteria", [])
                if isinstance(item, str)
            ),
            "steps": [
                {
                    "objective": " ".join(str(step.get("objective", "")).split()),
                    "allowed_skills": sorted(step.get("allowed_skills", [])),
                    "requires_verification": bool(step.get("requires_verification", False)),
                    "depends_on": sorted(step.get("depends_on", [])),
                    "inputs": step.get("inputs", []),
                    "outputs": step.get("outputs", []),
                }
                for step in raw_steps
                if isinstance(step, dict)
            ] if isinstance(raw_steps, list) else [],
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _build_decision_trace(
        db: Session,
        run_id: str,
        *,
        initial_turn_indices: dict[AgentRole, int] | None = None,
    ):
        """Return a run-local, role-indexed callback for safe decision summaries."""
        turn_indices = dict(initial_turn_indices or {role: 0 for role in AgentRole})

        def trace(
            role: AgentRole,
            decision_json: dict[str, object],
            turn_metadata: dict[str, object] | None = None,
        ) -> None:
            turn_indices[role] += 1
            model_name = None
            input_tokens = None
            output_tokens = None
            context_manifest = None
            if turn_metadata is not None and isinstance(turn_metadata, dict):
                model_name = turn_metadata.get("model_name")
                input_tokens = turn_metadata.get("input_tokens")
                output_tokens = turn_metadata.get("output_tokens")
                context_manifest = turn_metadata.get("context_manifest")
                # Preserve bounded, non-sensitive DeepExecutor diagnostics in
                # the durable turn JSON. Provider prompts and raw tool output
                # remain excluded from the trace.
                for key in ("deep_executor", "internal_model_calls"):
                    if key in turn_metadata:
                        decision_json = {
                            **decision_json,
                            key: turn_metadata[key],
                        }
            run_repository.create_turn(
                db,
                run_id=run_id,
                role=role,
                turn_index=turn_indices[role],
                decision_json=decision_json,
                model_name=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                context_manifest=context_manifest,
            )
            # A completed model decision is a recovery checkpoint. Tool output
            # remains evidence-bound and is replay-safe if a process stops before
            # the next decision persists its outcome.
            db.commit()

        return trace

    @staticmethod
    def _checkpoint(db: Session) -> None:
        """Commit a lifecycle boundary before entering an external model/tool turn."""
        db.commit()

    @staticmethod
    def _with_observed_public_evidence(
        db: Session, task: AgentTaskRequest, run_id: str
    ) -> AgentTaskRequest:
        """Expose only traceable JD pointers to later Agent turns.

        Full page bodies remain in the database and are hydrated by
        :meth:`_tool_context` immediately before a deterministic tool runs.
        Planner/Executor therefore carry stable identifiers and URLs across
        steps instead of copying the same JD text into every model request.
        """
        candidates: list[dict[str, Any]] = []
        for artifact in run_repository.list_evidence_artifacts(db, run_id):
            visible_text = artifact.content_json.get("visible_text")
            if artifact.artifact_type == "structured_job_details":
                raw_candidates = artifact.content_json.get("candidates")
                if not isinstance(raw_candidates, list) or not any(
                    isinstance(candidate, dict)
                    and not isinstance(candidate.get("evidence_refs"), list)
                    for candidate in raw_candidates
                ):
                    # A structured artifact explicitly linked to a source
                    # page is already represented by that page. Standalone
                    # structured output remains a bounded evidence item so a
                    # target JD does not disappear behind the page budget.
                    continue
                visible_text = _structured_artifact_visible_text(
                    artifact.content_json
                )
            if not isinstance(visible_text, str) or not visible_text:
                continue
            item: dict[str, Any] = {
                "artifact_id": artifact.id,
                "source_url": artifact.source_url,
                "content_hash": artifact.content_hash,
            }
            effective_url = artifact.content_json.get("effective_url")
            if isinstance(effective_url, str):
                item["effective_url"] = effective_url
            redirect_chain = artifact.content_json.get("redirect_chain")
            if isinstance(redirect_chain, list) and redirect_chain:
                item["redirect_chain"] = redirect_chain
            http_status = artifact.content_json.get("http_status")
            if isinstance(http_status, int):
                item["http_status"] = http_status
            title = artifact.content_json.get("title")
            if isinstance(title, str):
                item["title"] = title
            quality = artifact.content_json.get("quality")
            if isinstance(quality, str):
                item["quality"] = quality
            quality_signal = artifact.content_json.get("quality_signal")
            if isinstance(quality_signal, str):
                item["quality_signal"] = quality_signal
            candidates.append(item)
        context = dict(task.context)
        context["observed_public_evidence"] = candidates
        return task.model_copy(update={"context": context})

    @staticmethod
    def _tool_context(
        *, user_id: str, run_id: str, task: AgentTaskRequest, db: Session
    ) -> ToolContext:
        """Project only verified public evidence into deterministic tool authority.

        Raw page evidence flows through the bounded ``observed_public_evidence``
        context; structured job candidates are read from the run's persisted
        ``structured_job_details`` artifacts (extract outputs) so skill tools
        (e.g. ``match-observed-jobs``) rank real per-job units instead of one
        aggregated page. Candidates are tool-side authority only: they never
        enter ``task.context``, so model prompts stay unchanged.
        """
        evidence = _full_observed_public_evidence(db, run_id)
        if not evidence:
            # Compatibility for an initial in-memory tool call before its
            # first evidence artifact has been persisted.
            evidence = task.context.get("observed_public_evidence", [])
        profile_facts = task.private_context.get("confirmed_profile_facts", {})
        blocked_domains = task.context.get("blocked_public_domains", [])
        if not isinstance(blocked_domains, list):
            blocked_domains = []
        state_domains = task.execution_state.get("blocked_public_domains", [])
        if isinstance(state_domains, list):
            blocked_domains = sorted(
                {domain for domain in [*blocked_domains, *state_domains] if isinstance(domain, str)}
            )
        search_hashes = task.context.get("public_search_query_hashes", [])
        if not isinstance(search_hashes, list):
            search_hashes = []
        state_search_hashes = task.execution_state.get("public_search_query_hashes", [])
        if isinstance(state_search_hashes, list):
            search_hashes = sorted(
                {value for value in [*search_hashes, *state_search_hashes] if isinstance(value, str)}
            )
        return ToolContext(
            user_id=user_id,
            run_id=run_id,
            metadata={
                "observed_public_evidence": evidence if isinstance(evidence, list) else [],
                "structured_job_candidates": _structured_job_candidates(db, run_id),
                "confirmed_profile_facts": profile_facts
                if isinstance(profile_facts, dict)
                else {},
                "task_goal": task.goal,
                "resolved_step_inputs": task.context.get(
                    "resolved_step_inputs", {"context": {}, "artifacts": {}}
                ),
                "blocked_public_domains": blocked_domains,
                "public_search_query_hashes": search_hashes,
            },
        )

    @staticmethod
    def _persist_observed_evidence(
        db: Session,
        run_id: str,
        step: AgentStep,
        execution: ExecutorResult,
    ) -> list[dict[str, str]]:
        """Persist only tool-derived public evidence, never model-proposed URIs."""
        artifact_refs: list[dict[str, str]] = []
        for observation in execution.observations:
            output = observation.output or {}
            raw_pages = output.get("pages")
            pages = raw_pages if isinstance(raw_pages, list) else [output]
            for page in pages:
                if not isinstance(page, dict):
                    continue
                source_url = page.get("source_url")
                content_hash = page.get("content_hash")
                visible_text = page.get("visible_text")
                if not all(
                    isinstance(value, str)
                    for value in (source_url, content_hash, visible_text)
                ):
                    continue
                artifact = run_repository.create_evidence_artifact(
                    db,
                    run_id=run_id,
                    step_id=step.id,
                    source_url=source_url,
                    content_hash=content_hash,
                    content_json={
                        "title": page.get("title"),
                        "visible_text": visible_text[:_STRUCTURED_FULL_TEXT_CHARS],
                        "effective_url": page.get("effective_url"),
                        "redirect_chain": page.get("redirect_chain", []),
                        "http_status": page.get("http_status"),
                        "quality": page.get("quality"),
                        "quality_signal": page.get("quality_signal"),
                    },
                )
                artifact_ref = {
                    "artifact_id": artifact.id,
                    "artifact_type": "public_job_page",
                    "tool": observation.tool_name,
                    "source_url": source_url,
                    "content_hash": content_hash,
                }
                if page.get("quality") is not None:
                    artifact_ref["quality"] = page["quality"]
                artifact_refs.append(artifact_ref)
                run_repository.append_event(
                    db,
                    run_id=run_id,
                    event_type="executor_tool_observation",
                    payload_json={
                        "sequence": step.sequence,
                        "tool": observation.tool_name,
                        "artifact_id": artifact.id,
                        "source_url": source_url,
                        "content_hash": content_hash,
                    },
                )
        for observation in execution.observations:
            output = observation.output or {}
            source_url = output.get("source_url")
            content_hash = output.get("content_hash")
            # Sheet queries emit ``records``; page searches emit ``results``.
            # Both are candidate/URL lists carrying the evidence binding, so
            # both persist as job_search_results artifacts (C005).
            raw = output.get("results")
            if raw is None:
                raw = output.get("records")
            if not (
                isinstance(source_url, str)
                and isinstance(content_hash, str)
                and isinstance(raw, list)
            ):
                continue
            search_content: dict[str, Any] = {
                "query": output.get("query"),
                "results": raw,
            }
            if output.get("provider") == "juejin_official_search":
                search_content.update(
                    {
                        "terminal_reason": output.get("terminal_reason"),
                        "provider": output.get("provider"),
                        "source_scope": output.get("source_scope"),
                        "time_window_days": output.get("time_window_days"),
                        "coverage_complete": output.get("coverage_complete"),
                        "scanned_result_count": output.get("scanned_result_count"),
                        "matched_result_count": output.get("matched_result_count"),
                        "scan_queries": output.get("scan_queries", []),
                        "scan_evidence": output.get("scan_evidence", []),
                    }
                )
            artifact = run_repository.create_artifact(
                db,
                run_id=run_id,
                step_id=step.id,
                artifact_type="job_search_results",
                source_url=source_url,
                content_hash=content_hash,
                content_json=search_content,
            )
            completion_valid = (
                observation.tool_name == "search-public-job-pages"
                and skill_observation_is_semantically_valid(
                    observation.tool_name, output
                )
            )
            artifact_refs.append(
                {
                    "artifact_id": artifact.id,
                    "artifact_type": "job_search_results",
                    "tool": observation.tool_name,
                    "source_url": source_url,
                    "content_hash": content_hash,
                    "semantic_valid": (
                        "true"
                        if _job_search_results_are_routable(raw) or completion_valid
                        else "false"
                    ),
                    "completion_valid": "true" if completion_valid else "false",
                    "result_count": str(len(raw)),
                }
            )
            run_repository.append_event(
                db,
                run_id=run_id,
                event_type="executor_search_artifact",
                payload_json={
                    "sequence": step.sequence,
                    "tool": observation.tool_name,
                    "artifact_id": artifact.id,
                    "source_url": source_url,
                    "content_hash": content_hash,
                },
            )
        for observation in execution.observations:
            output = observation.output or {}
            raw_details = output.get("details")
            details = raw_details if isinstance(raw_details, list) else [output]
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                source_url = detail.get("source_url")
                content_hash = detail.get("content_hash")
                candidates = detail.get("candidates")
                if not (
                    isinstance(source_url, str)
                    and isinstance(content_hash, str)
                    and isinstance(candidates, list)
                ):
                    continue
                artifact = run_repository.create_artifact(
                    db,
                    run_id=run_id,
                    step_id=step.id,
                    artifact_type="structured_job_details",
                    source_url=source_url,
                    content_hash=content_hash,
                    content_json={"candidates": candidates},
                )
                detail_ref = {
                        "artifact_id": artifact.id,
                        "artifact_type": "structured_job_details",
                        "tool": observation.tool_name,
                        "source_url": source_url,
                        "content_hash": content_hash,
                }
                if detail.get("source_quality") is not None:
                    detail_ref["source_quality"] = detail["source_quality"]
                artifact_refs.append(detail_ref)
                run_repository.append_event(
                    db,
                    run_id=run_id,
                    event_type="executor_structured_artifact",
                    payload_json={
                        "sequence": step.sequence,
                        "tool": observation.tool_name,
                        "artifact_id": artifact.id,
                        "source_url": source_url,
                        "content_hash": content_hash,
                    },
                )
        skill_artifact_types = {
            "match-observed-jobs": "job_matching_report",
            "build-resume-tailoring-brief": "resume_tailoring_brief",
            "build-preparation-plan": "career_preparation_plan",
        }
        for observation in execution.observations:
            artifact_type = skill_artifact_types.get(observation.tool_name)
            output = observation.output or {}
            source_url = _skill_artifact_source_url(artifact_type, output)
            if artifact_type is None or source_url is None:
                continue
            content_json = dict(output)
            content_hash = hashlib.sha256(
                json.dumps(
                    content_json,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            artifact = run_repository.create_artifact(
                db,
                run_id=run_id,
                step_id=step.id,
                artifact_type=artifact_type,
                source_url=source_url,
                content_hash=content_hash,
                content_json=content_json,
            )
            artifact_refs.append(
                {
                    "artifact_id": artifact.id,
                    "artifact_type": artifact_type,
                    "tool": observation.tool_name,
                    "source_url": source_url,
                    "content_hash": content_hash,
                    "semantic_valid": (
                        "true"
                        if skill_observation_is_semantically_valid(
                            observation.tool_name, output
                        )
                        else "false"
                    ),
                }
            )
            run_repository.append_event(
                db,
                run_id=run_id,
                event_type="executor_skill_artifact",
                payload_json={
                    "sequence": step.sequence,
                    "tool": observation.tool_name,
                    "artifact_id": artifact.id,
                    "artifact_type": artifact_type,
                },
            )
        return artifact_refs

    @staticmethod
    def _persist_seeded_public_evidence(
        *,
        db: Session,
        run_id: str,
        step: AgentStep,
        task: AgentTaskRequest,
    ) -> None:
        """Hydrate trusted chain evidence into the current run's ledger.

        Chained evaluations pass the previous run's immutable artifact
        projection as ``observed_public_evidence``. Persisting that projection
        before the first step keeps the current run's audit and downstream
        tools consistent with the evidence already shown to the Executor.
        Entries without a source URL, hash, or visible text are ignored.
        """
        raw_evidence = task.context.get("observed_public_evidence")
        if not isinstance(raw_evidence, list):
            return
        seeded_count = 0
        for item in raw_evidence:
            if not isinstance(item, dict) or item.get("artifact_type") not in {
                None,
                "public_job_page",
            }:
                continue
            source_url = item.get("source_url")
            content_hash = item.get("content_hash")
            visible_text = item.get("visible_text")
            if not all(
                isinstance(value, str) and value
                for value in (source_url, content_hash, visible_text)
            ):
                continue
            content_json = {
                key: item[key]
                for key in (
                    "title",
                    "visible_text",
                    "effective_url",
                    "redirect_chain",
                    "http_status",
                    "quality",
                    "quality_signal",
                )
                if key in item
            }
            run_repository.create_evidence_artifact(
                db,
                run_id=run_id,
                step_id=step.id,
                source_url=source_url,
                content_hash=content_hash,
                content_json=content_json,
            )
            seeded_count += 1
        if seeded_count:
            run_repository.append_event(
                db,
                run_id=run_id,
                event_type="inherited_public_evidence_hydrated",
                payload_json={"sequence": step.sequence, "artifact_count": seeded_count},
            )

    @staticmethod
    def _step_seeded_artifact_refs(
        *, db: Session, run_id: str, step: AgentStep
    ) -> list[dict[str, str]]:
        """Project chain-hydrated page artifacts into the current step refs."""
        refs: list[dict[str, str]] = []
        for artifact in run_repository.list_evidence_artifacts(db, run_id):
            if artifact.step_id != step.id or artifact.artifact_type != "public_job_page":
                continue
            quality = artifact.content_json.get("quality")
            ref: dict[str, str] = {
                "artifact_id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "tool": "fetch-public-job-pages",
                "source_url": artifact.source_url,
                "content_hash": artifact.content_hash,
            }
            if isinstance(quality, str):
                ref["quality"] = quality
            refs.append(ref)
        return refs

    @staticmethod
    def _resolved_step_artifact_refs(
        context: ToolContext,
    ) -> list[dict[str, str]]:
        """Project trusted upstream step outputs into the current step refs.

        ``_prepare_step_inputs`` resolves artifact ports from the current run's
        previous steps and places that bounded projection in ToolContext. The
        Executor must see those refs as usable evidence even when it chooses no
        new fetch tool; otherwise a valid upstream JD is invisible to the
        current step's deterministic completion gate.
        """
        resolved = context.metadata.get("resolved_step_inputs", {})
        if not isinstance(resolved, dict):
            return []
        artifacts = resolved.get("artifacts", {})
        if not isinstance(artifacts, dict):
            return []
        refs: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for source in artifacts.values():
            if not isinstance(source, list):
                continue
            for item in source:
                if not isinstance(item, dict):
                    continue
                required = ("artifact_id", "artifact_type", "source_url", "content_hash")
                if not all(isinstance(item.get(key), str) and item.get(key) for key in required):
                    continue
                key = (item["artifact_id"], item["content_hash"])
                if key in seen:
                    continue
                seen.add(key)
                ref = {
                    key: item[key]
                    for key in ("artifact_id", "artifact_type", "source_url", "content_hash")
                }
                for optional in (
                    "tool",
                    "quality",
                    "source_quality",
                    "output_name",
                    "semantic_valid",
                    "completion_valid",
                ):
                    value = item.get(optional)
                    if isinstance(value, str) and value:
                        ref[optional] = value
                refs.append(ref)
        return refs

    def _auto_extract_jd_details(
        self,
        *,
        db: Session,
        run_id: str,
        task: AgentTaskRequest,
        plan_step: PlanStep,
        persisted_step: AgentStep,
        context: ToolContext,
        artifact_refs: list[dict[str, Any]],
        tool_budget: ToolCallBudget,
    ) -> tuple[list[Any], list[dict[str, str]]]:
        """Normalize captured JD pages before a model stall becomes a handoff."""
        if "job-discovery" not in plan_step.allowed_skills:
            return [], []
        if any(ref.get("runtime_auto_extract") == "true" for ref in artifact_refs):
            return [], []
        page_ids = [
            str(ref["artifact_id"])
            for ref in artifact_refs
            if ref.get("artifact_type") == "public_job_page"
            and ref.get("quality") == "jd_complete"
            and isinstance(ref.get("artifact_id"), str)
        ]
        if not page_ids:
            return [], []
        page_ids = list(dict.fromkeys(page_ids))
        tool_context = self._tool_context(
            user_id=context.user_id,
            run_id=run_id,
            task=task,
            db=db,
        )
        observations: list[Any] = []
        refs: list[dict[str, str]] = []
        for offset in range(0, len(page_ids), 10):
            if not tool_budget.try_consume():
                break
            observation = self._executor.invoke_registered_tool(
                name="extract-observed-job-details-batch",
                context=tool_context,
                payload={"artifact_ids": page_ids[offset : offset + 10]},
            )
            observations.append(observation)
            if observation.status != "succeeded":
                continue
            auto_execution = ExecutorResult(
                status="succeeded",
                observations=[observation],
                summary="已对已抓取的公开 JD 页面执行确定性结构化提取。",
            )
            batch_refs = self._persist_observed_evidence(
                db, run_id, persisted_step, auto_execution
            )
            for ref in batch_refs:
                ref["runtime_auto_extract"] = "true"
            refs.extend(batch_refs)
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="runtime_auto_extracted_jd_details",
            payload_json={
                "step_id": plan_step.step_id,
                "tool": "extract-observed-job-details-batch",
                "page_count": len(page_ids),
                "artifact_count": len(refs),
            },
        )
        return observations, refs

    def _auto_recover_discovery_evidence(
        self,
        *,
        db: Session,
        run_id: str,
        task: AgentTaskRequest,
        plan_step: PlanStep,
        persisted_step: AgentStep,
        context: ToolContext,
        observations: list[Any],
        artifact_refs: list[dict[str, Any]],
        tool_budget: ToolCallBudget,
    ) -> tuple[list[Any], list[dict[str, str]]]:
        """Continue a source-bound discovery route after a model stall.

        Two deterministic recovery cases are safe and common: a list-only
        page needs the existing bounded detail expansion, and a successful
        sheet/search result already contains public URLs that still need
        fetching. Both paths use the registered public fetch tool, preserve
        per-URL failures, and never invent or retry around access controls.
        """
        if "job-discovery" not in plan_step.allowed_skills:
            return [], []
        if any(
            ref.get("artifact_type") == "job_search_results"
            and ref.get("completion_valid") == "true"
            for ref in artifact_refs
        ):
            # A complete official source scan with zero matches is already a
            # final discovery proof. Re-running search cannot create a fetch
            # candidate and only adds route/hash noise before the terminal
            # negative rescue evaluates the original observation.
            return [], []

        # An explicitly named source or exact requested-role evidence page is
        # a hard constraint. Fetch reviewed priority routes before accepting a
        # complete but unrelated page from supplied URLs/search results.
        # Downstream normalization still enforces provenance and captured page
        # status; this never bypasses a captcha or implies an archived JD is open.
        processed_urls = {
            ref.get("source_url")
            for ref in artifact_refs
            if isinstance(ref.get("source_url"), str)
        }
        priority_source_urls = [
            url
            for url in [
                *_public_source_mirror_seed_urls(task.goal),
                *_requested_role_seed_urls(task.goal),
            ]
            if url not in processed_urls
        ]
        priority_observations, priority_refs = self._auto_fetch_public_urls(
            db=db,
            run_id=run_id,
            persisted_step=persisted_step,
            context=context,
            urls=priority_source_urls,
            tool_budget=tool_budget,
            event_type="runtime_auto_fetched_priority_source_mirror",
            event_payload={
                "step_id": plan_step.step_id,
                "url_count": len(priority_source_urls),
            },
        )
        if any(ref.get("quality") == "jd_complete" for ref in priority_refs):
            return priority_observations, priority_refs
        if self._contains_access_block(priority_observations):
            return priority_observations, priority_refs
        if priority_observations or priority_refs:
            observations = [*observations, *priority_observations]
            artifact_refs = [*artifact_refs, *priority_refs]

        routing_observations = [
            *observations,
            *_persisted_job_search_observations(db, run_id, artifact_refs),
        ]
        derived_search_hints = _discovery_search_hints(
            task.goal, routing_observations
        )
        existing_search_hints = context.metadata.get("discovery_search_hints", [])
        if not isinstance(existing_search_hints, list):
            existing_search_hints = []
        search_context = ToolContext(
            user_id=context.user_id,
            run_id=context.run_id,
            metadata={
                **context.metadata,
                "discovery_search_hints": list(
                    dict.fromkeys(
                        hint
                        for hint in [*existing_search_hints, *derived_search_hints]
                        if isinstance(hint, str) and hint.strip()
                    )
                )[:5],
            },
        )

        list_urls = list(
            dict.fromkeys(
                str(ref["source_url"])
                for ref in artifact_refs
                if ref.get("artifact_type") == "public_job_page"
                and ref.get("quality") == "list_only"
                and isinstance(ref.get("source_url"), str)
                and str(ref["source_url"]).startswith(("http://", "https://"))
                and not ref.get("runtime_auto_expand")
            )
        )
        if list_urls:
            expanded_observations, expanded_refs = self._auto_fetch_public_urls(
                db=db,
                run_id=run_id,
                persisted_step=persisted_step,
                context=search_context,
                urls=list_urls[:3],
                tool_budget=tool_budget,
                event_type="runtime_auto_expanded_list_pages",
                event_payload={"step_id": plan_step.step_id, "url_count": len(list_urls[:3])},
            )
            if any(ref.get("quality") == "jd_complete" for ref in expanded_refs):
                return expanded_observations, expanded_refs
            if self._contains_access_block(expanded_observations):
                return expanded_observations, expanded_refs
            supplied_urls = task.context.get("candidate_urls")
            processed_urls = {
                ref.get("source_url")
                for ref in artifact_refs
                if ref.get("artifact_type") == "public_job_page"
                and isinstance(ref.get("source_url"), str)
            }
            if (
                isinstance(supplied_urls, list)
                and supplied_urls
                and not {
                    url for url in supplied_urls if isinstance(url, str)
                }.issubset(processed_urls)
            ):
                # Preserve the Executor's source-boundary rule: public search
                # is a fallback only after every supplied candidate has been
                # processed or failed.
                return expanded_observations, expanded_refs
            processed_seed_urls = {
                ref.get("source_url")
                for ref in [*artifact_refs, *expanded_refs]
                if isinstance(ref.get("source_url"), str)
            }
            official_seed_urls = [
                url
                for url in _trusted_discovery_seed_urls(
                    task.goal, routing_observations
                )
                if url not in processed_seed_urls
            ]
            seeded_observations, seeded_refs = self._auto_fetch_public_urls(
                db=db,
                run_id=run_id,
                persisted_step=persisted_step,
                context=search_context,
                urls=official_seed_urls,
                tool_budget=tool_budget,
                event_type="runtime_auto_fetched_trusted_discovery_seeds",
                event_payload={
                    "step_id": plan_step.step_id,
                    "url_count": len(official_seed_urls),
                },
            )
            seeded_complete = any(
                isinstance(observation.output, dict)
                and any(
                    isinstance(page, dict) and page.get("quality") == "jd_complete"
                    for page in (
                        observation.output.get("pages")
                        if isinstance(observation.output.get("pages"), list)
                        else [observation.output]
                    )
                )
                for observation in seeded_observations
            )
            if seeded_complete or self._contains_access_block(seeded_observations):
                return (
                    [*expanded_observations, *seeded_observations],
                    [*expanded_refs, *seeded_refs],
                )
            search_observations, search_refs = self._auto_search_and_fetch(
                db=db,
                run_id=run_id,
                persisted_step=persisted_step,
                context=search_context,
                tool_budget=tool_budget,
                task_goal=context.metadata.get("task_goal"),
                step_id=plan_step.step_id,
            )
            return (
                [
                    *expanded_observations,
                    *seeded_observations,
                    *search_observations,
                ],
                [*expanded_refs, *seeded_refs, *search_refs],
            )

        has_complete_page = any(
            isinstance(observation.output, dict)
            and any(
                isinstance(page, dict) and page.get("quality") == "jd_complete"
                for page in (
                    observation.output.get("pages")
                    if isinstance(observation.output.get("pages"), list)
                    else [observation.output]
                )
            )
            for observation in observations
        )
        if has_complete_page:
            return [], []
        urls: list[str] = []
        for observation in routing_observations:
            output = observation.output if isinstance(observation.output, dict) else {}
            for collection_name in ("results", "records"):
                collection = output.get(collection_name)
                if not isinstance(collection, list):
                    continue
                for item in collection:
                    if not isinstance(item, dict):
                        continue
                    for value in _public_urls_from_search_item(item):
                        if value not in urls:
                            urls.append(value)
                    if len(urls) >= 10:
                        break
                if len(urls) >= 10:
                    break
            if len(urls) >= 10:
                break
        if not urls:
            supplied_urls = task.context.get("candidate_urls")
            if isinstance(supplied_urls, list):
                processed_urls = {
                    ref.get("source_url")
                    for ref in artifact_refs
                    if isinstance(ref.get("source_url"), str)
                }
                urls = list(
                    dict.fromkeys(
                        value
                        for value in supplied_urls
                        if isinstance(value, str)
                        and value.startswith(("http://", "https://"))
                        and value not in processed_urls
                    )
                )[:10]
        if not urls:
            processed_urls = {
                ref.get("source_url")
                for ref in artifact_refs
                if isinstance(ref.get("source_url"), str)
            }
            official_seed_urls = [
                url
                for url in _trusted_discovery_seed_urls(
                    task.goal, routing_observations
                )
                if url not in processed_urls
            ]
            seeded_observations, seeded_refs = self._auto_fetch_public_urls(
                db=db,
                run_id=run_id,
                persisted_step=persisted_step,
                context=search_context,
                urls=official_seed_urls,
                tool_budget=tool_budget,
                event_type="runtime_auto_fetched_trusted_discovery_seeds",
                event_payload={
                    "step_id": plan_step.step_id,
                    "url_count": len(official_seed_urls),
                },
            )
            seeded_complete = any(
                isinstance(observation.output, dict)
                and any(
                    isinstance(page, dict) and page.get("quality") == "jd_complete"
                    for page in (
                        observation.output.get("pages")
                        if isinstance(observation.output.get("pages"), list)
                        else [observation.output]
                    )
                )
                for observation in seeded_observations
            )
            if seeded_complete or self._contains_access_block(seeded_observations):
                return seeded_observations, seeded_refs
            search_observations, search_refs = self._auto_search_and_fetch(
                db=db,
                run_id=run_id,
                persisted_step=persisted_step,
                context=search_context,
                tool_budget=tool_budget,
                task_goal=context.metadata.get("task_goal"),
                step_id=plan_step.step_id,
            )
            return (
                [*seeded_observations, *search_observations],
                [*seeded_refs, *search_refs],
            )
        fetched_observations, fetched_refs = self._auto_fetch_public_urls(
            db=db,
            run_id=run_id,
            persisted_step=persisted_step,
            context=context,
            urls=urls,
            tool_budget=tool_budget,
            event_type="runtime_auto_fetched_search_results",
            event_payload={"step_id": plan_step.step_id, "url_count": len(urls)},
        )
        # A sheet/search result is only a routing artifact. If every direct
        # link it supplied ended in an empty/blocked page, spend the single
        # bounded public-search fallback on a different public route. This is
        # still source-bound and safe: no blocked URL is retried and only URLs
        # returned by the search adapter may be fetched next.
        has_complete_page = any(
            isinstance(observation.output, dict)
            and any(
                isinstance(page, dict) and page.get("quality") == "jd_complete"
                for page in (
                    observation.output.get("pages")
                    if isinstance(observation.output.get("pages"), list)
                    else [observation.output]
                )
            )
            for observation in fetched_observations
        )
        if not has_complete_page:
            processed_urls = {
                ref.get("source_url")
                for ref in [*artifact_refs, *fetched_refs]
                if isinstance(ref.get("source_url"), str)
            }
            official_seed_urls = [
                url
                for url in _trusted_discovery_seed_urls(
                    task.goal, routing_observations
                )
                if url not in processed_urls
            ]
            seeded_observations, seeded_refs = self._auto_fetch_public_urls(
                db=db,
                run_id=run_id,
                persisted_step=persisted_step,
                context=search_context,
                urls=official_seed_urls,
                tool_budget=tool_budget,
                event_type="runtime_auto_fetched_trusted_discovery_seeds",
                event_payload={
                    "step_id": plan_step.step_id,
                    "url_count": len(official_seed_urls),
                },
            )
            seeded_complete = any(
                isinstance(observation.output, dict)
                and any(
                    isinstance(page, dict) and page.get("quality") == "jd_complete"
                    for page in (
                        observation.output.get("pages")
                        if isinstance(observation.output.get("pages"), list)
                        else [observation.output]
                    )
                )
                for observation in seeded_observations
            )
            if seeded_complete or self._contains_access_block(seeded_observations):
                return (
                    [*fetched_observations, *seeded_observations],
                    [*fetched_refs, *seeded_refs],
                )
            search_observations, search_refs = self._auto_search_and_fetch(
                db=db,
                run_id=run_id,
                persisted_step=persisted_step,
                context=search_context,
                tool_budget=tool_budget,
                task_goal=context.metadata.get("task_goal"),
                step_id=plan_step.step_id,
            )
            return (
                [
                    *fetched_observations,
                    *seeded_observations,
                    *search_observations,
                ],
                [*fetched_refs, *seeded_refs, *search_refs],
            )
        return fetched_observations, fetched_refs

    def _auto_search_and_fetch(
        self,
        *,
        db: Session,
        run_id: str,
        persisted_step: AgentStep,
        context: ToolContext,
        tool_budget: ToolCallBudget,
        task_goal: object,
        step_id: str,
    ) -> tuple[list[Any], list[dict[str, str]]]:
        """Use the already-authorized public-search fallback once per stall."""
        if not self._executor.has_registered_tool("search-public-job-pages"):
            return [], []
        if not isinstance(task_goal, str) or len(task_goal.strip()) < 2:
            return [], []
        attempted_hashes = context.metadata.get("public_search_query_hashes", [])
        attempted_hashes = {
            value for value in attempted_hashes if isinstance(value, str)
        } if isinstance(attempted_hashes, list) else set()
        # A model may have already spent the exact goal query before the
        # deterministic recovery runs. Pick the first bounded query variant
        # whose route hash has not been used; this changes only the public
        # search wording, never the source authorization or URL safety rules.
        raw_hints = context.metadata.get("discovery_search_hints", [])
        hints = (
            [value.strip() for value in raw_hints if isinstance(value, str) and value.strip()]
            if isinstance(raw_hints, list)
            else []
        )
        query_candidates = tuple(
            dict.fromkeys(
                [
                    *hints,
                    *_discovery_search_hints(task_goal, []),
                    task_goal.strip(),
                    f"{task_goal.strip()} 岗位详情",
                    f"{task_goal.strip()} 官方招聘",
                ]
            )
        )
        queries = [
            candidate[:380]
            for candidate in query_candidates
            if hashlib.sha256(candidate[:380].encode("utf-8")).hexdigest()
            not in attempted_hashes
        ][: max(0, 3 - len(attempted_hashes))]
        search_observations: list[Any] = []
        search_refs: list[dict[str, str]] = []
        for query in queries:
            if not tool_budget.try_consume():
                break
            query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
            attempted_hashes.add(query_hash)
            search_observation = self._executor.invoke_registered_tool(
                name="search-public-job-pages",
                context=ToolContext(
                    user_id=context.user_id,
                    run_id=run_id,
                    metadata={
                        **context.metadata,
                        "runtime_auto_search": True,
                    },
                ),
                payload={"query": query, "max_results": 5},
            )
            search_observations.append(search_observation)
            search_execution = ExecutorResult(
                status="succeeded",
                observations=[search_observation],
                summary="已使用公开搜索回退核验岗位来源。",
            )
            search_refs.extend(
                self._persist_observed_evidence(
                    db, run_id, persisted_step, search_execution
                )
            )
            if search_observation.status != "succeeded":
                break
            urls = self._search_result_urls([search_observation])
            if not urls:
                continue
            fetched_observations, fetched_refs = self._auto_fetch_public_urls(
                db=db,
                run_id=run_id,
                persisted_step=persisted_step,
                context=context,
                urls=urls,
                tool_budget=tool_budget,
                event_type="runtime_auto_fetched_public_search_results",
                event_payload={"step_id": step_id, "url_count": len(urls)},
            )
            return (
                [*search_observations, *fetched_observations],
                [*search_refs, *fetched_refs],
            )
        return search_observations, search_refs

    @staticmethod
    def _search_result_urls(observations: list[Any]) -> list[str]:
        urls: list[str] = []
        for observation in observations:
            output = observation.output if isinstance(observation.output, dict) else {}
            for collection_name in ("results", "records"):
                collection = output.get(collection_name)
                if not isinstance(collection, list):
                    continue
                for item in collection:
                    if not isinstance(item, dict):
                        continue
                    for value in _public_urls_from_search_item(item):
                        if value not in urls:
                            urls.append(value)
                    if len(urls) >= 10:
                        return urls
        return urls

    @staticmethod
    def _contains_access_block(observations: list[Any]) -> bool:
        blocked_codes = {
            "anti_bot",
            "anti_bot_challenge",
            "captcha",
            "login_required",
            "access_denied",
            "domain_temporarily_blocked",
        }
        for observation in observations:
            if getattr(observation, "error_code", None) in blocked_codes:
                return True
            output = observation.output if isinstance(observation.output, dict) else {}
            failures = output.get("failures")
            if isinstance(failures, list) and any(
                isinstance(item, dict) and item.get("error_code") in blocked_codes
                for item in failures
            ):
                return True
        return False

    def _auto_fetch_public_urls(
        self,
        *,
        db: Session,
        run_id: str,
        persisted_step: AgentStep,
        context: ToolContext,
        urls: list[str],
        tool_budget: ToolCallBudget,
        event_type: str,
        event_payload: dict[str, Any],
    ) -> tuple[list[Any], list[dict[str, str]]]:
        if not urls or not tool_budget.try_consume():
            return [], []
        fetch_context = ToolContext(
            user_id=context.user_id,
            run_id=run_id,
            metadata=dict(context.metadata),
        )
        observation = self._executor.invoke_registered_tool(
            name="fetch-public-job-pages",
            context=fetch_context,
            payload={"urls": urls[:10]},
        )
        if observation.status != "succeeded":
            return [observation], []
        execution = ExecutorResult(
            status="succeeded",
            observations=[observation],
            summary="已对工具返回的公开岗位链接执行确定性页面核验。",
        )
        refs = self._persist_observed_evidence(
            db, run_id, persisted_step, execution
        )
        for ref in refs:
            ref["runtime_auto_expand"] = "true"
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type=event_type,
            payload_json={**event_payload, "artifact_count": len(refs)},
        )
        return [observation], refs

    def _auto_build_role_deliverable(
        self,
        *,
        db: Session,
        run_id: str,
        task: AgentTaskRequest,
        plan_step: PlanStep,
        persisted_step: AgentStep,
        context: ToolContext,
        artifact_refs: list[dict[str, Any]],
        tool_budget: ToolCallBudget,
    ) -> tuple[list[Any], list[dict[str, str]]]:
        """Finish a role-specific artifact from trusted candidates after a model stall."""
        if "job-matching" in plan_step.allowed_skills:
            artifact_type = "job_matching_report"
            tool_name = "match-observed-jobs"
            keywords = self._goal_role_keywords(task.goal)
        elif "resume-tailoring" in plan_step.allowed_skills:
            artifact_type = "resume_tailoring_brief"
            tool_name = "build-resume-tailoring-brief"
            keywords = self._goal_role_keywords(task.goal)
        elif "career-planning" in plan_step.allowed_skills:
            artifact_type = "career_preparation_plan"
            tool_name = "build-preparation-plan"
            keywords = self._goal_role_keywords(task.goal)
        else:
            return [], []
        if any(ref.get("artifact_type") == artifact_type for ref in artifact_refs):
            return [], []
        candidates = _structured_job_candidates(db, run_id)
        usable_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("source_quality") not in {"list_only", "js_shell", "empty"}
        ]
        if tool_name == "build-resume-tailoring-brief" and not usable_candidates:
            # A chained link can inherit complete public pages without their
            # prior run's structured-candidate rows. If matching legitimately
            # selected one of those raw pages, rehydrate that exact persisted
            # page as the deterministic tailoring candidate instead of asking
            # the model to invent a cross-run candidate id.
            raw_report_target_id = self._matching_report_target_artifact_id(
                db=db, run_id=run_id
            )
            if raw_report_target_id:
                for artifact in run_repository.list_evidence_artifacts(db, run_id):
                    if (
                        artifact.id != raw_report_target_id
                        or artifact.artifact_type != "public_job_page"
                        or artifact.content_json.get("quality") != "jd_complete"
                    ):
                        continue
                    visible_text = artifact.content_json.get("visible_text")
                    if not isinstance(visible_text, str) or not visible_text.strip():
                        continue
                    raw_title = artifact.content_json.get("title")
                    usable_candidates = [
                        {
                            "artifact_id": artifact.id,
                            "candidate_id": None,
                            "source_artifact_id": artifact.id,
                            "source_url": artifact.source_url,
                            "page_source_url": artifact.source_url,
                            "source_quality": "jd_complete",
                            "title": raw_title if isinstance(raw_title, str) else None,
                            "responsibilities": visible_text,
                            "requirements": "",
                            "full_text": visible_text,
                            "locations": [],
                            "recruitment_types": [],
                            "skills": [],
                        }
                    ]
                    break
        if tool_name != "match-observed-jobs":
            # Tailoring/planning must use the same deterministic role, city,
            # experience and graduate-scope filter as matching. Otherwise the
            # first body-backed JD can silently become a wrong-target brief.
            from backend.app.services.career_skills.job_matching import (
                _candidate_meets_goal_constraints,
                _source_allowed_for_goal,
            )

            constrained_candidates = [
                candidate
                for candidate in usable_candidates
                if _candidate_meets_goal_constraints(
                    candidate,
                    task.goal,
                    task.private_context.get("confirmed_profile_facts"),
                )
                and _source_allowed_for_goal(
                    candidate.get("page_source_url") or candidate.get("source_url"),
                    task.goal,
                    evidence_text="\n".join(
                        str(candidate.get(key) or "")
                        for key in (
                            "title",
                            "responsibilities",
                            "requirements",
                            "full_text",
                            "page_text_prefix",
                        )
                    ),
                )
            ]
            usable_candidates = constrained_candidates
        if tool_name == "match-observed-jobs":
            # Matching consumes the full trusted candidate projection. Do not
            # pre-filter it by title: the deterministic matcher owns role,
            # location, and experience constraints and must explain exclusions.
            if not usable_candidates:
                # A chained matching step may inherit only public page refs
                # (the prior step did not persist structured extraction). The
                # matching tool has a bounded raw-page fallback, so one
                # complete page is enough to invoke it without fabricating a
                # structured candidate.
                usable_candidates = [
                    {"artifact_id": artifact.id}
                    for artifact in run_repository.list_evidence_artifacts(db, run_id)
                    if artifact.artifact_type == "public_job_page"
                    and artifact.content_json.get("quality") == "jd_complete"
                ]
            if not usable_candidates or not tool_budget.try_consume():
                return [], []
            selected = usable_candidates[0]
            target_keywords = keywords or ["岗位"]
        else:
            selected = None
        ranked: list[tuple[int, dict[str, Any]]] = []
        for candidate in usable_candidates:
            if tool_name == "match-observed-jobs":
                break
            searchable = "\n".join(
                str(candidate.get(key) or "")
                for key in ("title", "company_name", "responsibilities", "requirements")
            ).lower()
            score = sum(1 for keyword in keywords if keyword.lower() in searchable)
            if score:
                ranked.append((score, candidate))
        if tool_name == "build-resume-tailoring-brief" and not ranked:
            # A chain step may describe only "the selected job" and therefore
            # contain no role keyword. Prefer a real, body-backed JD over a
            # recommendation card so the tailoring tool receives resolvable
            # target evidence even before the matching-report projection is
            # available.
            ranked = [
                (1, candidate)
                for candidate in usable_candidates
                if (
                    isinstance(candidate.get("title"), str)
                    and (
                        isinstance(candidate.get("responsibilities"), str)
                        and candidate.get("responsibilities").strip()
                        or isinstance(candidate.get("requirements"), str)
                        and candidate.get("requirements").strip()
                    )
                )
            ]
        if tool_name != "match-observed-jobs" and (
            not ranked or not tool_budget.try_consume()
        ):
            return [], []
        tailoring_target_id: str | None = None
        if tool_name != "match-observed-jobs":
            ranked.sort(
                key=lambda item: (
                    -item[0],
                    str(item[1].get("title") or ""),
                    str(item[1].get("artifact_id") or ""),
                )
            )
            selected = ranked[0][1]
            artifact_id = selected.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                return [], []
            target_keywords = [
                keyword
                for keyword in keywords
                if keyword.lower()
                in "\n".join(
                    str(selected.get(key) or "")
                    for key in ("title", "responsibilities", "requirements")
                ).lower()
            ] or ["岗位"]
            if tool_name == "build-resume-tailoring-brief":
                report_target_id = self._matching_report_target_artifact_id(
                    db=db, run_id=run_id
                )
                if report_target_id:
                    tailoring_target_id = report_target_id
                    report_candidate = next(
                        (
                            candidate
                            for candidate in usable_candidates
                            if report_target_id
                            in {
                                candidate.get("candidate_id"),
                                candidate.get("artifact_id"),
                                candidate.get("source_artifact_id"),
                            }
                        ),
                        None,
                    )
                    if report_candidate is not None:
                        selected = report_candidate
                        artifact_id = report_target_id
                        target_keywords = self._tailoring_keywords(
                            task, report_candidate
                        )
                    else:
                        report_facts = task.private_context.get(
                            "confirmed_profile_facts", {}
                        )
                        if isinstance(report_facts, dict):
                            for key, value in report_facts.items():
                                if "name" in str(key).lower() and isinstance(value, str):
                                    target_keywords = self._goal_role_keywords(value)
                                    break
        if tool_name == "match-observed-jobs":
            profile_facts = task.private_context.get("confirmed_profile_facts", {})
            profile_keywords = list(keywords)
            if isinstance(profile_facts, dict):
                skills = profile_facts.get("skills")
                if isinstance(skills, list):
                    profile_keywords.extend(
                        skill for skill in skills if isinstance(skill, str)
                    )
                for key, value in profile_facts.items():
                    if "name" in str(key).lower() and isinstance(value, str):
                        profile_keywords.extend(self._goal_role_keywords(value))
            profile_keywords.extend(
                value
                for value in (
                    task.context.get("role_keywords", [])
                    if isinstance(task.context.get("role_keywords", []), list)
                    else []
                )
                if isinstance(value, str)
            )
            preferred_locations = task.context.get("location_keywords", [])
            if not isinstance(preferred_locations, list):
                preferred_locations = []
            payload = {
                "profile_keywords": list(dict.fromkeys(profile_keywords))[:30],
                "preferred_locations": [
                    value for value in preferred_locations if isinstance(value, str)
                ][:20],
                "ranking_criteria": ["skills", "location", "recency"],
                "limit": 100,
            }
        else:
            payload = {
                "target_artifact_id": tailoring_target_id
                or selected.get("candidate_id")
                or selected.get("artifact_id"),
            }
        if tool_name == "build-resume-tailoring-brief":
            payload["target_keywords"] = target_keywords
        elif tool_name == "build-preparation-plan":
            matched_keywords = [
                keyword
                for keyword in target_keywords
                if keyword.lower()
                in "\n".join(
                    str(selected.get(key) or "")
                    for key in (
                        "title",
                        "responsibilities",
                        "requirements",
                        "full_text",
                    )
                ).lower()
            ]
            if not matched_keywords:
                from backend.app.services.job_discovery.tools.skill_validator import (
                    skills_from_text,
                )

                evidence_text = "\n".join(
                    str(selected.get(key) or "")
                    for key in (
                        "title",
                        "responsibilities",
                        "requirements",
                        "full_text",
                    )
                )
                matched_keywords = skills_from_text(evidence_text)[:8]
            target_keywords = matched_keywords or target_keywords
            payload["focus_keywords"] = target_keywords
        observation = self._executor.invoke_registered_tool(
            name=tool_name,
            context=self._tool_context(
                user_id=context.user_id,
                run_id=run_id,
                task=task,
                db=db,
            ),
            payload=payload,
        )
        if observation.status != "succeeded":
            return [observation], []
        auto_execution = ExecutorResult(
            status="succeeded",
            observations=[observation],
            summary="已基于目标角色匹配的公开 JD 生成职业交付物。",
        )
        refs = self._persist_observed_evidence(db, run_id, persisted_step, auto_execution)
        for ref in refs:
            ref["runtime_auto_deliverable"] = "true"
        return [observation], refs

    @staticmethod
    def _matching_report_target_artifact_id(
        *, db: Session, run_id: str
    ) -> str | None:
        """Resolve the top match to the exact artifact expected by tailoring."""
        for artifact in run_repository.list_evidence_artifacts(db, run_id):
            if artifact.artifact_type != "job_matching_report":
                continue
            matches = artifact.content_json.get("matches")
            if not isinstance(matches, list) or not matches:
                continue
            top = matches[0]
            if not isinstance(top, dict):
                continue
            candidate_ids = [
                top.get("candidate_id"),
                top.get("artifact_id"),
                top.get("source_artifact_id"),
            ]
            source_url = top.get("source_url")
            artifacts = run_repository.list_evidence_artifacts(db, run_id)
            structured_candidate_ids = {
                candidate.get("candidate_id")
                for candidate in _structured_job_candidates(db, run_id)
                if isinstance(candidate.get("candidate_id"), str)
            }
            for candidate_id in candidate_ids:
                if not isinstance(candidate_id, str):
                    continue
                if candidate_id in structured_candidate_ids:
                    return candidate_id
                if any(artifact.id == candidate_id for artifact in artifacts):
                    return candidate_id
            if isinstance(source_url, str) and source_url:
                for artifact in artifacts:
                    if artifact.source_url == source_url and artifact.artifact_type in {
                        "public_job_page",
                        "structured_job_details",
                    }:
                        return artifact.id
        return None

    @staticmethod
    def _tailoring_keywords(
        task: AgentTaskRequest, candidate: dict[str, Any]
    ) -> list[str]:
        keywords = AgentRuntime._goal_role_keywords(task.goal)
        if keywords != ["岗位"]:
            return keywords
        text = " ".join(
            str(candidate.get(key) or "")
            for key in ("title", "responsibilities", "requirements")
        )
        inferred = [
            marker
            for marker in (
                "产品经理",
                "前端",
                "Java",
                "后端",
                "大模型",
                "AIGC",
                "AI",
                "Agent",
                "RAG",
            )
            if marker.lower() in text.lower()
        ]
        facts = task.private_context.get("confirmed_profile_facts", {})
        if isinstance(facts, dict) and isinstance(facts.get("skills"), list):
            inferred.extend(
                skill for skill in facts["skills"] if isinstance(skill, str)
            )
        return list(dict.fromkeys(inferred)) or ["岗位"]

    @staticmethod
    def _goal_role_keywords(goal: str) -> list[str]:
        lowered = goal.lower()
        if "产品经理" in lowered or "aigc" in lowered:
            return ["产品经理", "AIGC", "AI"]
        if "ai 应用开发" in lowered or "ai应用开发" in lowered:
            return ["AI", "应用开发", "Agent", "智能体"]
        if "大模型应用开发" in lowered or "llm 应用" in lowered or "llm应用" in lowered:
            return ["大模型", "应用开发", "Agent", "AI"]
        if "前端开发" in lowered:
            return ["前端", "Frontend", "Vue"]
        if "java 后端" in lowered or "java后端" in lowered:
            return ["Java", "后端"]
        return ["岗位"]

    @staticmethod
    def _record_failed_executor_observations(
        db: Session,
        run_id: str,
        step: AgentStep,
        execution: ExecutorResult,
    ) -> None:
        """Persist stable failure codes so Agent retries are independently auditable."""
        for observation in execution.observations:
            if observation.status != "failed" or not observation.error_code:
                continue
            run_repository.append_event(
                db,
                run_id=run_id,
                event_type="executor_tool_failed",
                payload_json={
                    "sequence": step.sequence,
                    "tool": observation.tool_name,
                    "error_code": observation.error_code,
                },
            )

    def _finish_planner_non_plan(
        self,
        db: Session,
        run_id: str,
        run: AgentRun,
        planner_result: PlannerResult,
    ) -> AgentRunResult:
        if planner_result.status == "needs_user":
            planner_error_code = planner_result.error_code or "need_user"
            return self._finish_planner_waiting(
                db,
                run_id,
                run,
                planner_result.user_question,
                event_type="planner_needs_user",
                error_code=planner_result.error_code,
                terminal_contract=build_terminal_contract(
                    error_code=planner_error_code,
                    source_role="planner",
                    phase="planning",
                ),
            )
        if planner_result.error_code == "wall_clock_budget_exhausted":
            # The planner ran out of wall-clock before producing a plan. This
            # is a transport/resource pause, not a business failure: resume()
            # recomputes a fresh deadline and the planner re-attempts its plan.
            # Route to a recoverable waiting_user with a distinct event and the
            # stable error_code for observability, instead of failing the run.
            return self._finish_planner_waiting(
                db,
                run_id,
                run,
                "运行时间预算耗尽（模型响应偏慢），无法生成执行计划。恢复运行将获得新的时间窗口重试。",
                event_type="planner_budget_exhausted",
                error_code="wall_clock_budget_exhausted",
            )
        error_code = planner_result.error_code or "planner_failed"
        run_repository.finish_run(db, run, status=RunStatus.failed, error_code=error_code)
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="run_failed",
            payload_json={
                "error_code": error_code,
                **build_terminal_contract(
                    error_code=error_code,
                    source_role="planner",
                    phase="planning",
                ).as_payload(),
            },
        )
        return AgentRunResult(run_id, RunStatus.failed, None, error_code)

    def _finish_planner_invalid_model(
        self,
        db: Session,
        run_id: str,
        run: AgentRun,
    ) -> AgentRunResult:
        """A planner with persistently invalid output cannot plan safely: wait for a retry."""
        return self._finish_planner_waiting(
            db,
            run_id,
            run,
            "模型输出格式异常，无法生成执行计划。请重试，或补充岗位/条件信息后重新发起。",
            event_type="planner_needs_user",
        )

    def _finish_planner_waiting(
        self,
        db: Session,
        run_id: str,
        run: AgentRun,
        question: str,
        *,
        event_type: str,
        error_code: str | None = None,
        terminal_contract: TerminalContract | None = None,
    ) -> AgentRunResult:
        """Finish a planner-paused run as ``waiting_user`` without an AgentStep.

        The planner has no persisted step yet, so ``_wait_for_user`` (which
        requires a step) cannot be used. This helper closes the run as
        recoverable and emits the caller-supplied ``event_type`` so the
        distinct pause reasons (``planner_needs_user`` for invalid-model /
        need-user, ``planner_budget_exhausted`` for wall-clock) stay
        distinguishable in the event trace. ``error_code`` is persisted on the
        run row and surfaced on the result for observability when set (e.g.
        wall-clock); the invalid-model / need-user paths keep ``error_code=None``.
        """
        run_repository.finish_run(
            db,
            run,
            status=RunStatus.waiting_user,
            final_summary=question,
            error_code=error_code,
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type=event_type,
            payload_json={
                "question": question,
                **(
                    terminal_contract.as_payload()
                    if terminal_contract is not None
                    else build_terminal_contract(
                        error_code=error_code,
                        source_role="planner",
                        phase="planning",
                    ).as_payload()
                ),
            },
        )
        return AgentRunResult(run_id, RunStatus.waiting_user, question, error_code)

    def _rescue_step_succeeded(
        self,
        db: Session,
        run_id: str,
        step: AgentStep,
        execution: ExecutorResult,
        *,
        event_type: str,
        reason: str,
        output_artifact_refs: list[dict[str, str]] | None = None,
    ) -> AgentRunResult:
        """Upgrade a step to succeeded when its deliverable is already tool-backed.

        Used by the termination rescues: a verifier transport failure (invalid
        model output) or the executor's own needs_user hand-off must not fail
        a step whose skill deliverable is already persisted and verified by
        the deterministic contract. The caller guarantees both gates - the
        deliverable contract (tool-backed observation) and the blocked-
        evidence gate (no human-gated evidence) - so this helper never
        auto-passes a blocked step; blocked evidence keeps ending
        human-in-the-loop. The rescue event is explicit and human-readable so
        the trace always distinguishes a rescue from a real verification PASS.
        """
        run_repository.finish_step(
            db,
            step,
            status=StepStatus.succeeded,
            output_artifact_refs=(
                output_artifact_refs
                if output_artifact_refs is not None
                else execution.artifact_refs
            ),
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="step_succeeded",
            payload_json={"sequence": step.sequence},
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type=event_type,
            payload_json={"sequence": step.sequence, "reason": reason},
        )
        return AgentRunResult(run_id, RunStatus.running, execution.summary)

    def _request_replan(
        self,
        db: Session,
        run_id: str,
        step: AgentStep,
        *,
        feedback: str | None,
        summary: str,
        output_artifact_refs: list[dict[str, str]],
        reason: ReplanReason = ReplanReason.VERIFIER_REPLAN,
    ) -> AgentRunResult:
        """Close a step as skipped with a replan_required outcome.

        Shared by the verifier REPLAN decision and the bounded conversions
        (NEED_USER over a satisfied contract, RETRY over a scoped-out tool):
        the step is marked skipped with the stable ``replan_required`` code, a
        ``verification_replan`` event records the verifier feedback, and the
        run loop replans with ``summary`` appended to verifier_feedback.
        """
        run_repository.finish_step(
            db,
            step,
            status=StepStatus.skipped,
            output_artifact_refs=output_artifact_refs,
            error_code="replan_required",
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="verification_replan",
            payload_json={
                "sequence": step.sequence,
                "feedback": feedback,
                "reason": reason.value,
            },
        )
        return AgentRunResult(
            run_id,
            RunStatus.running,
            summary,
            "replan_required",
            reason,
        )

    def _wait_for_user(
        self,
        db: Session,
        run_id: str,
        step: AgentStep,
        question: str | None,
        *,
        output_artifact_refs: list[dict[str, str]] | None = None,
        error_code: str | None = None,
        terminal_contract: TerminalContract | None = None,
    ) -> AgentRunResult:
        """Pause a step-bearing run for a human reply, recoverable via resume().

        ``error_code`` is persisted on both the step and the run row when set
        (e.g. ``wall_clock_budget_exhausted``) so the pause reason is observable
        without inspecting the event trace; genuine need-user pauses keep the
        default ``need_user`` step code and ``None`` run error_code.
        """
        run = db.get(AgentRun, run_id)
        if run is None:  # defensive: foreign-key integrity normally prevents this.
            raise RuntimeError("Agent run disappeared during execution")
        run_repository.finish_step(
            db,
            step,
            status=StepStatus.failed,
            output_artifact_refs=output_artifact_refs,
            error_code=error_code or "need_user",
        )
        run_repository.finish_run(
            db,
            run,
            status=RunStatus.waiting_user,
            final_summary=question,
            error_code=error_code,
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="run_needs_user",
            payload_json={
                "question": question,
                **(
                    terminal_contract.as_payload()
                    if terminal_contract is not None
                    else build_terminal_contract(
                        error_code=error_code,
                        source_role="executor",
                        phase="execution",
                        artifact_count=len(output_artifact_refs or []),
                    ).as_payload()
                ),
            },
        )
        return AgentRunResult(run_id, RunStatus.waiting_user, question, error_code)

    def _fail_step(
        self,
        db: Session,
        run_id: str,
        step: AgentStep,
        error_code: str,
        *,
        output_artifact_refs: list[dict[str, str]] | None = None,
    ) -> AgentRunResult:
        run_repository.finish_step(
            db,
            step,
            status=StepStatus.failed,
            output_artifact_refs=output_artifact_refs,
            error_code=error_code,
        )
        return self._fail_run(db, run_id, error_code)

    @staticmethod
    def _fail_run(db: Session, run_id: str, error_code: str) -> AgentRunResult:
        """Close an already-created run with a stable, user-safe error code."""
        run = db.get(AgentRun, run_id)
        if run is None:  # defensive: foreign-key integrity normally prevents this.
            raise RuntimeError("Agent run disappeared during execution")
        run_repository.finish_run(
            db, run, status=RunStatus.failed, error_code=error_code
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="run_failed",
            payload_json={
                "error_code": error_code,
                **build_terminal_contract(
                    error_code=error_code,
                    source_role="runtime",
                    phase="execution",
                ).as_payload(),
            },
        )
        return AgentRunResult(run_id, RunStatus.failed, None, error_code)


def _execution_progress_fingerprint(execution: ExecutorResult) -> str:
    """Fingerprint evidence identity, not database-generated artifact IDs.

    Verifier retries may persist a fresh report row for the same source. Using
    that row ID would look like progress and permit an infinite retry loop.
    Stable source URLs, content hashes, candidate IDs and terminal reasons are
    the meaningful progress signals.
    """
    observations: list[dict[str, Any]] = []
    for observation in execution.observations:
        output = observation.output if isinstance(observation.output, dict) else {}
        pages = output.get("pages") if isinstance(output.get("pages"), list) else []
        page_keys = [
            {
                "source_url": page.get("source_url"),
                "content_hash": page.get("content_hash"),
                "quality": page.get("quality"),
            }
            for page in pages
            if isinstance(page, dict)
        ]
        candidates = output.get("candidates")
        candidate_keys = []
        if isinstance(candidates, list):
            candidate_keys = [
                {
                    "candidate_id": item.get("candidate_id"),
                    "source_artifact_id": item.get("source_artifact_id"),
                    "source_url": item.get("source_url"),
                    "content_hash": item.get("content_hash"),
                }
                for item in candidates
                if isinstance(item, dict)
            ]
        observations.append(
            {
                "tool": observation.tool_name,
                "status": observation.status,
                "error_code": observation.error_code,
                "source_url": output.get("source_url"),
                "content_hash": output.get("content_hash"),
                "terminal_reason": output.get("terminal_reason"),
                "pages": page_keys,
                "candidates": candidate_keys,
            }
        )
    payload = {"observations": observations}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _normalize_plan_steps(plan: ExecutionPlan) -> list[tuple[tuple[str, ...], str]]:
    """Project a plan onto its comparable step sequence for the replan guard."""
    return [
        (
            tuple(sorted(step.allowed_skills or [])),
            " ".join(str(step.objective or "").split()),
        )
        for step in plan.steps
    ]


def _skill_artifact_source_url(
    artifact_type: str | None, output: dict[str, object]
) -> str | None:
    """Keep a multi-JD report traceable without inventing a synthetic source.

    A matching report is derived from several immutable job pages, so it has no
    single top-level ``source_url``. Its individual match rows retain every
    source; use the first observed source only as the artifact's index field.
    """
    direct_source = output.get("source_url")
    if isinstance(direct_source, str) and direct_source:
        return direct_source
    if artifact_type != "job_matching_report":
        return None
    matches = output.get("matches")
    if not isinstance(matches, list):
        return None
    for match in matches:
        if isinstance(match, dict):
            source_url = match.get("source_url")
            if isinstance(source_url, str) and source_url:
                return source_url
    evaluated_source_urls = output.get("evaluated_source_urls")
    if isinstance(evaluated_source_urls, list):
        return next(
            (
                source_url
                for source_url in evaluated_source_urls
                if isinstance(source_url, str) and source_url
            ),
            None,
        )
    return None


def _job_search_results_are_routable(results: object) -> bool:
    """Return true when a registered route result can drive a public fetch."""
    if not isinstance(results, list) or not results:
        return False
    return any(
        isinstance(item, dict) and bool(_public_urls_from_search_item(item))
        for item in results
    )


def _public_urls_from_search_item(item: dict[str, Any]) -> list[str]:
    """Split duplicated/space-separated sheet URL cells into real URLs."""
    values = [
        item.get("url"),
        item.get("source_url"),
        item.get("apply_url"),
        item.get("link"),
    ]
    prior = item.get("prior_metadata")
    if isinstance(prior, dict):
        values.append(prior.get("apply_url"))
    urls: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        for candidate in re.findall(r"https?://[^\s]+", value):
            candidate = candidate.rstrip('.,;，。；)]}"')
            if candidate and candidate not in urls:
                urls.append(candidate)
    return urls


def _persisted_job_search_observations(
    db: Session,
    run_id: str,
    artifact_refs: list[dict[str, Any]],
) -> list[ToolObservation]:
    """Rehydrate referenced search/sheet rows for deterministic recovery.

    Step inputs carry immutable pointers rather than provider payloads. The
    recovery path reloads only those same-run ``job_search_results`` rows so
    it can fetch their public URLs and derive bounded company search hints.
    """
    refs_by_id = {
        str(ref["artifact_id"]): ref
        for ref in artifact_refs
        if isinstance(ref, dict)
        and ref.get("artifact_type") == "job_search_results"
        and isinstance(ref.get("artifact_id"), str)
    }
    if not refs_by_id:
        return []
    observations: list[ToolObservation] = []
    for artifact in run_repository.list_evidence_artifacts(db, run_id):
        ref = refs_by_id.get(artifact.id)
        if ref is None or artifact.artifact_type != "job_search_results":
            continue
        results = artifact.content_json.get("results")
        if not isinstance(results, list):
            continue
        query = artifact.content_json.get("query")
        tool_name = ref.get("tool")
        if tool_name not in {
            "query-career-sheet-records",
            "search-public-job-pages",
        }:
            tool_name = (
                "query-career-sheet-records"
                if isinstance(query, dict)
                else "search-public-job-pages"
            )
        collection_name = (
            "records"
            if tool_name == "query-career-sheet-records"
            else "results"
        )
        observations.append(
            ToolObservation(
                tool_name=tool_name,
                status="succeeded",
                output={
                    collection_name: results,
                    "query": query,
                    "source_url": artifact.source_url,
                    "content_hash": artifact.content_hash,
                },
            )
        )
    return observations


def _structured_job_candidates(db: Session, run_id: str) -> list[dict[str, Any]]:
    """Flatten the run's persisted ``structured_job_details`` into candidate units.

    Each extract artifact's ``candidates`` list becomes one compact dict
    (title / locations / bounded sections) carrying the artifact's identity.
    Returns [] when the run has no structured extraction yet, so the matching
    tool falls back to raw page evidence.
    """
    items: list[dict[str, Any]] = []
    artifacts = run_repository.list_evidence_artifacts(db, run_id)
    quality_by_source = {
        artifact.source_url: artifact.content_json.get("quality")
        for artifact in artifacts
        if artifact.artifact_type == "public_job_page"
        and isinstance(artifact.content_json.get("quality"), str)
    }
    search_metadata_by_url: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if artifact.artifact_type != "job_search_results":
            continue
        raw_results = artifact.content_json.get("results")
        if not isinstance(raw_results, list):
            continue
        for result in raw_results:
            if not isinstance(result, dict):
                continue
            urls = [
                result.get("url"),
                result.get("source_url"),
                result.get("apply_url"),
                result.get("link"),
            ]
            prior = result.get("prior_metadata")
            if isinstance(prior, dict):
                urls.append(prior.get("apply_url"))
            metadata = {
                key: result.get(key)
                for key in (
                    "updated_at",
                    "published_at",
                    "posted_at",
                    "publish_time",
                    "update_time",
                )
                if result.get(key) not in (None, "")
            }
            if isinstance(prior, dict):
                metadata.update(
                    {
                        key: prior.get(key)
                        for key in (
                            "updated_at",
                            "published_at",
                            "posted_at",
                            "publish_time",
                            "update_time",
                        )
                        if prior.get(key) not in (None, "")
                    }
                )
            if metadata:
                for url in urls:
                    if isinstance(url, str) and url:
                        search_metadata_by_url[url] = metadata
    for artifact in artifacts:
        raw_candidates = artifact.content_json.get("candidates")
        if not isinstance(raw_candidates, list):
            continue
        for candidate_index, candidate in enumerate(raw_candidates):
            if not isinstance(candidate, dict):
                continue
            source_url = candidate.get("apply_url")
            if not isinstance(source_url, str) or not source_url:
                source_url = artifact.source_url
            title = candidate.get("title")
            items.append(
                {
                    "artifact_id": artifact.id,
                    "candidate_id": f"{artifact.id}:candidate:{candidate_index}",
                    #: The evidence artifact this candidate was extracted from
                    #: (``ExtractedJobDetails.evidence_refs``), so a collapsed
                    #: page pointer can also resolve by artifact identity.
                    "source_artifact_id": _evidence_artifact_id(candidate),
                    "source_url": source_url,
                    "page_source_url": artifact.source_url,
                    "page_title": artifact.content_json.get("title")
                    if isinstance(artifact.content_json.get("title"), str)
                    else None,
                    "page_text_prefix": (
                        artifact.content_json.get("visible_text", "")[:2000]
                        if isinstance(artifact.content_json.get("visible_text"), str)
                        else ""
                    ),
                    "apply_url": (
                        candidate.get("apply_url")
                        if isinstance(candidate.get("apply_url"), str)
                        else None
                    ),
                    "content_hash": artifact.content_hash,
                    # The page that produced the candidate is authoritative:
                    # a list page may point at a URL that was later fetched as
                    # a detail page, but its card must not inherit the detail
                    # page's quality and bypass the list-only gate.
                    "source_quality": quality_by_source.get(artifact.source_url)
                    or quality_by_source.get(source_url),
                    **search_metadata_by_url.get(artifact.source_url, {}),
                    **search_metadata_by_url.get(source_url, {}),
                    "title": title if isinstance(title, str) else None,
                    "locations": _string_list(candidate.get("locations")),
                    "recruitment_types": _string_list(
                        candidate.get("recruitment_types")
                    ),
                    "deadline_text": (
                        candidate.get("deadline_text")
                        if isinstance(candidate.get("deadline_text"), str)
                        else None
                    ),
                    "published_at": (
                        candidate.get("published_at")
                        if isinstance(candidate.get("published_at"), str)
                        else None
                    ),
                    "skills": _string_list(candidate.get("skills")),
                    # Card-list extraction can land JD snippets in company_name
                    # (Feishu campus cards) — carry it as bounded evidence so the
                    # match tool can score on it, never as a trusted company fact.
                    "company_name": _bounded_section(candidate.get("company_name")),
                    "responsibilities": _bounded_section(candidate.get("responsibilities")),
                    "requirements": _bounded_section(candidate.get("requirements")),
                    # B1: strength dict {score, tier, base_score, evidence[]},
                    # optional for downstream scoring, carried for audit.
                    "strength": candidate.get("strength"),
                    # Full candidate JD text for deliverable tools (e.g.
                    # build-resume-tailoring-brief): the evidence projection may
                    # collapse an old artifact's visible_text, but the extracted
                    # sections retain the complete job text. Tool-side authority
                    # only - never enters model prompts.
                    "full_text": _full_candidate_text(candidate, title),
                }
            )
    return items


def _full_observed_public_evidence(
    db: Session, run_id: str
) -> list[dict[str, Any]]:
    """Hydrate persisted page pointers only at the deterministic tool boundary."""
    items: list[dict[str, Any]] = []
    artifacts = run_repository.list_evidence_artifacts(db, run_id)
    for artifact in artifacts:
        visible_text = artifact.content_json.get("visible_text")
        if not isinstance(visible_text, str) or not visible_text:
            continue
        item: dict[str, Any] = {
            "artifact_id": artifact.id,
            "source_url": artifact.source_url,
            "content_hash": artifact.content_hash,
            "visible_text": visible_text,
        }
        for key in (
            "title",
            "effective_url",
            "redirect_chain",
            "http_status",
            "quality",
            "quality_signal",
        ):
            value = artifact.content_json.get(key)
            if value not in (None, "", [], {}):
                item[key] = value
        items.append(item)
    return items


def _structured_artifact_visible_text(content_json: dict[str, Any]) -> str:
    """Project extracted JD candidates back into bounded model-visible text.

    Structured extraction artifacts intentionally do not duplicate page
    ``visible_text``.  They are nevertheless durable, tool-produced evidence
    and are the only complete fallback when an older page artifact has been
    collapsed by the run-level evidence budget.  Keep the projection bounded
    so this fallback cannot bypass the global context ceiling.
    """
    raw_candidates = content_json.get("candidates")
    if not isinstance(raw_candidates, list):
        return ""
    sections: list[str] = []
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            continue
        text = _full_candidate_text(candidate, candidate.get("title"))
        if text:
            sections.append(text)
    return "\n\n".join(sections)[:_STRUCTURED_FULL_TEXT_CHARS]


def _evidence_artifact_id(candidate: dict[str, Any]) -> str | None:
    """Return the evidence artifact a candidate was extracted from, if recorded.

    ``ExtractedJobDetails.evidence_refs`` pins each candidate to the source
    page artifact; carrying it lets a collapsed ``observed_public_evidence``
    pointer match by artifact identity even when the extraction found a
    distinct ``apply_url`` for the candidate.
    """
    evidence_refs = candidate.get("evidence_refs")
    if not isinstance(evidence_refs, list):
        return None
    for ref in evidence_refs:
        if not isinstance(ref, dict):
            continue
        value = ref.get("artifact_id")
        if isinstance(value, str) and value:
            return value
    return None


def _full_candidate_text(candidate: dict[str, Any], title: object) -> str:
    """Preserve the complete candidate JD text for deliverable tools.

    Section fields are kept at their persisted length (extraction already
    bounds them to the page's visible text); only a defensive per-candidate
    cap applies, so a collapsed page pointer can still resolve the full JD.
    """
    company_name = candidate.get("company_name")
    responsibilities = candidate.get("responsibilities")
    requirements = candidate.get("requirements")
    parts = [
        title if isinstance(title, str) and title else None,
        company_name if isinstance(company_name, str) and company_name else None,
        " ".join(_string_list(candidate.get("locations"))),
        responsibilities if isinstance(responsibilities, str) and responsibilities else None,
        requirements if isinstance(requirements, str) and requirements else None,
    ]
    return "\n".join(part for part in parts if part)[:_STRUCTURED_FULL_TEXT_CHARS]


def _string_list(value: object) -> list[str]:
    """Return the string items of a list value, or [] for non-list input."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _bounded_section(value: object) -> str:
    """Return a string section truncated to the structured-candidate budget."""
    if not isinstance(value, str):
        return ""
    return value[:_STRUCTURED_SECTION_CHARS]
