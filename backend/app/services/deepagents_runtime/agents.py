"""Factory for the three deep agents (Planner / Executor / Verifier).

Each agent is a ``create_deep_agent`` graph with:
- no ``backend`` and an explicit tool-exclusion middleware, so the
  deepagents default file/shell tools (ls/read_file/write_file/edit_file/
  glob/grep/execute) are never offered — the only execution channel is the
  whitelisted skill wrappers (security: skill scripts only, spec §4.2);
- ``subagents=None`` and ``task`` excluded, so no delegation tool is ever
  offered (deepagents 0.6.12 auto-attaches a general-purpose subagent even
  with ``subagents=None``, which would expose ``task`` unless excluded);
- the turn-budget middleware, counting every model call against the
  per-run budget injected via ``current_budgets``.
"""

from __future__ import annotations

from typing import Any, Sequence

from deepagents import create_deep_agent
from pydantic import BaseModel, Field

from backend.app.domain.agent_runtime import VerificationDecision
from backend.app.services.agent_runtime.schemas import ExecutionPlan
from backend.app.services.deepagents_runtime.middleware import (
    ToolExclusionMiddleware,
    TurnBudgetMiddleware,
)

# deepagents default tools that must never reach the model. ``write_todos``
# stays (todo discipline); ``task`` would require subagents we never pass.
_DISABLED_TOOLS = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute", "task"}
)

PLANNER_PROMPT = """你是求职助手 PEV 运行时的规划者。读取用户目标、已确认画像事实与
已观察证据，输出严格满足 ExecutionPlan schema 的执行计划：
- task 字段必须原样回显输入（goal / allowed_skills / context / budget）；
- 每一步 allowed_skills 只能包含恰好一个 skill；
- 计划只能使用任务允许的 skill；
- complexity 与 success_criteria 必须填写。
只能输出合法 JSON。"""

EXECUTOR_PROMPT = """你是求职助手 PEV 运行时的执行者。你只负责当前这一步的目标：
- 只能调用当前 step 允许的 skill 工具；
- 优先使用工具产出证据（source_url + content_hash），不要凭记忆编造；
- 用工具完成证据收集后，用一句话总结你观察到的事实。"""

VERIFIER_PROMPT = """你是求职助手 PEV 运行时的验证者。你独立检查证据与产物，
绝不轻信执行者的总结：
- 证据不足 -> REPLAN；
- 证据可补 -> RETRY_EXECUTOR；
- 需要用户提供信息 -> NEED_USER；
- 目标不可达成 -> FAIL；
- 证据充分且满足成功标准 -> PASS。
输出 VerifierDecision JSON：decision 必须是 PASS/RETRY_EXECUTOR/REPLAN/
NEED_USER/FAIL 之一，rationale 简述依据。"""


class VerifierDecision(BaseModel):
    """Structured Verifier outcome consumed by the harness router."""

    model_config = {"extra": "forbid"}

    decision: VerificationDecision
    rationale: str = Field(min_length=1, max_length=4_000)


def build_agent(
    *,
    model: Any,
    name: str,
    tools: Sequence[Any] | None = None,
    system_prompt: str | None = None,
    response_format: Any = None,
    checkpointer: Any = None,
) -> Any:
    """Build one deep agent with file/shell tools disabled and turn budget on."""
    return create_deep_agent(
        model,
        tools=list(tools) if tools else None,
        system_prompt=system_prompt,
        middleware=(
            TurnBudgetMiddleware(),
            ToolExclusionMiddleware(excluded=_DISABLED_TOOLS),
        ),
        subagents=None,
        backend=None,
        permissions=[],
        response_format=response_format,
        checkpointer=checkpointer,
        name=name,
    )


def build_planner_agent(*, model: Any, checkpointer: Any = None) -> Any:
    """Planner: no tools, structured ExecutionPlan output."""
    return build_agent(
        model=model,
        name="planner",
        system_prompt=PLANNER_PROMPT,
        response_format=ExecutionPlan,
        checkpointer=checkpointer,
    )


def build_executor_agent(
    *, model: Any, tools: Sequence[Any], checkpointer: Any = None
) -> Any:
    """Executor: only the current step's skill-scoped tools."""
    return build_agent(
        model=model,
        name="executor",
        tools=list(tools),
        system_prompt=EXECUTOR_PROMPT,
        checkpointer=checkpointer,
    )


def build_verifier_agent(*, model: Any, checkpointer: Any = None) -> Any:
    """Verifier: no business tools, structured VerifierDecision output."""
    return build_agent(
        model=model,
        name="verifier",
        system_prompt=VERIFIER_PROMPT,
        response_format=VerifierDecision,
        checkpointer=checkpointer,
    )
