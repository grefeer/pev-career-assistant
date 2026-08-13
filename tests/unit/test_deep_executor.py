from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.deep_executor import (
    DeepExecutorAgent,
    _DeepExecutionLedger,
    _bounded_deep_agent_messages,
    _terminal_from_messages,
)
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    PlanStep,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget
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
    assert "run_skill_script" in RecordingModel.bound_tool_names[0]


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
    assert bounded[2] is messages[3]


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
