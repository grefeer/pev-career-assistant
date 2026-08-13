"""DeepAgents-backed Executor adapter for the PEV runtime.

The adapter deliberately keeps the application's ToolRegistry as the only
business-tool boundary. DeepAgents supplies the autonomous loop, progressive
Skill disclosure, and bounded filesystem tools; this module translates every
business-tool call back into the existing ``ToolObservation`` contract so the
Runtime and unchanged Verifier continue to operate on durable evidence.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError
from pydantic import BaseModel, Field, ValidationError

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.model_budget import ModelCallBudget, estimate_input_tokens
from backend.app.services.agent_runtime.model_gateway import (
    AgentModelGateway,
    _extract_first_balanced_json_object,
)
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorResult,
    PlanStep,
    ToolObservation,
)
from backend.app.services.agent_runtime.skill_definition import SkillRegistry
from backend.app.services.agent_runtime.skill_script_runner import (
    RunSkillScriptInput,
    SkillScriptRunner,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_registry import ToolRegistry
from backend.app.services.agent_runtime.tracing import DecisionTrace
from backend.app.services.agent_runtime.tracing import decision_summary
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget

logger = logging.getLogger(__name__)


class DeepExecutorResponse(BaseModel):
    """Structured terminal state emitted by the DeepAgents Executor."""

    model_config = {"extra": "forbid"}

    status: str = Field(pattern="^(succeeded|needs_user|failed)$")
    summary: str = ""
    user_question: str | None = None
    error_code: str | None = None
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)


class _DeepExecutorBudgetError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _DeepExecutorStallError(RuntimeError):
    def __init__(self, question: str) -> None:
        super().__init__(question)
        self.question = question


@dataclass
class _DeepExecutionLedger:
    """Bounded, retry-stable call state shared by all DeepAgents tools."""

    candidate_urls: frozenset[str]
    prior_succeeded_calls: list[dict[str, str]] = field(default_factory=list)
    prior_stable_failed_calls: list[dict[str, str]] = field(default_factory=list)
    succeeded_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    stable_failed_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    failed_candidate_urls: set[str] = field(default_factory=set)
    unavailable_tools: set[str] = field(default_factory=set)
    invalid_input_signatures: list[str] = field(default_factory=list)
    blocked_public_domains: set[str] = field(default_factory=set)
    public_search_query_hashes: set[str] = field(default_factory=set)
    consecutive_stalls: int = 0
    total_wasted_turns: int = 0

    def _hash(self, payload: dict[str, Any]) -> str:
        from backend.app.services.agent_runtime.executor_agent import _input_hash

        return _input_hash(payload)

    def is_duplicate_success(self, name: str, payload: dict[str, Any]) -> bool:
        digest = self._hash(payload)
        return any(
            tool == name and self._hash(existing) == digest
            for tool, existing in self.succeeded_calls
        ) or any(
            entry.get("tool") == name and entry.get("hash") == digest
            for entry in self.prior_succeeded_calls
        )

    def is_duplicate_stable_failure(self, name: str, payload: dict[str, Any]) -> bool:
        digest = self._hash(payload)
        return any(
            tool == name and self._hash(existing) == digest
            for tool, existing in self.stable_failed_calls
        ) or any(
            entry.get("tool") == name and entry.get("hash") == digest
            for entry in self.prior_stable_failed_calls
        )

    def wasted(self, *, consecutive: bool = False) -> None:
        self.total_wasted_turns += 1
        if consecutive:
            self.consecutive_stalls += 1
        else:
            self.consecutive_stalls = 0
        if self.consecutive_stalls >= 3 or self.total_wasted_turns >= 3:
            raise _DeepExecutorStallError(
                "连续或累计多次工具调用未取得进展，请人工确认岗位证据后重试。"
            )

    def record(self, name: str, payload: dict[str, Any], observation: ToolObservation) -> None:
        if observation.status == "succeeded":
            self.succeeded_calls.append((name, payload))
            self.consecutive_stalls = 0
            return
        if observation.error_code in {
            "sheet_rate_limited",
            "sheet_call_failed",
            "sheet_bridge_unavailable",
            "route_already_consumed",
            "tool_skill_forbidden",
            "unknown_tool",
            "invalid_tool_input",
        }:
            self.stable_failed_calls.append((name, payload))
        if observation.error_code in {
            "public_fetch_failed",
            "empty_public_page",
            "public_page_content_insufficient",
            "dead_link",
            "anti_bot_challenge",
            "access_denied",
            "login_required",
            "captcha",
            "domain_temporarily_blocked",
        }:
            for url in _payload_fetch_urls(payload) & self.candidate_urls:
                self.failed_candidate_urls.add(url)
        output = observation.output
        if isinstance(output, dict) and isinstance(output.get("failures"), list):
            candidate_failures = {
                entry.get("source_url")
                for entry in output["failures"]
                if isinstance(entry, dict)
                and isinstance(entry.get("source_url"), str)
                and entry.get("error_code") in {
                    "public_fetch_failed",
                    "empty_public_page",
                    "public_page_content_insufficient",
                    "dead_link",
                    "anti_bot_challenge",
                    "access_denied",
                    "login_required",
                    "captcha",
                    "domain_temporarily_blocked",
                }
            }
            self.failed_candidate_urls.update(
                value for value in candidate_failures if value in self.candidate_urls
            )
        if observation.error_code in {
            "sheet_rate_limited",
            "sheet_call_failed",
            "sheet_bridge_unavailable",
            "route_already_consumed",
        }:
            self.unavailable_tools.add(name)
        if observation.error_code == "invalid_tool_input" and observation.error_message:
            from backend.app.services.agent_runtime.executor_agent import _validation_signature

            signature = _validation_signature(observation.error_message)
            if signature:
                self.invalid_input_signatures.append(signature)
                if self.invalid_input_signatures.count(signature) >= 2:
                    raise _DeepExecutorStallError(
                        "工具输入连续违反同一字段契约，请补充合法值后重试。"
                    )
        self.wasted()

    def snapshot(self) -> dict[str, Any]:
        from backend.app.services.agent_runtime.executor_agent import _snapshot_execution_state

        return _snapshot_execution_state(
            succeeded_calls=self.succeeded_calls,
            prior_succeeded_calls=self.prior_succeeded_calls,
            consecutive_stalls=self.consecutive_stalls,
            total_wasted_turns=self.total_wasted_turns,
            stable_failed_calls=self.stable_failed_calls,
            prior_stable_failed_calls=self.prior_stable_failed_calls,
            failed_candidate_urls=self.failed_candidate_urls,
            phase="deep_executor",
            candidate_status=(
                "all_unusable"
                if self.candidate_urls and self.candidate_urls.issubset(self.failed_candidate_urls)
                else "partially_processed"
                if self.failed_candidate_urls
                else "supplied"
                if self.candidate_urls
                else "unknown"
            ),
            invalid_input_signatures=self.invalid_input_signatures,
            unavailable_tools=self.unavailable_tools,
            blocked_public_domains=list(self.blocked_public_domains),
            public_search_query_hashes=list(self.public_search_query_hashes),
        )


class _DeepExecutorBudgetMiddleware(AgentMiddleware[Any, Any, Any]):
    """Apply PEV budgets to every DeepAgents model/tool boundary."""

    def __init__(
        self,
        *,
        turn_budget: AgentTurnBudget | None,
        model_budget: ModelCallBudget | None,
        deadline: float | None,
        max_model_calls: int,
        model_call_counter: dict[str, int] | None = None,
    ) -> None:
        # ``turn_budget`` belongs to the PEV lifecycle and is consumed once by
        # DeepExecutorAgent.run.  It must not be charged again for every
        # internal DeepAgents model call.
        self._turn_budget = turn_budget
        self._model_budget = model_budget
        self._deadline = deadline
        self._max_model_calls = max(1, max_model_calls)
        self._model_call_counter = model_call_counter
        self._model_calls = 0

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        self._check_deadline()
        bounded_messages = _bounded_deep_agent_messages(
            getattr(request, "messages", None)
        )
        if bounded_messages is not None:
            request = request.override(messages=bounded_messages)
        self._model_calls += 1
        if self._model_call_counter is not None:
            self._model_call_counter["count"] = self._model_calls
        if self._model_calls > self._max_model_calls:
            raise _DeepExecutorBudgetError("deep_executor_call_limit_exhausted")
        reserved = False
        if self._model_budget is not None:
            state = _bounded_model_request_state(request)
            estimate = estimate_input_tokens("deep_executor", state)
            if not self._model_budget.try_reserve(estimate):
                raise _DeepExecutorBudgetError("model_budget_exhausted")
            reserved = True
        response = None
        handler_failed = False
        try:
            response = handler(request)
        except BaseException:
            handler_failed = True
            if reserved and self._model_budget is not None:
                # A failed provider call must not permanently consume a
                # reservation or make the next retry look over budget.
                self._model_budget.cancel()
            raise
        finally:
            if reserved and self._model_budget is not None and not handler_failed:
                if not self._model_budget.record(_usage_from_model_response(response)):
                    raise _DeepExecutorBudgetError("model_budget_exhausted")
        return response

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        self._check_deadline()
        # Business-tool budgets are consumed in the wrapper closure after
        # duplicate/candidate/stall checks. Built-in filesystem tools are
        # intentionally outside the application's ToolCallBudget.
        return handler(request)

    def _check_deadline(self) -> None:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise _DeepExecutorBudgetError("wall_clock_budget_exhausted")


class _DeepExecutorToolFilterMiddleware(AgentMiddleware[Any, Any, Any]):
    """Hide generic execution and subagent tools from the step agent."""

    _EXCLUDED = frozenset({"execute", "task", "write_todos"})

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        tools = getattr(request, "tools", None)
        if isinstance(tools, list):
            request = request.override(
                tools=[
                    tool
                    for tool in tools
                    if getattr(tool, "name", None) not in self._EXCLUDED
                ]
            )
        return handler(request)

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(request)


class DeepExecutorAgent:
    """Run one PEV step through ``create_deep_agent``."""

    def __init__(
        self,
        *,
        gateway: AgentModelGateway,
        tools: ToolRegistry,
        skills: SkillRegistry | None,
        skill_root: Path,
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._skills = skills
        self._skill_root = skill_root.resolve()

    def run(
        self,
        *,
        task: AgentTaskRequest,
        plan: ExecutionPlan,
        step: PlanStep,
        context: ToolContext,
        trace: DecisionTrace | None = None,
        tool_budget: ToolCallBudget | None = None,
        turn_budget: AgentTurnBudget | None = None,
        model_budget: ModelCallBudget | None = None,
        deadline: float | None = None,
        prior_observations: list[ToolObservation] | None = None,
    ) -> ExecutorResult:
        model = getattr(self._gateway, "chat_model", None)
        if model is None:
            model = getattr(self._gateway, "_model", None)
        if model is None:
            return ExecutorResult(
                status="failed",
                error_code="deep_executor_model_unavailable",
            )

        if deadline is not None and time.monotonic() >= deadline:
            return ExecutorResult(
                status="failed",
                error_code="wall_clock_budget_exhausted",
            )
        allowed_skills = tuple(dict.fromkeys(step.allowed_skills))
        if len(allowed_skills) != 1:
            return ExecutorResult(
                status="needs_user",
                user_question="当前计划步骤必须只绑定一个 Skill，请重新规划后重试。",
                error_code="deep_executor_requires_one_skill",
            )
        skill_name = allowed_skills[0]
        skill_dir = (self._skill_root / skill_name).resolve()
        if not _is_within(skill_dir, self._skill_root) or not skill_dir.is_dir():
            return ExecutorResult(
                status="failed",
                error_code="skill_package_not_found",
            )

        # One deep-agent lifecycle is one PEV Executor turn. Internal model
        # calls are bounded independently by the middleware below.
        if turn_budget is not None and not turn_budget.try_consume():
            return ExecutorResult(
                status="failed",
                error_code="agent_turn_budget_exhausted",
            )
        max_model_calls = min(12, max(4, task.budget.max_agent_turns))
        model_call_counter = {"count": 0}

        from backend.app.services.agent_runtime.executor_agent import (
            _load_execution_state,
            _load_failed_candidate_urls,
            _load_invalid_input_signatures,
            _load_stable_failed_calls,
            _load_unavailable_tools,
            _scope_feedback_to_step_catalog,
        )

        prior_succeeded_calls, consecutive_stalls, total_wasted_turns = _load_execution_state(task)
        ledger = _DeepExecutionLedger(
            candidate_urls=frozenset(_candidate_urls(task)),
            prior_succeeded_calls=prior_succeeded_calls,
            prior_stable_failed_calls=_load_stable_failed_calls(task),
            failed_candidate_urls=_load_failed_candidate_urls(task.execution_state),
            invalid_input_signatures=_load_invalid_input_signatures(task),
            unavailable_tools=_load_unavailable_tools(task),
            consecutive_stalls=consecutive_stalls,
            total_wasted_turns=total_wasted_turns,
        )
        observations: list[ToolObservation] = []
        projected_observations: list[dict[str, Any]] = []
        context_holder = {"value": context}
        script_runner = SkillScriptRunner(skill_dir)
        wrapped_tools = self._build_tools(
            context_holder=context_holder,
            allowed_skills=frozenset(allowed_skills),
            observations=observations,
            projected_observations=projected_observations,
            ledger=ledger,
            tool_budget=tool_budget,
            script_runner=script_runner,
        )
        scoped_out_tool_names = frozenset(
            entry["name"]
            for entry in self._tools.tool_catalog(
                role=AgentRole.executor, allowed_skills=None
            )
            if entry["name"]
            not in {
                item["name"]
                for item in self._tools.tool_catalog(
                    role=AgentRole.executor, allowed_skills=frozenset(allowed_skills)
                )
            }
        )
        system_prompt = self._system_prompt(skill_name)
        human_input = self._input_message(
            task=task,
            plan=plan,
            step=step,
            context=context,
            private_context=(
                self._skills.project_private_context(
                    allowed_skills, task.private_context
                )
                if self._skills is not None
                else {}
            ),
            prior_observations=prior_observations or [],
            projected_observations=projected_observations,
            verifier_feedback=_scope_feedback_to_step_catalog(
                task.context.get("verifier_feedback", []),
                scoped_out_tool_names=scoped_out_tool_names,
            ),
        )
        try:
            agent = self._build_agent(
                model=model,
                tools=wrapped_tools,
                skill_dir=skill_dir,
                skill_name=skill_name,
                turn_budget=turn_budget,
                model_budget=model_budget,
                deadline=deadline,
                execution_policy=system_prompt,
                max_model_calls=max_model_calls,
                model_call_counter=model_call_counter,
            )
            recursion_limit = max(12, max_model_calls * 3 + 4)
            result = agent.invoke(
                {"messages": [{"role": "user", "content": human_input}]},
                config={
                    "recursion_limit": recursion_limit,
                    "configurable": {
                        "thread_id": f"{context.run_id}:{step.step_id}"
                    },
                },
            )
        except _DeepExecutorStallError as error:
            return self._result(
                status="needs_user",
                observations=observations,
                summary=None,
                user_question=error.question,
                error_code="executor_stalled",
                artifact_refs=[],
                ledger=ledger,
                trace=trace,
                trace_metadata={"internal_model_calls": model_call_counter["count"]},
            )
        except _DeepExecutorBudgetError as error:
            if error.code == "deep_executor_call_limit_exhausted":
                return self._result(
                    status="needs_user",
                    observations=observations,
                    summary=None,
                    user_question=(
                        "本步骤的内部模型调用已达到安全上限，可能需要缩小任务范围或补充岗位证据后重试。"
                    ),
                    error_code=error.code,
                    artifact_refs=[],
                    ledger=ledger,
                    trace=trace,
                    trace_metadata={"internal_model_calls": model_call_counter["count"]},
                )
            return self._result(
                status="failed",
                observations=observations,
                summary=None,
                user_question=None,
                error_code=error.code,
                artifact_refs=[],
                ledger=ledger,
                trace=trace,
                trace_metadata={"internal_model_calls": model_call_counter["count"]},
            )
        except GraphRecursionError:
            return self._result(
                status="failed",
                observations=observations,
                summary=None,
                user_question=None,
                error_code="deep_executor_recursion_limit",
                artifact_refs=[],
                ledger=ledger,
                trace=trace,
                trace_metadata={"internal_model_calls": model_call_counter["count"]},
            )
        except TimeoutError:
            return self._result(
                status="failed",
                observations=observations,
                summary=None,
                user_question=None,
                error_code="wall_clock_budget_exhausted",
                artifact_refs=[],
                ledger=ledger,
                trace=trace,
                trace_metadata={"internal_model_calls": model_call_counter["count"]},
            )
        except Exception as error:
            logger.exception(
                "deep executor failed run_id=%s step_id=%s error_type=%s",
                context.run_id,
                step.step_id,
                type(error).__name__,
            )
            return self._result(
                status="failed",
                observations=observations,
                summary=None,
                user_question=None,
                error_code="deep_executor_failed",
                artifact_refs=[],
                ledger=ledger,
                trace=trace,
                trace_metadata={"internal_model_calls": model_call_counter["count"]},
            )
        terminal = _terminal_from_messages(result)
        if terminal is None:
            return self._result(
                status="failed",
                observations=observations,
                summary=None,
                user_question=None,
                error_code="deep_executor_invalid_response",
                artifact_refs=[],
                ledger=ledger,
                trace=trace,
                trace_metadata={"internal_model_calls": model_call_counter["count"]},
            )
        return self._result(
            status=terminal.status,
            observations=observations,
            summary=terminal.summary,
            user_question=terminal.user_question,
            error_code=terminal.error_code,
            artifact_refs=terminal.artifact_refs,
            ledger=ledger,
            trace=trace,
            trace_metadata={"internal_model_calls": model_call_counter["count"]},
        )

    @staticmethod
    def _result(
        *,
        status: str,
        observations: list[ToolObservation],
        summary: str | None,
        user_question: str | None,
        error_code: str | None,
        artifact_refs: list[dict[str, Any]],
        ledger: _DeepExecutionLedger,
        trace: DecisionTrace | None,
        trace_metadata: dict[str, Any] | None = None,
    ) -> ExecutorResult:
        """Map untrusted model output into the strict PEV result contract."""
        if status == "succeeded" and not isinstance(summary, str):
            return ExecutorResult(
                status="failed",
                observations=observations,
                error_code="deep_executor_invalid_terminal",
                execution_state=ledger.snapshot(),
            )
        if status == "succeeded" and not summary.strip():
            return ExecutorResult(
                status="failed",
                observations=observations,
                error_code="deep_executor_invalid_terminal",
                execution_state=ledger.snapshot(),
            )
        if status == "needs_user" and (not isinstance(user_question, str) or not user_question.strip()):
            return ExecutorResult(
                status="failed",
                observations=observations,
                error_code="deep_executor_invalid_terminal",
                execution_state=ledger.snapshot(),
            )
        try:
            result = ExecutorResult(
                status=status,
                summary=summary.strip() if isinstance(summary, str) else None,
                user_question=user_question.strip() if isinstance(user_question, str) else None,
                error_code=error_code,
                artifact_refs=artifact_refs,
                observations=observations,
                execution_state=ledger.snapshot(),
            )
        except ValidationError:
            return ExecutorResult(
                status="failed",
                observations=observations,
                error_code="deep_executor_invalid_terminal",
                execution_state=ledger.snapshot(),
            )
        if trace is not None:
            metadata = {"deep_executor": True}
            if trace_metadata:
                metadata.update(trace_metadata)
            decision = decision_summary(action=status)
            if error_code:
                decision["error_code"] = error_code
            trace(
                AgentRole.executor,
                decision,
                metadata,
            )
        return result

    def _build_tools(
        self,
        *,
        context_holder: dict[str, ToolContext],
        allowed_skills: frozenset[str],
        observations: list[ToolObservation],
        projected_observations: list[dict[str, Any]],
        ledger: _DeepExecutionLedger,
        tool_budget: ToolCallBudget | None,
        script_runner: SkillScriptRunner,
    ) -> list[Any]:
        from langchain_core.tools import StructuredTool
        from backend.app.services.agent_runtime.executor_agent import _with_observed_page
        from backend.app.services.agent_runtime.observation_projection import (
            observation_for_decision,
        )

        def append_observation(observation: ToolObservation) -> str:
            observations.append(observation)
            projected_observations.append(observation_for_decision(observation))
            return _observation_text(observation, projected=True)

        def guard_call(name: str, payload: dict[str, Any]) -> str | None:
            if name in ledger.unavailable_tools:
                raise _DeepExecutorStallError(
                    "当前招聘来源已确认不可用，请改用其他公开来源或提供岗位文本。"
                )
            if ledger.is_duplicate_success(name, payload) or ledger.is_duplicate_stable_failure(
                name, payload
            ):
                duplicate = ToolObservation(
                    tool_name=name,
                    status="failed",
                    error_code="duplicate_tool_call",
                )
                ledger.wasted(consecutive=True)
                return append_observation(duplicate)
            if (
                name in {"search-public-job-pages", "query-career-sheet-records"}
                and ledger.candidate_urls
                and not ledger.candidate_urls.issubset(ledger.failed_candidate_urls)
            ):
                blocked = ToolObservation(
                    tool_name=name,
                    status="failed",
                    error_code="candidate_urls_already_supplied",
                    error_message="请先抓取并处理仍未失败的候选 URL，再使用替代来源。",
                )
                ledger.wasted(consecutive=True)
                return append_observation(blocked)
            if tool_budget is not None and not tool_budget.try_consume():
                raise _DeepExecutorBudgetError("tool_budget_exhausted")
            return None

        definitions = [
            definition
            for definition in self._tools.definitions
            if AgentRole.executor in definition.allowed_roles
            and definition.skill_name in allowed_skills
        ]
        wrapped: list[Any] = []
        for definition in definitions:
            def invoke_tool(
                _definition=definition,
                **payload: Any,
            ) -> str:
                duplicate_result = guard_call(_definition.name, payload)
                if duplicate_result is not None:
                    return duplicate_result
                observation = self._tools.invoke(
                    role=AgentRole.executor,
                    name=_definition.name,
                    context=context_holder["value"],
                    payload=payload,
                    allowed_skills=allowed_skills,
                )
                observations.append(observation)
                projected_observations.append(observation_for_decision(observation))
                context_holder["value"] = _with_observed_page(
                    context_holder["value"], observation
                )
                ledger.blocked_public_domains = set(
                    context_holder["value"].metadata.get("blocked_public_domains", [])
                )
                ledger.public_search_query_hashes = set(
                    context_holder["value"].metadata.get("public_search_query_hashes", [])
                )
                ledger.record(_definition.name, payload, observation)
                return _observation_text(observation, projected=True)

            wrapped.append(
                StructuredTool.from_function(
                    invoke_tool,
                    name=definition.name,
                    description=definition.description or definition.name,
                    args_schema=definition.input_model,
                )
            )

        def run_skill_script(**payload: Any) -> str:
            duplicate_result = guard_call("run_skill_script", payload)
            if duplicate_result is not None:
                return duplicate_result
            parsed = RunSkillScriptInput.model_validate(payload)
            output = script_runner.run(parsed)
            observation = ToolObservation(
                tool_name="run_skill_script",
                status="succeeded" if output.status == "succeeded" else "failed",
                output=output.model_dump(mode="json")
                if output.status == "succeeded"
                else None,
                error_code=output.error_code,
                error_message=output.stderr[:500] if output.stderr else None,
            )
            observations.append(observation)
            projected_observations.append(observation_for_decision(observation))
            ledger.record("run_skill_script", payload, observation)
            return _observation_text(observation, projected=True)

        wrapped.append(
            StructuredTool.from_function(
                run_skill_script,
                name="run_skill_script",
                description=(
                    "Run one Python helper under the active Skill directory. "
                    "Use a relative .py path; never use an absolute path or .. ."
                ),
                args_schema=RunSkillScriptInput,
            )
        )
        return wrapped

    @staticmethod
    def _build_agent(
        *,
        model: Any,
        tools: list[Any],
        skill_dir: Path,
        skill_name: str,
        turn_budget: AgentTurnBudget | None,
        model_budget: ModelCallBudget | None,
        deadline: float | None,
        execution_policy: str,
        max_model_calls: int = 12,
        model_call_counter: dict[str, int] | None = None,
    ) -> Any:
        from deepagents import create_deep_agent
        from deepagents.backends import FilesystemBackend
        from deepagents.middleware.filesystem import FilesystemPermission
        from langgraph.checkpoint.memory import InMemorySaver

        backend = FilesystemBackend(root_dir=skill_dir, virtual_mode=True)
        permissions = [
            FilesystemPermission(
                operations=["write"],
                paths=["/**"],
                mode="deny",
            ),
            FilesystemPermission(
                operations=["read"],
                paths=[
                    "/anti_crawl/store/profiles/**",
                    "/.env",
                    "/settings.json",
                    "/.claude/logs/**",
                ],
                mode="deny",
            ),
            FilesystemPermission(
                operations=["read"], paths=["/**"], mode="allow"
            ),
        ]
        tool_catalog = "\n".join(
            f"- {getattr(tool, 'name', 'unknown')}: {getattr(tool, 'description', '')}"
            for tool in tools
        )
        return create_deep_agent(
            model=model,
            tools=tools,
            system_prompt=(
                execution_policy
                + "\n\n"
                "You are the Executor for exactly one PEV plan step. The active "
                f"Skill is '{skill_name}'. Use the listed business tools first; "
                "read SKILL.md or references only when the task genuinely needs "
                "their procedure, and never write files. Do not repeatedly call "
                "filesystem tools, write_todos, or the same business tool. "
                "Never invent evidence, bypass login/captcha/anti-bot, or submit "
                "an irreversible application.\n\n"
                "Scoped business tools:\n"
                f"{tool_catalog}\n\n"
                "When work is complete, output ONLY one final JSON object with "
                "status=succeeded, needs_user, or failed. succeeded requires a "
                "non-empty summary; needs_user requires a non-empty user_question; "
                "failed may include error_code. Do not output a tool result as a "
                "terminal status."
            ),
            # Directly inject the scoped policy above. Progressive skill
            # disclosure caused extra filesystem calls and consumed the old
            # lifecycle turn budget before business work started.
            skills=None,
            backend=backend,
            permissions=permissions,
            middleware=[
                _DeepExecutorBudgetMiddleware(
                    turn_budget=turn_budget,
                    model_budget=model_budget,
                    deadline=deadline,
                    max_model_calls=max_model_calls,
                    model_call_counter=model_call_counter,
                ),
                _DeepExecutorToolFilterMiddleware(),
            ],
            checkpointer=InMemorySaver(),
            name="executor",
        )

    def _system_prompt(self, skill_name: str) -> str:
        if self._skills is None:
            return f"Active Skill: {skill_name}"
        definition = self._skills.get(skill_name)
        return definition.execution_policy if definition is not None else f"Active Skill: {skill_name}"

    @staticmethod
    def _input_message(
        *,
        task: AgentTaskRequest,
        plan: ExecutionPlan,
        step: PlanStep,
        context: ToolContext,
        private_context: dict[str, Any],
        prior_observations: list[ToolObservation],
        projected_observations: list[dict[str, Any]],
        verifier_feedback: object,
    ) -> str:
        from backend.app.services.agent_runtime.observation_projection import (
            observation_for_decision,
        )

        return json.dumps(
            {
                "goal": task.goal,
                "plan": plan.model_dump(mode="json"),
                "step": step.model_dump(mode="json"),
                "context": _bounded_context_metadata(context.metadata),
                "task_context": {
                    "candidate_urls": task.context.get("candidate_urls", []),
                    "resolved_step_inputs": task.context.get("resolved_step_inputs", {}),
                    "verifier_feedback": verifier_feedback,
                    "replan_state": task.replan_state.model_dump(mode="json"),
                },
                "private_context": private_context,
                "prior_observations": [
                    observation_for_decision(observation)
                    for observation in prior_observations[-10:]
                ],
                "current_observations": projected_observations[-10:],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )


def _observation_text(
    observation: ToolObservation, *, projected: bool = False
) -> str:
    from backend.app.services.agent_runtime.observation_projection import (
        observation_for_decision,
    )

    payload = observation_for_decision(observation) if projected else observation.model_dump(mode="json")
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _terminal_from_messages(result: Any) -> DeepExecutorResponse | None:
    if not isinstance(result, dict):
        return None
    messages = result.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        # Tool-call AI messages are intermediate decisions, not terminal
        # output. In particular, a tool result may itself contain JSON that
        # looks like an ExecutorResponse.
        if getattr(message, "tool_calls", None):
            continue
        content = _message_text(getattr(message, "content", None))
        if not content:
            continue
        try:
            candidate = _extract_first_balanced_json_object(content)
            if candidate is None:
                candidate = _strip_json_fence(content)
            return DeepExecutorResponse.model_validate(json.loads(candidate))
        except Exception:
            continue
    return None


def _strip_json_fence(content: str) -> str:
    value = content.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return value


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts).strip()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _usage_from_model_response(response: Any) -> dict[str, int] | None:
    if response is None:
        return None
    results = getattr(response, "result", None)
    if not isinstance(results, list) or not results:
        return None
    message = results[0]
    usage = getattr(message, "usage_metadata", None)
    if not isinstance(usage, dict):
        return None
    return {
        key: value
        for key, value in {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }.items()
        if isinstance(value, int) and value >= 0
    }


def _candidate_urls(task: AgentTaskRequest) -> list[str]:
    raw = task.context.get("candidate_urls")
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(value.strip() for value in raw if isinstance(value, str) and value.strip()))


def _bounded_context_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Keep model context pointer-sized while tools retain full evidence."""
    bounded: dict[str, Any] = {}
    for key in (
        "blocked_public_domains",
        "public_search_query_hashes",
        "confirmed_profile_facts",
        "resolved_step_inputs",
    ):
        if key in metadata:
            bounded[key] = metadata[key]
    evidence = metadata.get("observed_public_evidence")
    if isinstance(evidence, list):
        bounded["observed_public_evidence"] = [
            {
                field: item[field]
                for field in (
                    "artifact_id",
                    "source_url",
                    "content_hash",
                    "effective_url",
                    "title",
                    "http_status",
                )
                if isinstance(item, dict) and field in item
            }
            | ({"visible_text": item["visible_text"][:1200]} if isinstance(item, dict) and isinstance(item.get("visible_text"), str) else {})
            for item in evidence[:10]
            if isinstance(item, dict)
        ]
    candidates = metadata.get("structured_job_candidates")
    if isinstance(candidates, list):
        bounded["structured_job_candidates"] = [
            {
                field: item[field]
                for field in (
                    "candidate_id",
                    "artifact_id",
                    "source_artifact_id",
                    "source_url",
                    "title",
                    "company",
                    "content_hash",
                )
                if isinstance(item, dict) and field in item
            }
            for item in candidates[:20]
            if isinstance(item, dict)
        ]
    return bounded


def _bounded_model_request_state(request: Any) -> dict[str, Any]:
    """Estimate only textual prompt/tool schema content, not message metadata."""
    messages: list[str] = []
    for message in getattr(request, "messages", []):
        content = getattr(message, "content", "")
        if isinstance(content, str):
            messages.append(content)
        elif isinstance(content, list):
            messages.append(json.dumps(content, ensure_ascii=False, default=str))
    tools = []
    for tool in getattr(request, "tools", []):
        name = getattr(tool, "name", None)
        description = getattr(tool, "description", None)
        if isinstance(name, str):
            tools.append({"name": name, "description": description if isinstance(description, str) else ""})
    return {
        "system_prompt": getattr(request, "system_prompt", ""),
        "messages": messages,
        "tools": tools,
    }


def _payload_fetch_urls(payload: dict[str, Any]) -> set[str]:
    raw = payload.get("urls")
    if isinstance(raw, list):
        return {value.strip() for value in raw if isinstance(value, str) and value.strip()}
    value = payload.get("url")
    return {value.strip()} if isinstance(value, str) and value.strip() else set()


def _bounded_deep_agent_messages(messages: Any) -> list[Any] | None:
    """Bound replayed tool history so input usage stays approximately linear.

    DeepAgents retains every intermediate tool message. Sending the complete
    history on every model call makes input-token usage grow roughly O(n²).
    The initial system/user request is retained, followed by a recent window;
    the window moves left when it would begin with an orphan ToolMessage.
    Current observations and durable execution state are already supplied by
    the bounded step input and ledger, so old raw tool payloads are not needed
    for decision making.
    """
    if not isinstance(messages, list) or len(messages) <= 18:
        return None
    prefix = messages[:2]
    start = max(2, len(messages) - 16)
    while start > 2 and getattr(messages[start], "type", None) == "tool":
        start -= 1
    return [*prefix, *messages[start:]]
