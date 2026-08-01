# 自适应三 Agent PEV 个人求职助手实施计划

> 设计依据：`docs/superpowers/specs/2026-08-01-personal-career-agent-adaptive-pev-design.md`  
> 工作方式：直接在 `master` 开发；每项行为先写失败测试，再写最小实现；不以 mock 冒充真实功能。  
> 目标：交付可演示、可审计、可降级、以真实工具为基础的 Planner–Executor–Verifier（PEV）个人求职助手，并完成真实 URL、真实简历和自然语言端到端验收。

## 0. 不变量与完成边界

- **Agent 定义不可弱化**：Planner、Executor、Verifier 都必须独立围绕目标感知信息、决策、调用受限工具、观察结果、在预算内调整下一动作。Harness 只做生命周期、鉴权、持久化、硬预算和工具 schema 校验，不决定业务答案或 Skill 选择。
- **降级不可伪造**：L1/L2/L3/L4 都先运行 Planner；复杂度只影响计划长度、工具预算和是否调用 Verifier。没有“规则路由后直接回答”的快捷路径。
- **真实性**：最终 URL 验收使用 7 个可公开访问的真实招聘 URL；PDF 验收使用用户给出的本地简历；模型不可用、登录/验证码、反爬和资料缺失必须如实返回 `NEED_USER` / `needs_manual_review`，不得编造岗位、简历事实、薪资或执行证据。
- **数据与安全**：MySQL 是 Agent 运行、计划、步骤和事件的权威来源；Redis 只可做临时缓存/限流。所有读写按 `user_id` 隔离；绝不自动投递、登录、绕过验证码或泄露简历原文/令牌。
- **覆盖率口径**：保留的生产 Python 包与前端 `src` 均纳入覆盖；删除或移出生产路径的 legacy 代码不进入该口径。CI/本地命令必须使用 `--cov-fail-under=100` 与 Vitest 100% 阈值，不能通过空文件、排除核心行或只跑子集获得数字。

## 1. 基线与测试基础

### 1.1 建立可重复的质量基线

**Files**

- Modify: `pyproject.toml` 或 `pytest.ini`（以现有测试配置实际位置为准）
- Modify: `frontend/vitest.config.ts`、`frontend/package.json`
- Add: `docs/runbooks/adaptive-pev-validation.md`

**步骤**

1. 运行完整 Python、前端测试、Ruff、前端 typecheck/build，记录当前失败与跳过原因；绝不把既存失败算作通过。
2. 增加明确的 coverage source 列表：保留的 `backend/app` 运行时包；排除仅由迁移框架加载的 Alembic、纯类型协议可用 `pragma: no cover` 的理由必须逐行记录。
3. 配置 pytest-cov branch coverage 与 `fail_under = 100`；配置 Vitest `all: true`、lines/functions/branches/statements 均为 100。
4. 把所有验收命令、外部环境门禁、真实 URL 记录格式写入 runbook，敏感变量只检查是否存在，不输出值。

**先写的失败测试**

- Add: `tests/unit/test_quality_config.py`，断言 coverage 配置包含保留生产源与 100 阈值。
- Add: `frontend/src/__tests__/coverageConfig.spec.ts`，从配置导入/检查并确保前端门禁非零阈值。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_quality_config.py -q
npm.cmd --prefix frontend run test -- coverageConfig
```

### 1.2 对现有功能划定保留、收敛、归档边界

**Files**

- Add: `docs/architecture/legacy-runtime-disposition.md`
- Modify: `README.md`

**步骤**

1. 逐模块列出老 `src/graph.py`、`src/checkpointing.py`、Job Discovery DeepAgents 入口与新的 PEV 入口；标明“生产保留 / adapter 封装 / archive / 删除”的明确条件。
2. 保留已实现的职位发现证据工具、匹配、简历定制、面试准备、投递跟踪及其数据模型；公司研究并入 `job-discovery`，投递跟踪维持确定性服务工具。
3. README 产品名称、系统图、启动方式改为个人求职助手，不再宣传职业平台或 LangGraph 为主运行时。

## 2. Agent Harness Foundation（阶段一）

### 2.1 定义纯领域契约与运行态 DTO

**Files**

- Add: `backend/app/domain/agent_runtime.py`
- Add: `backend/app/services/agent_runtime/schemas.py`
- Add: `backend/app/services/agent_runtime/__init__.py`
- Modify: `backend/app/config.py`

**设计**

- `AgentRole`: `planner`、`executor`、`verifier`；`RunStatus`: `queued/running/waiting_user/succeeded/failed/cancelled`；`StepStatus`: `planned/running/succeeded/failed/skipped`；`VerificationDecision`: `PASS/RETRY_EXECUTOR/REPLAN/NEED_USER/FAIL`。
- 领域层只含枚举、无副作用的 allowed-transition / budget 校验、错误码。服务 DTO 使用 Pydantic 定义 `AgentTaskRequest`、`ExecutionPlan`、`PlanStep`、`ToolCall`、`ToolObservation`、`VerificationResult`、`RunResult`。
- 配置新增 `agent_harness_enabled`、模型名、每层最大 turn/工具调用/重规划次数、L1–L4 阈值、事件最大 payload 大小；生产环境强制为正的硬上限。

**先写的失败测试**

- Add: `tests/unit/test_agent_runtime_domain.py`：非法状态迁移、非法 verifier decision、预算边界、序列化 round-trip。
- Add: `tests/unit/test_agent_runtime_schemas.py`：拒绝没有目标、空 Skill allowlist、越界预算、未知决策。
- Add: `tests/unit/test_agent_runtime_config.py`：默认值、环境覆盖、生产非法预算被拒绝。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_agent_runtime_domain.py tests/unit/test_agent_runtime_schemas.py tests/unit/test_agent_runtime_config.py -q
```

### 2.2 持久化 Agent 运行证据（MySQL）

**Files**

- Modify: `backend/app/db/models.py`
- Add: `alembic/versions/20260801_0017_agent_runtime_runs.py`
- Add: `backend/app/repositories/agent_runtime.py`
- Add: `tests/unit/test_agent_runtime_repository.py`
- Add: `tests/integration/test_agent_runtime_migration.py`

**设计**

- 新建 `AgentRun`（用户、原始目标、复杂度、状态、最终摘要、版本、预算）、`AgentPlan`（run、revision、结构化计划）、`AgentStep`（plan、顺序、Skill、输入摘要、状态、输出 artifact 引用）、`AgentTurn`（role、turn index、受控 prompt/decision 摘要、token/cost 统计）、`AgentEvent`（append-only sequence、类型、脱敏 JSON payload）。
- 所有 JSON 仅保存 schema 白名单与 artifact 引用，不保存完整 PDF、原始模型 prompt、API key 或 cookie。run/plan/step 均具外键、用户索引及稳定排序；重要状态更新与 event 在一个事务中写入。
- repository 只含 SQL/ORM 访问和 flush；所有 ownership、状态和决策规则留给 service。

**先写的失败测试**

1. 使用 SQLite fixture 验证 create run → plan revision → steps → turns/events 的稳定时间线。
2. 验证不同 `user_id` 不能通过 owner 查询取得 run；事件序号单调；取消/终态不可被覆盖。
3. migration 在干净数据库 upgrade/downgrade/re-upgrade 后模型可读写。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_agent_runtime_repository.py tests/integration/test_agent_runtime_migration.py -q
```

### 2.3 实现可替换模型网关与安全 Tool Registry

**Files**

- Add: `backend/app/services/agent_runtime/model_gateway.py`
- Add: `backend/app/services/agent_runtime/tool_registry.py`
- Add: `backend/app/services/agent_runtime/tool_context.py`
- Add: `tests/unit/test_agent_model_gateway.py`
- Add: `tests/unit/test_agent_tool_registry.py`

**设计**

- `ModelGateway` 协议只负责一次结构化 agent turn；OpenAI-compatible 实现与 `ScriptedModelGateway` 测试实现都返回经过 Pydantic 验证的 agent action。模型输出不是业务真相，必须由工具 observation 与 verifier 证据约束。
- `ToolRegistry` 显式注册 name、输入/输出 schema、角色 allowlist、用户上下文要求、timeout、是否 read/write、artifact policy。它拒绝未注册工具、越权工具、超预算调用、跨用户对象和不合 schema 的结果。
- 每次真实工具调用写 `tool_call_started/finished/failed` event，捕获时限和稳定错误码；不吞掉失败也不把异常文本泄露到 API。

**先写的失败测试**

- Executor 试图调用不存在、Planner-only、其他用户资源的工具均失败并产生日志事件。
- 工具超时、schema 失败、可恢复失败、artifact 截断均返回明确 observation。
- 模型网关拒绝非结构化/未知 action；scripted gateway 能驱动多 turn 测试而不模拟工具结果。

### 2.4 实现三个真正的 Agent 循环

**Files**

- Add: `backend/app/services/agent_runtime/planner_agent.py`
- Add: `backend/app/services/agent_runtime/executor_agent.py`
- Add: `backend/app/services/agent_runtime/verifier_agent.py`
- Add: `backend/app/services/agent_runtime/prompts.py`
- Add: `tests/unit/test_planner_agent.py`
- Add: `tests/unit/test_executor_agent.py`
- Add: `tests/unit/test_verifier_agent.py`

**设计**

- Planner 接收用户目标、经许可的上下文和 Skill manifest；必要时调用低风险 context 工具（例如读取偏好摘要或最近 artifact），输出带成功标准、依赖、允许 Skill 与验证等级的 `ExecutionPlan`。它对简单请求也产出单步 plan。
- Executor 对每个 step 观察前序 artifact 与工具 observation，自己在 allowlist 内选择 Skill/tool，循环执行、反思、换合法动作或请求澄清；不能由 harness 预先决定工具序列。每次 tool 结果都会进入它的下一 turn。
- Verifier 接收计划、实际 observation、artifact 和风险；它可调用证据/完整性/事实一致性工具，自主决策 `PASS/RETRY_EXECUTOR/REPLAN/NEED_USER/FAIL`，并在 retry/replan 中写机器可执行的缺口说明。它不是规则 if/else 或纯分类 LLM 节点。

**先写的失败测试（每个测试均使用真实 Registry + 独立 scripted 模型，不伪造 Agent 成果）**

1. Planner 从模糊目标主动读取偏好工具后形成有成功标准的计划；简单问题仍产生单步计划。
2. Executor 第一次选择的工具失败后观察失败，下一 turn 改用第二个合法工具并完成步骤；断言事件时间线中有两次 agent turn 和真实 tool observations。
3. Verifier 在证据不足时调用 evidence-check tool 并给出 `RETRY_EXECUTOR`；在 retry 成果充分后给 `PASS`；在计划假设失效时给 `REPLAN`。
4. 所有角色分别测试 turn/工具/重规划预算耗尽与 `NEED_USER`，确保没有无限循环。

### 2.5 编排服务、复杂度降级与 API

**Files**

- Add: `backend/app/services/agent_runtime/runtime.py`
- Add: `backend/app/services/agent_runtime/service.py`
- Add: `backend/app/api/agent_runtime_schemas.py`
- Add: `backend/app/api/routes/agent_runtime.py`
- Modify: `backend/app/api/dependencies.py`
- Modify: `backend/app/api/router.py`
- Modify: `backend/app/main.py`
- Add: `tests/unit/test_agent_runtime_service.py`
- Add: `tests/unit/test_agent_runtime_routes.py`
- Add: `tests/integration/test_agent_runtime_pev_flow.py`

**设计**

- `AgentRuntime.run()` 固定编排 `Planner → Executor(step loop) → conditional Verifier → retry/replan/user → terminal`，但只读取 Agent action 进行调度。复杂度 L1–L4 来源于 Planner 的结构化评估并由 hard cap 约束；L1 可跳过 Verifier，L2+ 的触发条件来自 Agent 标识的风险/不确定性并被记录。
- API：`POST /api/agent-runs` 创建/同步执行一个有界 run；`GET /api/agent-runs/{id}` 只返回 owner 白名单摘要；`GET /events` 返回脱敏 timeline；`POST /cancel` 仅 owner 可调用。路由只解析 DTO、调用 service、映射稳定错误，commit 在 route 层。
- main lifespan 注入 session factory、registry、gateway 和 runtime；启用 flag 为 false 时返回稳定 503，而不是暗中转 LangGraph。旧功能维持可用直到阶段五明确移除。

**先写的失败测试**

- 创建 run 的 owner 隔离、禁用 flag、非法 feature/Skill、取消、终态不可重跑、API 字段白名单。
- L1、L2、L3、L4 分别验证 plan 数量、verifier 是否触发、retry 路径与最终 event 时间线。
- 集成测试：一个发现→匹配→简历→面试的 scripted-agent but real-service-tool PEV 闭环，验证每个 handoff 都含 artifact ref 与 trace，且 tool 调用未由 harness 编排。

## 3. 收敛四个业务 Skill（阶段二）

### 3.1 建立统一 Skill manifest 与适配器

**Files**

- Add: `backend/app/services/career_skills/manifest.py`
- Add: `backend/app/services/career_skills/registry.py`
- Add: `backend/app/services/career_skills/context.py`
- Add: `tests/unit/test_career_skill_manifest.py`
- Add: `tests/unit/test_career_skill_registry.py`

**步骤**

1. 把 `skill/*/SKILL.md` 转为版本化、可机器读取的 manifest（inputs/outputs/工具/风险/成本/证据要求），保留 README 供人阅读。
2. 显式注册四个 Skill：`job-discovery`、`job-matching`、`resume-tailoring`、`career-planning`；将 application tracking 注册成 Executor 可调用的确定性工具而非第五个 Agent/Skill。
3. Skill adapter 的输入为 user-scoped context + artifact refs，输出为可验证 artifact；禁止整个数据库行、未经脱敏的简历内容或任意 shell 命令进入模型。

### 3.2 `job-discovery`：真实 URL 到证据化完整 JD

**Files**

- Modify: `backend/app/services/job_discovery/skill_runtime.py`
- Add: `backend/app/services/career_skills/job_discovery.py`
- Add: `backend/app/services/job_discovery/tools/coverage.py`
- Add: `tests/unit/test_pev_job_discovery_skill.py`
- Add: `tests/integration/test_pev_job_discovery_live.py`（显式 live gate）

**步骤**

1. 复用 URL triage、public fetch/Playwright、ReadGZH、JSON evidence、JD extraction、normalization/dedupe；将 DeepAgents 调度替换为 PEV adapter，不破坏公开站点、不尝试登录或反爬绕过。
2. 输出 `JobEvidence`（source URL、抓取时间、来源片段/哈希、公司/岗位/地点/性质/发布时间/截止/薪资、职责/要求、证据置信度）；字段缺失必须标为缺失。
3. 将公司研究并入该 Skill 的可选后续 step，失败时保留岗位结果并向 Verifier 提供 evidence gap。

**先写的失败测试**

- 合法公开 HTML/JSON/WeChat fixture 产出完整字段；重复 URL 与不同追踪参数被合并；登录/CAPTCHA 返回 manual review。
- Executor 因页面不含 JD 而选择列表页或 JSON endpoint 后补齐信息；coverage 工具发现分页未覆盖则 Verifier 触发 retry/replan。

### 3.3 `job-matching`：证据化个性化排序与 gap

**Files**

- Add: `backend/app/services/career_skills/job_matching.py`
- Modify: `backend/app/services/match_service.py`（仅在实际接口存在时）
- Add: `tests/unit/test_pev_job_matching_skill.py`

**步骤**

1. 以 confirmed profile facts、用户偏好、完整 JD evidence 为输入，复用现有 MatchReport，明确区分事实匹配、偏好匹配、未知项与推断。
2. 评分必须可解释：岗位相关性、薪资/地点/企业偏好、技能契合、缺口、证据 freshness；不使用“模型说高薪”代替页面证据。
3. 把推荐理由和不推荐理由形成 artifact，供 Verifier 复核。

### 3.4 `resume-tailoring`：事实约束的简历差异稿

**Files**

- Modify: `backend/app/services/resume_tailoring/runtime.py`
- Add: `backend/app/services/career_skills/resume_tailoring.py`
- Add: `tests/unit/test_pev_resume_tailoring_skill.py`
- Add: `tests/integration/test_pdf_resume_tailoring.py`（本地 PDF 可用时 gate）

**步骤**

1. 只从 confirmed profile/PDF 解析事实与目标 JD 生成 structured diff；任何无证据的量化、项目、技术、获奖都必须拒绝或标为“待用户确认”。
2. adapter 调用已有 generator/validator，产出原文片段、建议改写、理由、引用事实、风险与需确认项；保留 ResumeDraft 版本审计。
3. Verifier 用事实一致性工具逐条检查，发现虚构事实则 `RETRY_EXECUTOR` 或 `NEED_USER`。

### 3.5 `career-planning`：求职计划与面试准备

**Files**

- Modify: `backend/app/services/interview_prep/runtime.py`
- Add: `backend/app/services/career_skills/career_planning.py`
- Add: `tests/unit/test_pev_career_planning_skill.py`

**步骤**

1. 合并企业研究、投递节奏、能力缺口计划、面试题/讲点；不新增独立 company/interview Agent。
2. 计划的每个任务必须映射到 JD 要求或简历缺口，标注优先级、预估时间、验证产出；面试建议引用真实 JD 和候选人事实。
3. 对不能从公开证据确认的企业信息写出不确定性，不杜撰薪酬/面试流程。

## 4. 个人求职助手产品闭环（阶段三）

### 4.1 记忆、偏好与 Artifact 生命周期

**Files**

- Modify: `backend/app/services/personalized_discovery/*`
- Add: `backend/app/services/agent_runtime/context_builder.py`
- Add: `tests/unit/test_agent_context_builder.py`
- Add: `tests/integration/test_personal_career_memory_flow.py`

**步骤**

1. context builder 按“稳定资料 → 近期偏好/行为 → 当前会话 → artifact 摘要”分层、按 token/大小预算截取并记录 provenance。
2. 用户可查看和纠正偏好、确认 profile facts、批准简历版本；Agent 只能读已授权层。
3. artifact 统一支持访问控制、引用计数/retention、下载时敏感字段保护。

### 4.2 前端工作台与 HITL

**Files**

- Add: `frontend/src/features/agent-runs/AgentRunWorkspace.vue`
- Add: `frontend/src/features/agent-runs/agentRunsApi.ts`
- Add: `frontend/src/features/agent-runs/agentRunTypes.ts`
- Add: `frontend/src/features/agent-runs/*.spec.ts`
- Modify: `frontend/src/router/*`、`frontend/src/App.vue`（按实际结构）

**步骤**

1. 一个输入框覆盖“找岗位 → 全部 JD → 推荐 → 改简历 → 复习建议”主链，显示 plan、当前 Agent、工具证据、Verifier 决策、最终 artifact。
2. 用户可在 `NEED_USER` 时补充偏好/事实，在简历 diff 前确认；前端绝不声称 Agent 已投递。
3. 组件测试覆盖 loading/error/retry/replan/need-user/owner-not-found 的所有 UI 分支；通过真实 API contract test 而非只 snapshot。

### 4.3 用户流 API 集成

**Files**

- Modify: `backend/app/api/routes/agent_runtime.py`
- Add: `tests/contract/test_agent_runtime_api.py`
- Add: `tests/integration/test_personal_career_assistant_flow.py`

**验收场景**

- “帮我找近三天 AI 应用开发岗位”会产生计划、真实 discovery artifact、岗位列表和证据链接。
- 用户选择岗位后，matching→resume-tailoring→career-planning 在同一 run/child run 中引用前一 artifact。
- 陌生用户、过期 artifact、未确认事实、失败的外部 URL 均返回明确状态，不泄漏其他人的 run。

## 5. 迁移、归档与清理（阶段四）

**Files**

- Modify: `backend/app/main.py`
- Modify: `backend/app/config.py`
- Move/Delete only after replacement tests pass: `src/graph.py`, `src/checkpointing.py`，及仅服务旧生产路径的 DeepAgents supervisor wiring
- Add: `docs/architecture/langgraph-baseline.md`
- Modify: `requirements.txt` / lock files
- Add: `tests/security/test_no_legacy_langgraph_production_path.py`

**步骤**

1. 在新 PEV 的 job-discovery/matching/resume/career-planning 闭环通过前，旧路径保持 feature-flag rollback；记录其测试与对照结果。
2. 成功后把 LangGraph/DeepAgents 代码移入 docs/archive 或删除生产导入，保留输入/输出 fixtures 和对照指标，不再在 `main.py` 初始化 checkpointer/graph。
3. 移除不再使用的依赖，所有 import、Docker build、配置样例、README、runbook 和测试一起更新。禁止只把调用藏到未测试分支来声称已移除。
4. 运行静态搜索，确认生产代码没有 `langgraph`、`deepagents` 或旧 supervisor runtime import；如果依赖必须保留给 archive，则 archive 不可被 API/main import。

## 6. 验证与真实验收（阶段五，最后执行）

### 6.1 完整自动化门禁

1. `ruff check backend src tests scripts`。
2. 完整 Python 测试与 branch coverage 100%，包含迁移 round-trip、API contract、integration，外部需要的测试明确配置安全隔离库后运行。
3. `npm ci`、Vitest coverage 100%、typecheck、production build。
4. Docker migration、`/api/health/live`、`/api/health/ready`、前端首页、注册/登录、Agent run 创建的 smoke test。
5. `git diff --check`、依赖安全检查；记录 commit SHA、命令、通过/skip/环境限制。任何未跑或失败门禁都不能称“开发完成”。

### 6.2 7 URL 偏好/职位提取测试

1. 在测试前从公开互联网选择 **7 个真实、仍可访问且来源不同** 的招聘 URL（央国企与私企可混合），每个 URL 的页面或官方 JSON 能提供岗位证据；记录访问时间与公开链接。
2. 使用真实 PEV API/CLI，以用户偏好“AI 应用开发、Agent 开发”运行，逐 URL 保存原始请求、Planner plan、Executor 真实工具事件、Verifier 决策和完整 JD artifact。
3. 每条 JD 至少验证公司、岗位名称、类别、地点（若有）、发布时间/截至（若有）、职责、要求、薪资（仅页面明示时）、来源链接和证据片段。URL 被登录/反爬阻断时如实列为阻断，补选公开 URL 达到 7 条成功提取，不能伪造。
4. 输出表格：URL、企业性质、岗位、匹配标签、字段完整度、验证结论、阻断原因；验证跨 URL 去重、偏好排序和“全部符合 JD 先返回”的行为。

### 6.3 PDF 简历定制与面试建议模块验收

1. 先只检查并读取用户指定 PDF：`D:\Desktop\高硕谦-东北大学-控制科学与工程-硕士-男-简历 .pdf`；仅在本地受控测试中使用，不提交、打印或上传简历原文。
2. 从 7 条真实 JD 中选至少 3 条不同要求岗位，运行真实 resume-tailoring adapter，生成可复核 diff；逐条人工/Verifier 检查其事实是否都来自 PDF/confirmed profile，零虚构。
3. 对同 3 条 JD 运行 career-planning，核对面试题、复习点和计划都能追溯到 JD 或已知候选人事实；输出“需要候选人补充”的项而非猜测。
4. 保存只含摘要/哈希/测试判定的验收记录；最终交付不复述敏感个人信息。

### 6.4 自然语言端到端验收

以真实运行输入：

> 统计最近 3 天的央国企（或私企）关于 AI 应用开发、Agent 开发岗位，先返回所有符合的 JDs，然后推荐一个最符合我、待遇比较好的岗位，根据岗位修改简历，并给出复习意见。

**必须逐项验收**

1. Planner 创建多步骤、带 freshness/企业性质/岗位关键词/完整列表优先的计划。
2. Executor 通过真实 job-discovery 工具收集并筛选公开证据，先输出全部符合 JD；无法核验“最近 3 天”时清楚标识日期证据不足。
3. job-matching 以偏好和简历事实推荐一个岗位；“待遇较好”只能基于公开薪资/福利证据或明确声明无可比数据，不能编造。
4. resume-tailoring 产出事实约束的 diff，career-planning 产出 JD 可追溯复习计划。
5. Verifier 对每一关键 artifact 独立调用验证工具，最终 timeline 可显示其 PASS/retry/replan 依据。
6. 截图/导出完整 UI trace，连同命令日志和脱敏 JSON artifact 路径写入验收报告。

## 7. 实施节奏、提交与复盘

1. 每个小节都遵循 **RED → 最小 GREEN → 重构 → 全相关测试**；先提交测试与实现的原子变更，再开始下一项。不得在测试之前一次性堆积大量 production code。
2. 每完成一个阶段，运行阶段全量测试并以 `git diff --check` 审查；仅在证据成功时更新本计划状态和设计的完成标准。
3. 最终只在“自动化 100% coverage、smoke、7 URL、PDF 三 JD、自然语言 E2E”均有可复现证据后，声明完成。若外部站点变化，保留真实错误、替换来源并重新跑，不以 fixture 代替。
