# Notes: Agent 5 可观测性驱动的 4 轮闭环优化

## 已核对的现状事实

- `backend/app/db/models.py` 已有 `AgentRun`、`AgentPlan`、`AgentStep`、`AgentTurn`、`AgentEvent`、`AgentArtifact`；事件按 `(run_id, sequence)` 持久化，可通过 SSE 按游标回放。
- `backend/app/services/agent_runtime/runtime.py` 已写入 `run_started`、`plan_created`、`executor_tool_failed`、工具产物事件、Verifier 重试/重规划/通过、`run_failed` 等事件。
- `backend/app/services/agent_runtime/tool_registry.py` 已将异常归一为稳定码（如 `invalid_tool_input`、`invalid_tool_output`、`tool_skill_forbidden`、`unknown_tool`、`tool_execution_failed`），并对错误消息做脱敏和截断。
- 当前 `/api/metrics` 只暴露应用版本、依赖 readiness 和 MySQL/Redis/MinIO 状态，没有运行失败率、链路、工具调用、Verifier 决策或停机状态指标。
- `tests/question/eval_runner.py` 产出每题的状态、工具成功/失败计数、错误码、turn 数、token 汇总，但不保存失败调用原始 payload 或完整 `error_message`；`merge_round.py` 进一步压缩为按工具聚合。
- `docs/83-question-full-eval-report-2026-08-12.md` 记录：直接并行 83 题为 34 succeeded / 49 waiting_user / 0 failed；错峰并行为 30 / 52 / 1，且有 `public_page_content_insufficient`、`invalid_tool_input`、`target_evidence_not_found`、`model_request_failed` 等混合信号。
- 现有测试已覆盖预算、重复调用去重、三次 stall、Verifier RETRY/REPLAN、同构重规划保护、blocked evidence 降级和失败事件持久化；但没有“每 3 分钟巡检—>30 失败自动熔断—>失败链聚类—>自动回归”的端到端闭环。

## 推断的三类根因（需通过诊断链确认）

1. 工具输入/输出/证据指针的跨步骤契约断裂：已有 `invalid_tool_input`、`target_evidence_not_found`、`tool_skill_forbidden` 与下游产物断链证据。
2. 外部来源/渲染环境的可用性波动：`public_page_content_insufficient`、登录/反爬/SPA/微信等失败集中在抓取前段，不能与代码回归混为一类。
3. 闭环判定与上下文压力导致的无效重试：已有报告指出聚合级产物被 Verifier 拒收、`duplicate_tool_call` 后 stall，以及跨多轮 token 累计放大模型选错工具和指针丢失概率。

## 方案约束

- 4 轮优化的每一轮都必须先生成诊断快照，再允许修复进入下一轮。
- “失败超过 30”定义为当前评测批次累计的不可接受失败数 `hard_failures` > 30，或任一 3 分钟窗口新增硬失败 > 30；触发后停止新任务入场，并冻结当前批次，不把停止后的样本混入下一轮。
- 可恢复的 `waiting_user`、外部阻断和人工所需不直接计入硬失败；但单独计数为 `blocked_or_waiting`，防止通过安全降级掩盖可用性下降。
- 诊断链只保存脱敏的工具名、schema 字段路径摘要、错误码、artifact/content hash、角色、step、turn、revision、时间和输入指纹，不保存 token、完整简历、原始网页或敏感 payload。
