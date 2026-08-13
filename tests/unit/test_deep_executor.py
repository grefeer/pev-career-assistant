from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.deep_executor import (
    DeepExecutorAgent,
    _DeepExecutionLedger,
    _EXECUTOR_OPERATING_PROCEDURE,
    _bounded_context_metadata,
    _bounded_deep_agent_messages,
    _produces_persisted_artifact,
    _terminal_from_messages,
)
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    PlanStep,
    StepOutputRef,
    ToolObservation,
)
from backend.app.services.agent_runtime.skill_definition import (
    CompletionContract,
    SkillDefinition,
    SkillRegistry,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget
from backend.app.services.career_skills.manifest import build_career_skill_registry
from tests.unit.deepagents_testkit import ScriptedModel


class EchoInput(BaseModel):
    value: str


class EchoOutput(BaseModel):
    value: str


class Gateway:
    def __init__(self, model: object) -> None:
        self._model = model


class RecordingModel(ScriptedModel):
    bound_tool_names: ClassVar[list[list[str | None]]] = []

    def bind_tools(self, tools: object, **kwargs: object) -> object:
        self.bound_tool_names.append([getattr(tool, "name", None) for tool in tools])
        return self


def _inputs() -> tuple[AgentTaskRequest, ExecutionPlan, PlanStep]:
    task = AgentTaskRequest(goal="执行测试步骤", allowed_skills=["job-discovery"])
    step = PlanStep(
        step_id="discover",
        objective="调用业务工具",
        allowed_skills=["job-discovery"],
    )
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L1,
        success_criteria=["完成工具调用"],
        steps=[step],
    )
    return task, plan, step


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo-tool",
            skill_name="job-discovery",
            input_model=EchoInput,
            output_model=EchoOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, payload: {"value": payload.value},
        )
    )
    return registry


def test_deep_executor_bridges_business_tool_and_structured_terminal_state() -> None:
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "echo-tool",
                        "args": {"value": "ok"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            '{"status":"succeeded","summary":"完成","artifact_refs":[]}',
        ]
    )
    task, plan, step = _inputs()

    result = DeepExecutorAgent(
        gateway=Gateway(model),
        tools=_registry(),
        skills=None,
        skill_root=Path("skill"),
    ).run(
        task=task,
        plan=plan,
        step=step,
        context=ToolContext(user_id="user", run_id="run"),
    )

    assert result.status == "succeeded"
    assert result.summary == "完成"
    assert [observation.tool_name for observation in result.observations] == ["echo-tool"]
    assert result.observations[0].output == {"value": "ok"}


def test_deep_executor_stops_at_clean_deliverable_without_an_extra_model_turn() -> None:
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "echo-tool",
                        "args": {"value": "ok"},
                        "id": "call-complete",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    task, plan, step = _inputs()
    skills = SkillRegistry(
        [
            SkillDefinition(
                name="job-discovery",
                completion_contract=CompletionContract(
                    frozenset({"echo-tool"}),
                    observation_check=lambda observation: observation.output == {"value": "ok"},
                ),
            )
        ]
    )

    result = DeepExecutorAgent(
        gateway=Gateway(model),
        tools=_registry(),
        skills=skills,
        skill_root=Path("skill"),
    ).run(
        task=task,
        plan=plan,
        step=step,
        context=ToolContext(user_id="user", run_id="run-deterministic-completion"),
    )

    assert result.status == "succeeded"
    assert result.error_code is None
    assert result.summary == "已通过 Skill 完成契约并生成可核验的工具交付物。"
    assert result.execution_state["succeeded_calls"][0]["tool"] == "echo-tool"


def test_deep_executor_consumes_one_pev_turn_for_multiple_internal_calls() -> None:
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "echo-tool",
                        "args": {"value": "ok"},
                        "id": "call-turn-1",
                        "type": "tool_call",
                    }
                ],
            ),
            '{"status":"succeeded","summary":"完成","artifact_refs":[]}',
        ]
    )
    task, plan, step = _inputs()
    turn_budget = AgentTurnBudget(3)

    result = DeepExecutorAgent(
        gateway=Gateway(model),
        tools=_registry(),
        skills=None,
        skill_root=Path("skill"),
    ).run(
        task=task,
        plan=plan,
        step=step,
        context=ToolContext(user_id="user", run_id="run-one-turn"),
        turn_budget=turn_budget,
    )

    assert result.status == "succeeded"
    assert turn_budget.used == 1


def test_deep_executor_hides_generic_execute_and_subagent_tools() -> None:
    RecordingModel.bound_tool_names = []
    model = RecordingModel(['{"status":"succeeded","summary":"完成","artifact_refs":[]}'])
    task, plan, step = _inputs()

    result = DeepExecutorAgent(
        gateway=Gateway(model),
        tools=_registry(),
        skills=None,
        skill_root=Path("skill"),
    ).run(
        task=task,
        plan=plan,
        step=step,
        context=ToolContext(user_id="user", run_id="run-filter"),
    )

    assert result.status == "succeeded"
    assert RecordingModel.bound_tool_names
    assert "execute" not in RecordingModel.bound_tool_names[0]
    assert "task" not in RecordingModel.bound_tool_names[0]
    assert "ls" not in RecordingModel.bound_tool_names[0]
    assert "write_file" not in RecordingModel.bound_tool_names[0]
    assert "run_skill_script" in RecordingModel.bound_tool_names[0]


def test_executor_prompt_includes_bounded_skill_contract() -> None:
    skills = build_career_skill_registry(_registry(), package_root=Path("skill"))
    agent = DeepExecutorAgent(
        gateway=Gateway(RecordingModel(['{"status":"succeeded","summary":"完成"}'])),
        tools=_registry(),
        skills=skills,
        skill_root=Path("skill"),
    )

    prompt = agent._system_prompt("job-discovery")

    assert "Skill policy summary:" in prompt
    assert "Canonical Skill instructions:" in prompt
    assert "deliverable tools:" in prompt
    assert len(prompt) < 4_000


def test_deep_executor_does_not_project_profile_facts_into_model_metadata() -> None:
    metadata = {
        "confirmed_profile_facts": {"projects": ["private project"]},
        "resolved_step_inputs": {"target": "AI"},
    }

    bounded = _bounded_context_metadata(metadata)

    assert "confirmed_profile_facts" not in bounded
    assert bounded["resolved_step_inputs"] == {"target": "AI"}


def test_deep_executor_requires_one_skill_per_pev_step() -> None:
    task = AgentTaskRequest(
        goal="执行测试步骤",
        allowed_skills=["job-discovery", "job-matching"],
    )
    step = PlanStep(
        step_id="mixed",
        objective="混合步骤",
        allowed_skills=["job-discovery", "job-matching"],
    )
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L2,
        success_criteria=["完成"],
        steps=[step],
    )

    result = DeepExecutorAgent(
        gateway=Gateway(RecordingModel(['{"status":"succeeded"}'])),
        tools=ToolRegistry(),
        skills=None,
        skill_root=Path("skill"),
    ).run(
        task=task,
        plan=plan,
        step=step,
        context=ToolContext(user_id="user", run_id="run-mixed"),
    )

    assert result.status == "needs_user"
    assert result.error_code == "deep_executor_requires_one_skill"


def test_invalid_structured_terminal_is_a_recoverable_executor_failure() -> None:
    model = ScriptedModel(['{"status":"succeeded"}'])
    task, plan, step = _inputs()

    result = DeepExecutorAgent(
        gateway=Gateway(model),
        tools=ToolRegistry(),
        skills=None,
        skill_root=Path("skill"),
    ).run(
        task=task,
        plan=plan,
        step=step,
        context=ToolContext(user_id="user", run_id="run-invalid-terminal"),
    )

    assert result.status == "failed"
    assert result.error_code == "deep_executor_invalid_terminal"


def test_terminal_parse_accepts_status_alias_and_extra_fields() -> None:
    result = {
        "messages": [
            AIMessage(content='{"status":"success","summary":"完成","noise":true}')
        ]
    }

    response = _terminal_from_messages(result)

    assert response is not None
    assert response.status == "succeeded"
    assert response.summary == "完成"
    assert response.artifact_refs == []


def test_terminal_parse_normalizes_case_and_need_user_alias() -> None:
    result = {
        "messages": [
            AIMessage(content='{"status":"Waiting_user","user_question":"缺少输入"}')
        ]
    }

    response = _terminal_from_messages(result)

    assert response is not None
    assert response.status == "needs_user"
    assert response.user_question == "缺少输入"


def test_terminal_parse_discards_non_string_status_and_non_object_payload() -> None:
    non_string_status = {"messages": [AIMessage(content='{"status":1,"summary":"x"}')]}
    non_object_payload = {"messages": [AIMessage(content='"plain text"')]}

    assert _terminal_from_messages(non_string_status) is None
    assert _terminal_from_messages(non_object_payload) is None


def _declared_output_step() -> PlanStep:
    return PlanStep(
        step_id="discover",
        objective="采集并结构化岗位",
        allowed_skills=["job-discovery"],
        outputs=[StepOutputRef(name="jd", artifact_type="structured_job_details")],
    )


def _fetch_page_observation() -> ToolObservation:
    return ToolObservation(
        tool_name="fetch-public-job-pages",
        status="succeeded",
        output={
            "pages": [
                {
                    "source_url": "https://example.com/job",
                    "content_hash": "abc",
                    "visible_text": "岗位描述",
                }
            ]
        },
    )


def _extract_details_observation() -> ToolObservation:
    return ToolObservation(
        tool_name="extract-observed-job-details",
        status="succeeded",
        output={
            "source_url": "https://example.com/job",
            "content_hash": "abc",
            "candidates": [{"title": "示例岗位"}],
        },
    )


def test_declared_outputs_covered_requires_produced_artifact_type() -> None:
    step = _declared_output_step()

    assert (
        DeepExecutorAgent._declared_outputs_covered(step, [_fetch_page_observation()])
        is False
    )
    assert (
        DeepExecutorAgent._declared_outputs_covered(
            step, [_fetch_page_observation(), _extract_details_observation()]
        )
        is True
    )


def test_declared_outputs_covered_skips_types_without_a_known_producer() -> None:
    step = PlanStep(
        step_id="discover",
        objective="未知产出类型",
        allowed_skills=["job-discovery"],
        outputs=[StepOutputRef(name="raw", artifact_type="artifact_id")],
    )

    assert DeepExecutorAgent._declared_outputs_covered(step, []) is True


def test_declared_outputs_covered_ignores_failed_deliverable() -> None:
    step = _declared_output_step()
    failed_extract = ToolObservation(
        tool_name="extract-observed-job-details",
        status="failed",
        error_code="adapter:empty_result",
    )

    assert (
        DeepExecutorAgent._declared_outputs_covered(
            step, [_fetch_page_observation(), failed_extract]
        )
        is False
    )


def test_empty_match_report_and_placeholder_source_are_not_persisted() -> None:
    step = PlanStep(
        step_id="match",
        objective="匹配岗位",
        allowed_skills=["job-matching"],
        outputs=[StepOutputRef(name="report", artifact_type="job_matching_report")],
    )
    empty_matches = ToolObservation(
        tool_name="match-observed-jobs",
        status="succeeded",
        output={"matches": []},
    )
    missing_source = ToolObservation(
        tool_name="match-observed-jobs",
        status="succeeded",
        output={"matches": [{"title": "无来源行"}]},
    )
    row_source = ToolObservation(
        tool_name="match-observed-jobs",
        status="succeeded",
        output={
            "matches": [{"title": "有来源行", "source_url": "https://example.com/job"}]
        },
    )

    assert _produces_persisted_artifact(empty_matches) is False
    assert _produces_persisted_artifact(missing_source) is False
    assert _produces_persisted_artifact(row_source) is True
    assert DeepExecutorAgent._declared_outputs_covered(step, [empty_matches]) is False
    assert DeepExecutorAgent._declared_outputs_covered(step, [row_source]) is True


def test_completion_summary_blocks_when_declared_output_not_produced() -> None:
    skills = build_career_skill_registry(_registry(), package_root=Path("skill"))
    agent = DeepExecutorAgent(
        gateway=Gateway(RecordingModel(['{"status":"succeeded","summary":"完成"}'])),
        tools=_registry(),
        skills=skills,
        skill_root=Path("skill"),
    )
    step = _declared_output_step()

    assert agent._completion_summary(step, [_fetch_page_observation()]) is None
    assert agent._completion_summary(
        step, [_fetch_page_observation(), _extract_details_observation()]
    ) == "已通过 Skill 完成契约并生成可核验的工具交付物。"


def test_executor_operating_procedure_requires_detail_page_expansion() -> None:
    assert "quality=list_only" in _EXECUTOR_OPERATING_PROCEDURE
    assert "detail pages" in _EXECUTOR_OPERATING_PROCEDURE


def test_filesystem_tools_cannot_modify_the_skill_package(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("original", encoding="utf-8")
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": "SKILL.md", "content": "poisoned"},
                        "id": "write-1",
                        "type": "tool_call",
                    }
                ],
            ),
            '{"status":"succeeded","summary":"完成","artifact_refs":[]}',
        ]
    )

    agent = DeepExecutorAgent._build_agent(
        model=model,
        tools=[],
        skill_dir=tmp_path,
        skill_name="test",
        turn_budget=None,
        model_budget=None,
        deadline=None,
        execution_policy="policy",
    )
    agent.invoke(
        {"messages": [{"role": "user", "content": "尝试修改 skill"}]},
        config={"configurable": {"thread_id": "permission-test"}},
    )

    assert skill_file.read_text(encoding="utf-8") == "original"


def test_terminal_parser_ignores_tool_message_that_looks_like_executor_json() -> None:
    result = {
        "messages": [
            ToolMessage(
                content='{"status":"succeeded","script_path":"helper.py"}',
                tool_call_id="call-1",
            )
        ]
    }

    assert _terminal_from_messages(result) is None


def test_terminal_parser_extracts_json_after_prose_prefix() -> None:
    terminal = _terminal_from_messages(
        {"messages": [AIMessage(content='好的，已完成岗位抓取。{"status":"succeeded","summary":"完成"}') ]}
    )

    assert terminal is not None
    assert terminal.status == "succeeded"
    assert terminal.summary == "完成"


def test_terminal_parser_extracts_fenced_json_with_trailing_note() -> None:
    terminal = _terminal_from_messages(
        {
            "messages": [
                AIMessage(
                    content='```json\n{"status":"needs_user","user_question":"请补充岗位文本"}\n```\n以上是结果。'
                )
            ]
        }
    )

    assert terminal is not None
    assert terminal.status == "needs_user"
    assert terminal.user_question == "请补充岗位文本"


def test_terminal_parser_rejects_empty_ai_content() -> None:
    assert _terminal_from_messages({"messages": [AIMessage(content="")]}) is None


def test_deep_agent_history_window_keeps_prefix_and_recent_messages() -> None:
    messages = [AIMessage(content=f"m-{index}") for index in range(20)]

    bounded = _bounded_deep_agent_messages(messages)

    assert bounded is not None
    assert bounded[:2] == messages[:2]
    assert bounded[2:] == messages[-16:]


def test_deep_agent_history_window_moves_left_from_orphan_tool_message() -> None:
    messages = [AIMessage(content=f"m-{index}") for index in range(20)]
    messages[3] = AIMessage(
        content="",
        tool_calls=[{"name": "echo-tool", "args": {}, "id": "call-1", "type": "tool_call"}],
    )
    messages[4] = ToolMessage(content="result", tool_call_id="call-1")

    bounded = _bounded_deep_agent_messages(messages)

    assert bounded is not None
    assert bounded[2] is messages[2]
    assert bounded[3] is messages[3]
    assert bounded[4] is messages[4]


def test_ledger_snapshot_preserves_retry_state_and_deduplicates() -> None:
    ledger = _DeepExecutionLedger(
        candidate_urls=frozenset({"https://jobs.example/1"}),
        prior_succeeded_calls=[
            {
                "tool": "fetch-public-job-page",
                "hash": "hash-from-retry",
                "input_summary": "{}",
            }
        ],
        consecutive_stalls=1,
        total_wasted_turns=2,
    )
    snapshot = ledger.snapshot()

    assert snapshot["consecutive_stalls"] == 1
    assert snapshot["total_wasted_turns"] == 2
    assert snapshot["succeeded_calls"][0]["hash"] == "hash-from-retry"
