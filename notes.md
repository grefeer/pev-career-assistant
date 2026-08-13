# Notes: Canonical Skill Migration Audit

## Scope
- Repository: `D:\Program Files\JetBrains\PyCharm Community Edition 2024.2.2\proj\langgraph-multi-agent-career-assistant-main`
- Focus: root-level Skill package, `backend/app/services/career_skills`, Agent Core, manifests, tool and I/O contracts.
- Constraint: read-only code audit; no code changes.

## Sources

### Repository files
- `skill/`: 7 directories with `SKILL.md`: `application-tracking`, `career-planning`, `company-research`, `interview-prep`, `job-discovery`, `job-matching`, `resume-tailoring`.
- `backend/app/services/career_skills/`: 14 Python modules; `registry.py` currently registers 13 tools; `manifest.py` exposes only 4 PEV-selectable skills.
- `backend/app/services/agent_runtime/`: generic PEV harness files plus remaining career-specific fallback logic in `schemas.py`, `executor_agent.py`, `runtime.py`, `error_policy.py`, `skill_definition.py`, and `evidence_gate.py`.
- `backend/app/services/deepagents_runtime/`: adapters and workflow code directly import `career_skills`; the job-discovery graph duplicates package workflow behavior.
- `docs/pev-agent-architecture.zh-CN.md`, `CLAUDE.md`, `docs/agent-runtime-skill-decoupling.md`, and `docs/superpowers/specs/2026-08-07-deepagents-runtime-design.md`.

## Synthesized Findings

### Ownership boundary
- The root packages currently own human-readable policy, references, scripts, evals, and some CLI execution. The backend owns the PEV-callable Pydantic contracts, tool registry, most deterministic business logic, artifact mapping, and error policy.
- `build_career_skill_registry()` discovers package text but still hard-codes the four manifest entries and deliverable tool sets. Package instructions are prompt input, not an executable source of truth.
- `build_career_tool_registry()` is the current executable source. `deepagents_runtime/tools/adapters.py` imports it directly; `agent_runtime/evidence_gate.py` has a legacy fallback import to `career_skills.manifest`.
- Agent Core still contains domain knowledge: already-collected-job markers and `job-discovery` assumptions in `schemas.py`; candidate URL/search gating and job-specific handoff text in `executor_agent.py`; JD projection, structured-candidate hydration, and hard-coded artifact-type mapping in `runtime.py`; OCR/WeChat and career error classifications in `error_policy.py` / `skill_definition.py`.

### Migration design
- Make each package self-describing with a machine-readable manifest, JSON/Pydantic-compatible input/output schemas, dependency declarations, tool adapters, completion/verification policy, artifact declarations, and error taxonomy.
- Keep the PEV runtime generic: it consumes a `SkillPackage`/`SkillDefinition` and opaque evidence/artifact envelopes; it does not name a career skill, job field, candidate URL, JD type, or career-specific error.
- Reduce `backend/app/services/career_skills/` to a compatibility host/loader, then remove it after parity. Business implementations move under the owning `skill/<name>/` package.

### Compatibility and verification
- Preserve public API DTOs, tool names, artifact types, stable error codes, owner scoping, and human-gated behavior during migration.
- Use a root-composition feature flag and explicit legacy shims; never silently dual-execute or fall back inside Agent Core.
- Verify package discovery, manifest/schema validation, import-boundary absence, focused unit tests, full unit/ruff gates, and canonical-vs-legacy fixture parity before deleting shims.

## 2026-08-12 四轮续跑结果

The six-agent design review converged on typed cross-step artifact ports,
evidence-quality routing, compiled Skill contracts, and observable live
evaluation. The five proposals and the separate evaluator are recorded in the
root proposal markdown files.

The stopped round-4 live evaluation is in
`tests/question/eval_results/round4_live_20260812_contracts_v2/`. It covered 47
of 83 documents before the circuit breaker fired:

| status | count |
|---|---:|
| succeeded | 4 |
| waiting_user | 40 |
| failed | 3 |

The 40 waiting cases clustered into 28 compliant anti-bot/access-control
handoffs, 8 missing/insufficient source cases, and 2 duplicate-call stalls.
The three failed cases were Q143, Q115, and R006, all ending in
`replan_budget_exhausted`. Tool-level counts included 17
`sheet_rate_limited`, 5 `duplicate_tool_call`, 3 `anti_bot_challenge`, and 1
`public_page_content_insufficient`.

Post-evaluation fixes reject recruiting-site root homepages from search,
classify long navigation pages without JD sections as `list_only`, and carry
source page quality through structured extraction into matching. A reusable
monitor is available at `scripts/monitor_question_eval.ps1`; it stops when
success reaches 65 or failed/waiting_user exceeds 30.

Verification after the fixes: 154 focused unit tests passed and Ruff passed.
The 65-success threshold was not reached; this is an explicit incomplete
outcome, not a claim of full-evaluation success.
## 2026-08-12 第2轮 5+1 闭环

- 独立方案：Laplace（来源路由租约）、Dalton（Executor 进度账本/终止门控）、Mencius（JD Evidence Bundle）、Maxwell（Skill Contract Compiler）、Rawls（顶层 case 统计与失败轨迹）。
- 独立评估 Copernicus 取舍：本轮实现 B 的可控子集、E 全量口径、A 的最小有界来源路由；C/D 暂缓，避免重复已有 artifact quality/port 修复或进行跨层迁移。
- 改动：`tests/question/eval_policy.py`；`tests/question/eval_runner.py`；`scripts/monitor_question_eval.ps1`；`backend/app/services/agent_runtime/executor_agent.py`；`backend/app/services/agent_runtime/runtime.py`；`backend/app/services/career_skills/job_discovery.py`；`tests/unit/test_eval_stop_policy.py`。
- 行为：顶层题目去重；chain link 不单独计数；`non_success = failed + waiting_user + unknown`；超过 30 停止；结果原子写入；输出 `failure_trace/root_cause`；跨链路不再把 matching report 当候选 URL；保留 structured candidates；公开搜索相同 query/超过两次路由返回 `route_already_consumed`；空搜索有 `search_empty` 终态并计入 no-progress；Executor decision state 增加 progress ledger；反爬域名和搜索租约跨 step/retry 传播。
- 单元验证：定向 PEV/来源/监控测试 `217 passed`；ruff/compileall 通过。
- 受控评测目录：`tests/question/eval_results/round2_live_20260812_progress_route_v2/`。停止时 36 个顶层 case：3 succeeded、31 waiting_user、2 failed，`non_success=33`，已停止 6 个仅匹配该目录的评测进程。
- 失败轨迹聚类：`model_or_verifier_decision` 23、`upstream_tool_failure` 8、`no_progress_duplicate` 3、`external_blocked` 2（初版分类低估了摘要中明确的反爬/人工交接，下一轮需把 summary 纳入分类）；错误码频次：`sheet_rate_limited` 9、`route_already_consumed` 4、`anti_bot_challenge` 2、`duplicate_tool_call` 2、`public_page_content_insufficient` 2、`candidate_urls_already_supplied` 1、`observed_evidence_not_found` 1。
- 结论：当前剩余低成功率主要不是单纯工具调用失败，而是外部反爬导致的合法 waiting_user，以及模型/Verifier 直接请求人工补充来源。可继续修复的是摘要根因分类、`observed_evidence_not_found` 的输入契约和 replan 终止；不能通过绕过反爬提高成功率。

## 2026-08-12 第3/4轮收尾与离线审计

- 第3轮实现了统一 `terminal.v1` 终态合同、结构化候选 EvidenceRef 解析、稳定的无效输入签名门禁、失败摘要脱敏；第4轮实现了成功结果审计、重复语义计划门禁、搜索结果/网页来源身份分离、Verifier `FAIL` 的人工交接合同。
- 用户要求停止在线测试，因此没有启动新的真实模型/网络评测；已有第4轮目录 `round4_live_20260812_contracts_v2` 离线统计为 32 个顶层 case：4 succeeded、25 waiting_user、3 failed，非成功 28。目录生成早于本轮成功审计和终态合同补丁，不能把它当作补丁后的新评测。
- 严格成功审计对第4轮已有 4 个 succeeded 全部判定为通过：均至少有 `public_job_page`、`jd_complete`、来源 URL、64 位 SHA-256、非空正文。第3轮的 2 个 succeeded 中，Q017 通过，Q133 只有结构化产出而没有完整页面，按新规则应改判为 `waiting_user`；这说明旧统计存在至少 1 个成功虚高。
- 第4轮非成功按首要原因归类（类别可重叠，按优先级只计一次）：16 external_blocked、7 sheet_rate_limited、1 no_progress_duplicate、1 replan_budget_exhausted、3 model/verifier_or_evidence_contract。重叠证据中 sheet 限流出现在 16 个 case，重复/无进展信号出现在 6 个 case。
- 工具层只记录到 3 次 `anti_bot_challenge`，但摘要明确写出反爬/访问控制的 case 有 16 个；因此仅按 `ToolObservation.error_code` 统计会严重低估外部阻断，必须同时保留终态合同和受控摘要分类。
- 第4轮工具调用频次：`sheet_rate_limited` 17 次、`duplicate_tool_call` 5 次、`anti_bot_challenge` 3 次；4 个成功案例中 Q081/R011 也各带有一次 sheet 限流，但已有足够网页证据，不应因单个辅助源失败而整体判失败。
- 全量单元测试：1538 passed, 1 warning；Ruff backend/tests/scripts 全通过；compileall 通过。新规则的定向回归为 132 passed。

## 2026-08-12 运行时问题一次性收敛补丁

- 来源状态：Executor 新增持久化 `unavailable_tools`。`sheet_rate_limited`、`sheet_call_failed`、`sheet_bridge_unavailable`、`route_already_consumed` 一旦触发，本轮不再因更换关键词重复访问同一来源；模型若再次选择已熔断来源，立即人工交接。
- 反爬降级：所有候选 URL 均死链或被访问阻断后，允许转公开搜索；搜索结果过滤本轮已阻断域名，避免“换了路由但仍回到同一反爬站点”。
- 无进展：Verifier RETRY 前比较稳定的证据指纹（来源 URL、content_hash、quality、candidate/source identity）；没有新增证据时直接 `no_progress_duplicate -> waiting_user`，不再反复执行 Executor/Planner。
- 重规划：超过 `max_replans` 改为带 `terminal.v1` 的可恢复 `waiting_user`，不再落成 `failed`；`FAIL` 和重复计划均停止循环。
- 成功契约：job-discovery 的 search/sheet 索引不能直接关闭步骤，必须产生完整公开页面或 source-bound structured JD；`list_only/js_shell/empty` 不得算成功。
- 这次修改后的验证：`tests/unit` 为 **1539 passed, 1 warning**；定向回归 86 passed；Ruff 全通过；compileall 通过。没有重新启动在线评测，因此反爬之外的成功率提升尚未用真实网络数据重新量化。
## 2026-08-12 第5轮 83案例全量在线评测

- 输出目录：\`tests/question/eval_results/round5_live_20260812_unified_state_v2/\`。
- 最终状态：**2 succeeded、81 waiting_user、0 failed、0 unknown**；83/83 个 manifest 顶层案例均有终态。严格成功审计通过：\`Q115\`、\`R040\`，成功率 2.4%。
- 首要原因：\`external_blocked\` 29、\`upstream_tool_failure\` 21、\`model_or_verifier_decision\` 15、\`model_output_invalid\` 11、\`budget_exhausted\` 3、\`contract_or_policy_error\` 3、\`no_progress_duplicate\` 1。
- 工具错误码出现次数：\`sheet_rate_limited\` 24、\`duplicate_tool_call\` 4、\`route_already_consumed\` 4、\`anti_bot_challenge\` 4。
- 结论：反爬/访问控制仍是最大单一根因，但来源限流、模型输出不可解析、Verifier/契约决策和预算问题仍独立造成大量 \`waiting_user\`。
## 2026-08-12 PEV 通用规则提示词补强

- 根因判断：第5轮的非外部失败并非都能靠 harness 兜底。现有提示词有“不要重复/不要伪造”等散点规则，但缺少统一的失败分类决策表，导致模型在 \`need_user\`、\`RETRY_EXECUTOR\`、\`REPLAN\`、\`complete\` 之间误选。
- 新增 \`backend/app/services/agent_runtime/prompt_rules.py\`：只包含业务无关的工具权限、证据等级、失败分流、预算、提问门槛、完成条件和 JSON 输出规则；不包含招聘来源、搜索关键词或任何 career Skill 流程。
- Planner 新增：有允许路径时不得过早提问；每步单 Skill；success criteria 必须可验证；重复失败路线不算重规划。
- Executor 新增：调用前检查权限/新证据；工具成功不等于步骤完成；稳定失败不原样重试；无终端阻断且尚无成功 observation 时，对过早 \`need_user\` 做一次纠偏。
- Verifier 新增：PASS/RETRY/REPLAN/NEED_USER/FAIL 的明确决策表；无新动作不得 RETRY；单个来源失败不得直接 REPLAN；已有完整 contract 不因辅助工具失败而否定。
- JSON 网关修复：格式修复重试加入角色级行为优先级，避免只强调 JSON 格式而没有纠正错误的行动选择。
- 验证：新增提示词/纠偏回归；定向 124 passed；完整 \`tests/unit\` **1543 passed, 1 warning**；Ruff 全通过；compileall 通过。
- 尚未重新执行 83 个 live 案例；下一次 live 评测需要与 \`round5_live_20260812_unified_state_v2\` 做同口径对比，重点观察 \`model_output_invalid\`、\`model_or_verifier_decision\`、\`budget_exhausted\` 和 \`sheet_rate_limited\`。

## 2026-08-12 第6轮全量评测与自适应修复

- 全量目录：`tests/question/eval_results/round6_live_20260812_prompt_harness_adaptive/`；按 manifest 顶层 ID 统计为 **1 succeeded、82 waiting_user、0 failed、0 unknown**。成功案例为 `Q017`。
- 原始终态中有 11 个 Planner `need_user` 被旧 runtime 标为 `invalid_model_response`；本轮已修复为 `need_user/model_or_verifier_decision`，以后统计不会再把合法人工交接误报为模型 JSON 解析失败。
- Planner decision state 现在显式提供 `available_executor_tools`，并在通用提示词中规定：只要存在 Executor 可执行路径，Planner 不得因自己的工具目录为空而直接提问。
- Executor 增加抓取语义去重：同一请求 URL 已成功出现时不再重复调用；进一步增加不依赖动态 content hash 的 route repetition guard，避免同一路由换 query/filter/page 造成假进展。
- 定向目录：`tests/question/eval_results/round6_targeted_after_prompt_harness/`；16/16 waiting_user。11 个主要是 Smartsheet `sheet_rate_limited`，1 个反爬，3 个模型/Verifier 或证据契约交接，1 个旧的 wall-clock 问题。
- 路由保护复测目录：`tests/question/eval_results/round6_route_guard_check/`；R020 从旧轮次的 48 次 fetch、328.3 秒、`wall_clock_budget_exhausted`，收敛到 12 次 fetch、94.2 秒、`no_progress_duplicate`。证据不足仍等待用户，符合契约。
- 代码回归：`tests/unit` **1547 passed, 1 warning**；Ruff 通过。全仓 `pytest -q` 会错误收集历史 `temp/round5_worktrees/*` 并产生 import mismatch，未将该环境污染误判为本轮代码回归失败。
- 结论：提示词修复消除了 Planner 误分类/过早提问的一类问题；Harness 修复解决了重复抓取的无界消耗。成功率仍被外部限流、反爬和证据契约主导，不能通过放宽契约把 waiting_user 虚报为 succeeded。
