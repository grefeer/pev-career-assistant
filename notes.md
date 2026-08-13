# DeepSeek / DeepAgents 修复记录

## 当前证据

- `tests/question/eval_results/deep_executor_nonchain_20260813_run3`：10/10 完成，0 succeeded、2 waiting_user、8 failed。
- 多个失败题在 `tool_calls=[]` 时耗尽 `agent_turn_budget`，说明 DeepAgents 内部模型调用被当成旧 Executor 的生命周期 turn。
- 当前生产 DeepExecutor 使用 `create_deep_agent(response_format=DeepExecutorResponse)`；对 DeepSeek 兼容模型可能走 ToolStrategy，结构化终态工具调用会阻止自然终止。
- `ModelCallBudget` 的预留在模型 handler 异常时没有显式取消，存在预留泄漏风险。
- Planner 使用 JSON mode，但首轮 system prompt 只要求“匹配 schema”，没有嵌入完整 JSON schema；默认模型输出上限为 4096。

## 本轮决策

1. DeepExecutor 不再把终态交给 DeepAgents 的 `response_format`；使用自然文本终态，由本地严格解析最后一条无 tool call 的 AIMessage。
2. DeepExecutor 关闭 DeepAgents progressive skill disclosure，直接注入 Skill execution policy 和有界业务工具目录；Python helper 仍只能通过 `run_skill_script` 执行。
3. 共享生命周期 turn 只在进入一个 DeepExecutor step 时消耗一次；DeepAgents 内部模型调用使用独立的 step call ceiling，并继续受到 ModelCallBudget 的物理请求/token上限约束。
4. 模型预算失败必须 cancel 未提交 reservation；GraphRecursionError 使用独立错误码。
5. Planner 首轮 JSON prompt 内嵌完整 schema，计划复杂度高时提高输出上限；解析失败日志保留安全指纹。
6. DeepAgents 模型请求只保留首段请求和最近消息窗口，避免完整 tool history 在每次调用中重复增长；终态 trace 记录内部模型调用数。
7. 终态解析共享平衡 JSON 对象提取器，兼容散文前缀、fenced JSON 和尾注；所有 DeepExecutor 异常终态统一进入 trace，并把错误码和内部调用数持久化到评测 turn。

## 未做

- 不修改 Verifier。
- 不恢复或引用已退役 runtime。
- 不重新启动10题公网评测，直到本轮单元/集成回归通过。

## 验证

- 定向：`tests/unit/test_deep_executor.py tests/unit/test_skill_script_runner.py tests/unit/test_agent_model_gateway.py tests/unit/test_agent_runtime_contracts.py`：84 passed。
- 相关全量：`tests/unit`：1557 passed、7 skipped，1 个既有 Starlette/httpx 弃用警告。
- ruff：agent runtime、question eval runner 和新增回归测试通过。
- `compileall`、`git diff --check`：通过。

## 当前边界

- 整仓 `pytest -q` 仍会把 `temp/round5_worktrees/*` 重复副本收集进来，触发既有 import mismatch；本次未修改或删除这些临时目录。
- 公网 10 题仍未重跑，待行为层 flip matrix 单独执行。
