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
8. Executor prompt 不再依赖空的 `SkillDefinition.execution_policy`；改用有界 `SkillRegistry.prompt_policy`、完成契约和明确的单步工具流程。历史窗口只保留完整的 tool-call 对。

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
- run4 的 Q013/Q040/Q055 暴露的 orphan tool-call 400 已在历史窗口选择器增加成组边界保护；尚未重新跑公网验证。

## 2026-08-13 audit disposition

- `plan/audit-2026-08-13/findings_report.md` 的 8 项 P1 均已逐项复核并修复；没有把 DNS TOCTOU 的未证实理论风险误报成已解决。
- 额外修复了证据边界明确的 P2：cache hash 路径注入、浏览器非 Web scheme、deduplicate 输入/输出路径、OCR 联系方式提示、artifact visible_text 上限和 anti_crawl 敏感目录 ignore。
- 定向验证：98 passed；相关 runtime/model/deep-executor 测试与 ruff 通过。
- 正式测试目录验证：1615 passed、21 skipped；`tests/security/test_personal_assistant_default_path.py` 仍有 1 个与本轮无关的 FastAPI/Starlette `_IncludedRouter.path` 兼容性失败。
- 直接执行整仓 `pytest -q` 不可作为结果依据：仓库内 `temp/round*_worktrees` 会造成重复收集/import mismatch，skill 内 anti_crawl 测试还需要其包路径配置。
# 2026-08-13 P0/P1 收敛优化记录

## 当前事实

- 当前工作区已有大量未提交修改，本轮只做增量编辑，不回退或覆盖。
- `deep_executor.py` 已有 ledger 去重/停顿、观察投影、execution_state 和终态 trace，但工具成功后仍会把控制权交回模型。
- `runtime.py` 已对 `needs_user` 做部分 contract rescue，但 `deep_executor_invalid_response` 仍走 failed。
- `SkillRegistry.step_contract_met()` 已能判断工具交付是否满足 skill contract；`completion_evidence_gate()` 当前要求 summary 非空并拒绝任意 blocked evidence。
- Q028/Q057 有有效 `public_job_page` + `jd_complete`，适合做确定性收敛回归；Q034 只有 `list_only`，不能直接 rescue。

## 本轮实现边界

- 先实现可复用的 contract 判定与 DeepExecutor 工具后收敛。
- 终态解析失败仅在证据不满足时降级为 `waiting_user`；证据满足时由 harness 生成稳定 summary。
- planner 重试和 list-only 详情页策略分别补测试，避免把 Q034 误判成功。

## 已完成变更

- `deep_executor.py`：工具成功后检查 Skill completion contract；干净交付物触发内部完成控制流，避免继续模型调用；无效终态保留 trace 并降级为 `waiting_user`。
- `runtime.py`：增加 `deep_executor_invalid_response` + contract met + no blocked 的确定性 rescue。
- `planner_agent.py`：`ExecutionPlan` 校验失败最多回灌一次结构化错误后重试。
- `job_discovery.py` / `manifest.py`：结构化 JD 输出携带 `source_quality`；`list_only` 不再满足 job-discovery detail completion gate。
- adapter 解析分支现在也从原始页面证据继承 `source_quality`，不会绕过 `list_only` 门禁。
- invalid-terminal rescue 显式使用 Runtime 已持久化的 `observed_artifact_refs`，避免成功步骤丢失输出产物引用。
- 验证：定向执行器/planner/evidence/job-discovery 165 passed；runtime 相关集合 160 passed；`tests/unit` 1569 passed、7 skipped；改动范围 Ruff clean。
- 全仓 Ruff 仍有 47 个既有 `tests/manual` 诊断脚本问题；本轮未修改这些手工脚本，不能将全仓 Ruff 记为通过。

## 2026-08-13 Executor Skill/Tool run6 audit

- run6 Q057 的落库结果包含 2 个 `public_job_page` / `quality=jd_complete`，Executor 为 succeeded，Verifier 为 PASS，但运行时最终为 `waiting_user`，事件原因为 `verification_pass_rejected_by_contract`。
- 当前 `SkillRegistry.completion_evidence_gate()` 只消费 `ToolObservation.output`，而 `_persist_observed_evidence()` 另行生成 artifact；两者没有统一诊断对象，导致 artifact 与 gate 结论不一致时无法判断是缺字段、质量不合格、blocked 还是输出形状漂移。
- Q034 的实际 seed 为 `iguopin.com/job/list` / `job?`，4 个页面均为 `list_only`，不能直接 rescue；需要定位公开 DOM 的详情路由发现能力，而不是放宽契约。
- 评测 runner 只开启 Playwright fallback，未镜像生产环境的 `enable_public_api_adapters(settings.use_public_api_adapters)`。

## run8 复核

- 10 个固定样例均为 `waiting_user`。
- 7 个为 `external_blocked/anti_bot_challenge`，不应通过绕过站点安全页来提升成功数。
- Q017：`public_job_page` 和 `structured_job_details` 均已落库，页面质量为 `jd_complete`；步骤 1 仍为 `need_user`。
- Q028：学校就业页 `jd_complete` + 结构化详情已落库；步骤 2 在 Verifier PASS 后仍为 `need_user`，最终反馈是目标岗位相关性不足。
- Q034：国聘网页面均为 `list_only`，`search-public-job-pages` 被 `candidate_urls_already_supplied` 阻止后触发 `executor_stalled`；当前没有可信详情页 artifact。

## 当前假设

1. runtime 的 `needs_user` rescue 只使用 `step_contract_met(observations)`，未使用 `_persist_observed_evidence()` 返回的 trusted refs；这解释了“artifact 已存在但步骤失败”。
2. 非验证步骤的 `_completion_gate_rejected()` 也只使用 observations，应该同时接收本次已持久化 refs。
3. Q034 是否可修取决于公开 DOM 是否含 job-shaped detail links；若没有，保留 `list_only` 门禁和人工交接。
