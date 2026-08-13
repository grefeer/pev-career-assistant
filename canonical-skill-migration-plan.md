# Canonical Skill 包成为唯一业务执行来源：独立迁移方案

> 本文是方案 Agent 2 的独立方案。它解决的是“业务规则和执行实现到底由谁拥有”，不是单纯修复 PEV 运行时循环、预算或验证门。
> 本轮只做审计和设计，不修改 Python、前端、配置、数据库迁移或测试代码。

## 结论

应把 `skill/<name>/` 定义为职业业务的唯一发布单元：`SKILL.md` 负责可读政策，机器 manifest 负责依赖和权限，contracts 负责 I/O，package runtime/adapter 负责确定性业务执行，evals 负责行为回归。`backend/app/services/career_skills/` 只能保留短期兼容宿主和加载桥；Agent Core 只消费通用 Skill 契约，不再包含求职业务判断。

当前最大的结构性问题不是 Agent Core 的某个判断错误，而是业务源有两套：根目录 Skill 包有一套规则和脚本，`career_skills` 又有一套 Pydantic 工具、JD 解析、匹配、简历/规划逻辑。现有 `SkillDefinition` 仅把 `SKILL.md` 文本放入 prompt，不能阻止后端实现继续漂移。

## 1. 审计结果：现状边界

### 1.1 规模和职责

| 区域 | 当前事实 | 结论 |
|---|---|---|
| `skill/` | 7 个包；包含 `SKILL.md`、references、scripts、evals | 已经是最完整的业务语义来源，但不是统一的机器执行入口 |
| `backend/app/services/career_skills/` | 14 个 Python 模块，`registry.py` 注册 13 个工具 | 当前实际执行来源，持有输入/输出模型、工具 handler、错误和业务规则 |
| `backend/app/services/career_skills/manifest.py` | 只登记 `job-discovery`、`job-matching`、`resume-tailoring`、`career-planning` 4 个包 | manifest 与 Canonical 包清单不一致；`company-research`、`interview-prep`、`application-tracking` 不可被当前 PEV 选择 |
| `backend/app/services/agent_runtime/skill_package.py` | 只解析 `SKILL.md` frontmatter/body | 发现和 prompt 注入，不是 adapter/contract loader |
| `backend/app/services/deepagents_runtime/` | `tools/adapters.py` 直接导入 `career_skills.registry`；job-discovery graph 重编码包流程 | 第二条执行路径也没有以 Canonical 包为唯一来源 |

### 1.2 业务重复点

- `skill/job-discovery/` 已有抓取、分类、提取、校验、去重、增量和反爬/人工复核规则；`career_skills/job_discovery.py`、`career_sheets.py`、`classify_url.py`、`deduplicate_observed.py`、`validate_candidates.py`、`wechat.py` 又提供后端版本。
- `backend/app/services/job_discovery/` 仍持有 `jd_extraction.py`、`taxonomy.py`、`job_strength.py`、`skill_validator.py` 等求职知识；它们被 `career_skills` 调用，不属于通用基础设施。
- `skill/job-matching/SKILL.md` 声明“只匹配已观察岗位、缺失事实为 unverified”；`career_skills/job_matching.py` 同时实现关键词/地点/待遇评分。这是同一规则的两份来源。
- `skill/resume-tailoring/` 的 diff 操作、fact/evidence 引用约束与 `career_skills/resume_tailoring.py` 的 `highlight/reorder` 输出并非同一完整契约。
- `skill/career-planning/` 的 deadline/target evidence/skill gap 约束与 `career_skills/career_planning.py` 的 topic 规则存在并行实现。

### 1.3 Agent Core 仍携带的求职知识

以下内容必须迁出 `backend/app/services/agent_runtime/`，否则“唯一业务来源”不成立：

- `schemas.py`：`_ALREADY_COLLECTED_MARKERS`、`candidate_urls`、只允许 `job-discovery` 之外 deliverable 的规则。
- `executor_agent.py`：`search-public-job-pages` 特判、候选 URL 死链账本、`candidate_urls_already_supplied`、岗位页面/岗位产出中文 handoff 文案。
- `runtime.py`：JD/structured candidate 投影、`structured_job_details` 处理、`match-observed-jobs` / `build-resume-tailoring-brief` / `build-preparation-plan` 到 artifact type 的硬编码映射。
- `error_policy.py`、`skill_definition.py`、`evidence_gate.py`：OCR、WeChat、login/captcha/anti-bot 的职业来源分类，以及 `_legacy_registry()` 对 `career_skills.manifest` 的回退导入。
- `service.py`：`confirmed_profile_facts` 这一候选人领域字段及 profile repository 注入逻辑。通用 Agent Core 应接收不透明的 scoped private context；职业 profile 注入应在职业应用服务或 composition root 完成。

## 2. 目标架构

```text
skill/<name>/
  SKILL.md                 # 人可读业务政策和工作流
  manifest.yaml            # 机器可读 Skill/工具/依赖/权限契约
  contracts/*.json         # 稳定输入、输出、artifact JSON Schema
  runtime/                 # 确定性业务实现和 ToolAdapter
  scripts/                 # CLI/批处理入口；可由 adapter 复用
  references/ evals/       # 详细规则与行为回归
          |
          v
Generic SkillPackageLoader -> SkillCatalog -> ToolRegistry adapter view
          |
          v
Generic Planner / Executor / Verifier / PEV lifecycle harness
```

### 2.1 所有权规则

1. `SKILL.md` 是业务意图和人审规则的 canonical source；如果实现与文档冲突，先修包并更新 eval，再发布 adapter。
2. `manifest.yaml` 是唯一机器入口；禁止从 tool name、目录名或 prompt 文本推断 deliverable、依赖或权限。
3. `runtime/` 和 `scripts/` 是唯一业务执行实现；`backend` 不再复制业务算法。
4. Agent Core 只知道通用的 `SkillRef`、`ToolSpec`、`ContextRef`、`ArtifactRef`、`EvidenceEnvelope`、`CompletionResult`、预算和生命周期状态。
5. 包内可以声明 `blocked`、`needs_manual_review` 等结果，但 Core 不解释其职业含义，只按 manifest 声明的结果分类和恢复策略处理。

### 2.2 manifest 设计

每个包新增 `skill/<name>/manifest.yaml`，至少包含：

```yaml
name: job-matching
version: 2.0.0
instructions: SKILL.md
entrypoint: runtime.adapter:build_tools
context:
  - name: confirmed_profile_facts
    visibility: private
    required: true
dependencies:
  skills:
    - name: job-discovery
      artifacts: [structured_job_details]
tools:
  - name: match-observed-jobs
    roles: [executor, verifier]
    input_schema: contracts/match-input.json
    output_schema: contracts/match-output.json
    deliverable: true
    side_effects: read_only
    idempotency: deterministic
artifacts:
  produces: [job_matching_report]
completion:
  required_tools: [match-observed-jobs]
  verification: required
errors:
  blocked: [blocked_source, manual_review_required]
  transient: [transport_error, rate_limited]
```

字段含义必须固定：

- `dependencies` 只允许显式依赖上游 Skill artifact，不允许把整份 task context 暴露给下游。
- `context` 只声明字段名、可见性和是否必需；private 值由应用服务注入，永不复制进 generic model state。
- 每个 tool 都有 `input_schema`、`output_schema`、allowed roles、side effects、idempotency 和 artifact 产出声明。
- `completion` 声明确定性完成门；Verifier 可增加独立检查，但不能用 prose 绕过它。
- `version` 与 manifest 内容 hash 应记录到 run/step metadata，使历史运行按旧契约恢复。

### 2.3 输入/输出契约迁移

统一采用 `extra: forbid` 的 JSON Schema/Pydantic 等价契约；公共名称和字段先保持兼容：

| Skill | 主要输入 | 主要输出/artifact | 上游依赖 |
|---|---|---|---|
| `job-discovery` | 官方 URL/有限搜索条件、可选 sheet query | page evidence、`structured_job_details` | 无；只产生公开证据 |
| `job-matching` | candidate/artifact refs、confirmed facts、explicit preferences | `job_matching_report` | `job-discovery.structured_job_details` |
| `resume-tailoring` | 一个 target JD ref、confirmed facts、preferences、match analysis | `resume_tailoring_brief` / diff operations | discovery + 可选 matching |
| `career-planning` | target JD ref、confirmed facts、target date | `career_preparation_plan` | discovery；可选 profile facts |
| `company-research` | 一个公开 company URL | company research artifact | 无 |
| `interview-prep` | target JD、facts、preferences、match analysis | five-section interview kit | discovery + 可选 matching |
| `application-tracking` | current status、requested transition | advisory transition result | 无；不自动提交 |

迁移期间对外保留现有 PEV tool names 和 artifact type；Canonical package 的 adapter 可把新内部 schema 映射到旧输出，直到所有消费者切换完成。

## 3. 文件级改动清单

以下是实施时的文件级范围；本轮没有执行这些改动。

### P0：建立 Canonical package contract 和加载入口

新增：

- `skill/_common/manifest.schema.json`：manifest 自身 schema。
- `skill/_common/runtime_protocol.py`：与 backend 无业务耦合的 `SkillContext`、`ToolSpec`、`EvidenceEnvelope`、`ArtifactEnvelope` 协议。
- 每个包的 `skill/<name>/manifest.yaml`、`contracts/`；首批至少覆盖 4 个当前 PEV Skill，随后覆盖其余 3 个包。
- `skill/<name>/runtime/adapter.py`：包级 tool adapter 入口；输入输出只依赖 package contract。
- `tests/unit/test_canonical_skill_manifest.py`、`tests/unit/test_canonical_skill_contracts.py`、`tests/unit/test_skill_import_boundary.py`。

修改：

- `backend/app/services/agent_runtime/skill_package.py`：从“解析 SKILL.md”升级为解析 manifest、schema、adapter entrypoint、version/digest；缺 manifest 或 schema 不得静默发现。
- `backend/app/services/agent_runtime/skill_definition.py`：只保留通用定义和 contract evaluation；移除 career deliverable/error 推断。
- `backend/app/services/agent_runtime/tool_registry.py`：增加从 `ToolSpec` 注册的通用入口；保留现有 `ToolDefinition` 作为过渡类型，不再在 Core 里写业务 tool 名称。

### P1：把业务执行搬回所属包

迁移到 `skill/job-discovery/runtime/` 的来源：

- `backend/app/services/career_skills/job_discovery.py`
- `backend/app/services/career_skills/career_sheets.py`
- `backend/app/services/career_skills/classify_url.py`
- `backend/app/services/career_skills/deduplicate_observed.py`
- `backend/app/services/career_skills/validate_candidates.py`
- `backend/app/services/career_skills/wechat.py`
- `backend/app/services/career_skills/playwright_worker.py`
- `backend/app/services/job_discovery/schemas.py`
- `backend/app/services/job_discovery/tools/jd_extraction.py`
- `backend/app/services/job_discovery/tools/job_strength.py`
- `backend/app/services/job_discovery/tools/skill_validator.py`
- `backend/app/services/job_discovery/tools/taxonomy.py`
- `backend/app/services/job_discovery/tools/batch_progress.py`

迁移到其他 Canonical 包的来源：

- `backend/app/services/career_skills/job_matching.py` → `skill/job-matching/runtime/`。
- `backend/app/services/career_skills/resume_tailoring.py` → `skill/resume-tailoring/runtime/`，同时把 `scripts/generate.py`/`validate.py` 的差异收敛到同一 contract。
- `backend/app/services/career_skills/career_planning.py`、`target_evidence.py` → `skill/career-planning/runtime/`；target resolver 只能解析 artifact refs。
- `skill/application-tracking/scripts/track.py` 与 `backend/app/domain/application_tracking.py` 的重复状态机需在单独 P1 子任务中选定唯一来源；本方案不把它偷偷注册成 PEV 工具。

在 parity 完成前，上述 backend 文件改为 forwarding shim；禁止继续新增业务逻辑。Parity 完成且 import graph 清理后，删除 shim。

### P1：清空 Agent Core 的求职知识

修改：

- `backend/app/services/agent_runtime/planner_agent.py`：保留通用计划规则；删除 `confirmed_profile_fact_fields`、preferences、职业前置依赖等语义，改为读取 manifest 提供的 context/dependency 摘要。
- `backend/app/services/agent_runtime/executor_agent.py`：删除 `search-public-job-pages`、candidate URL、岗位页面等特判；通用 stall/dedup 由 manifest 的 idempotency 和 generic progress signal 驱动。
- `backend/app/services/agent_runtime/verifier_agent.py`：只调用当前 Skill manifest 的 verifier tools 和 completion contract。
- `backend/app/services/agent_runtime/schemas.py`：删除中文“已收集岗位” marker 和 `job-discovery` 特判；增加通用 `ContextRef`/`ArtifactRef` 校验。
- `backend/app/services/agent_runtime/runtime.py`：删除 JD/structured candidate 投影、职业 artifact type 映射和 `_full_candidate_text`；改成调用 generic `SkillHooks.project_inputs()`、`persist_artifacts()`。
- `backend/app/services/agent_runtime/evidence_gate.py`：移除 `_legacy_registry()`；兼容函数必须显式接收 registry，默认不加载任何职业包。
- `backend/app/services/agent_runtime/error_policy.py`、`skill_definition.py`：删除 OCR/WeChat/职业错误集合，改为读取 package manifest error classes。
- `backend/app/services/agent_runtime/service.py`：移出 profile repository 和 `confirmed_profile_facts` 注入；新增通用 `private_context_provider` seam。职业实现放到 `backend/app/services/career_assistant.py` 或 API composition layer。

### P1：清空第二条 runtime 的业务耦合

- `backend/app/services/deepagents_runtime/tools/adapters.py`：不再导入 `career_skills.registry`；改为消费通用 `SkillCatalog`。
- `backend/app/services/deepagents_runtime/tools/skill_graphs/job_discovery_graph.py`、`browse_fetch.py`、`llm_extractor.py`、`wechat_slice.py`：移入 `skill/job-discovery/runtime/`，或改为只调用包 adapter 的通用工作流 runner。
- `backend/app/services/deepagents_runtime/tools/skill_graphs/subprocess_runner.py`：改名/改造为通用 package entrypoint runner，去掉固定 `skill/job-discovery` 路径。
- `backend/app/services/deepagents_runtime/tools/skill_graphs/__init__.py`：通过 manifest 的 entrypoint 选择工具，不得 `if skill_name == "job-discovery"`。
- `backend/app/services/deepagents_runtime/eval/compare_runner.py`、`tests/unit/test_deepagents_*`：改从 `SkillCatalog` 获取工具，保留现有 job-discovery parity 资产。

### P1：组合根、兼容和文档

- `backend/app/main.py`：只负责加载 `skill/` catalog、注入 generic runtime 和职业应用层 hooks；删除对 `career_skills.job_discovery`/`wechat` 的直接配置调用，改由包 manifest lifecycle hook。
- `backend/app/config.py`：增加明确的 `canonical_skill_runtime_enabled` 与 `legacy_career_skill_adapter_enabled`；两个 flag 只能在 composition root 解释。
- `backend/app/services/career_skills/manifest.py`、`registry.py`：先改为 compatibility facade，最终删除硬编码 4-skill manifest 和 13-tool registration。
- `docs/pev-agent-architecture.zh-CN.md`、`CLAUDE.md`、`docs/agent-runtime-skill-decoupling.md`、`docs/superpowers/specs/2026-08-07-deepagents-runtime-design.md`：更新为“Canonical Skill package → generic catalog → runtime”，移除“career_skills 是 Skill 来源”的表述。
- 如需审计历史运行：在 `backend/app/db/models.py`、`backend/app/repositories/agent_runtime.py` 增加 `skill_manifest_version`/`skill_manifest_digest` 字段及 Alembic migration；否则至少把 digest 写入 run metadata。不能改已有 migration，只能新增 migration。

### P2：删除重复源

仅在下列门禁通过后删除：

- `backend/app/services/career_skills/*.py` 中已迁移的业务实现，只保留明确标注的兼容 shim。
- `backend/app/services/job_discovery/tools/` 中已迁移的业务算法和 `data/job_taxonomy.json`/`data/skill_tags.json`；若这些数据被其他平台 API 使用，先拆成通用数据包并由 Canonical Skill 引用。
- `backend/app/services/deepagents_runtime/` 中只服务 job-discovery 的重复 graph 实现。

## 4. 迁移顺序和切换门

1. **建立清单**：为 7 个包补齐 manifest/schema；CI 先只检查一致性，不切换执行。
2. **迁移 job-discovery**：它规则最多，先把抓取/提取/校验/去重/证据 envelope 迁入包内；跑现有 10-URL parity 和真实公开页面安全回归。
3. **迁移 3 个当前 deliverable Skill**：matching、tailoring、planning；旧 tool name 和 artifact type 保持不变。
4. **迁移其余 3 个 package-only Skill**：先以 manifest 注册为 `package_only`，确认是否需要 PEV adapter；未实现 adapter 前不得伪装成可执行 Skill。
5. **切换 composition root**：Canonical adapter 作为唯一运行路径；legacy shim 只接收显式 flag，不在 Agent Core fallback。
6. **双轨 parity 但单轨执行**：同一 fixture 可调用 old shim 和 canonical adapter 做对比；生产一次只执行一条路径，避免重复抓取、重复写 artifact。
7. **删除重复源**：import graph、测试、文档全部通过后删除业务实现和 legacy fallback。

## 5. 兼容策略

- **API 兼容**：不改 agent-run API、SSE 事件、`ToolObservation` 顶层结构、13 个旧 tool name、现有 artifact type 和 owner scoping。
- **输入兼容**：canonical adapter 接受旧 payload；新字段通过 manifest `contract_version` 逐步启用，未知字段继续拒绝。
- **输出兼容**：canonical output 先映射到旧 Pydantic/JSON 形状；字段语义变化必须新增 contract version，不覆盖旧含义。
- **运行恢复兼容**：queued/running/waiting_user 的历史 run 按保存的 manifest digest/version 恢复；找不到旧版本时明确 `skill_contract_unavailable`，不得用另一套业务规则悄悄接管。
- **导入兼容**：`backend/app/services/career_skills/*` 在一个迁移窗口内保留薄 shim；shim 不复制算法，只转发到 package adapter，并输出一次 deprecation telemetry。
- **安全兼容**：登录、验证码、反爬、OCR 未启用等都由包声明为 human-gated；Core 只执行“需要人工”的通用路由，绝不新增绕过逻辑。
- **配置兼容**：旧配置名在 composition root 做映射；Agent Core 不读取 `job_discovery_*` 等职业配置。

## 6. 验证命令和验收标准

### 6.1 当前基线审计

```powershell
Set-Location 'D:\Program Files\JetBrains\PyCharm Community Edition 2024.2.2\proj\langgraph-multi-agent-career-assistant-main'
rg -n 'career_skills|job-discovery|candidate_urls|confirmed_profile_facts|structured_job_candidates|match-observed-jobs|build-resume-tailoring-brief|build-preparation-plan' backend/app/services/agent_runtime
rg -n 'backend\.app\.services\.career_skills|skill/job-discovery' backend/app/services/deepagents_runtime
```

迁移完成时，第一条只能命中通用契约测试/明确的 adapter boundary 注释，不得命中 Agent Core 的职业分支；第二条只能命中 generic package loader 测试，不得有固定职业 import。

### 6.2 Manifest 与导入边界

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_canonical_skill_manifest.py tests/unit/test_canonical_skill_contracts.py tests/unit/test_skill_import_boundary.py -q
.\.venv\Scripts\python.exe -c "from pathlib import Path; from backend.app.services.agent_runtime.skill_package import discover_skill_packages; p=discover_skill_packages(Path('skill')); print([(x.name, x.path.as_posix()) for x in p]); assert len(p) == 7"
```

验收：7 个包都能发现；每个都有 manifest、schema、版本和唯一 adapter/`package_only` 状态；缺文件时启动失败而不是降级为旧业务实现。

### 6.3 契约和 parity

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_canonical_skill_* tests/unit/test_agent_runtime_contracts.py tests/unit/test_career_skill_manifest.py tests/unit/test_career_skill_registry.py -q
.\.venv\Scripts\python.exe tests/manual/run_skill_ten_url_eval.py --help
```

实际实施时，对 `job-discovery` 运行现有 10-URL/公开页面 fixture；对 matching/tailoring/planning 用固定 evidence + profile fixture 比较 old shim 与 canonical adapter 的 JSON。要求：工具名、artifact type、来源 hash、阻断语义不回归；排序/文本仅允许在 contract version 中明确变化。

### 6.4 运行时和安全回归

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_agent_runtime*.py tests/unit/test_planner_agent.py tests/unit/test_executor_agent.py tests/unit/test_verifier_agent.py tests/unit/test_*pev_skill.py tests/unit/test_job_matching_skill.py -q
.\.venv\Scripts\python.exe -m pytest tests/unit/ -q
.\.venv\Scripts\python.exe -m ruff check backend tests scripts
coverage run --branch --source=backend -m pytest tests/unit/
coverage report --fail-under=100
```

额外必须有负向测试：

- generic runtime 注册一个非职业 Skill，运行时不导入任何 `career_skills` 模块。
- tool manifest 缺 input/output schema、越权 role、未声明 dependency、未声明 deliverable 时启动拒绝。
- blocked/anti-bot/credential-bearing error 只返回稳定通用结果，不泄露 URL token 或用户私密字段。
- 旧 run 恢复时使用保存的 package version；没有对应 package 时暂停并报告 `skill_contract_unavailable`。

## 7. 风险和明确不做事项

- 不把所有 `SKILL.md` prose 直接拼进 system prompt 当作执行实现；这只能减少 prompt 耦合，不能解决双重业务源。
- 不在 Agent Core 继续添加 `if skill_name == ...` 作为过渡；过渡逻辑必须位于 composition root 或 package adapter。
- 不先删除 `career_skills` 再迁移；应先建立 contract、adapter、parity 和 import-boundary 门禁。
- 不把 `company-research`、`interview-prep`、`application-tracking` 未完成的 adapter 宣布为 PEV 可执行 Skill；manifest 可以先标记 `package_only`。
- 不改变登录/验证码/反爬安全红线，不把模型提出的 URL 或 JD 文本当作证据。

## 8. 本轮文件状态

输入/审计路径：根目录 `skill/`、`backend/app/services/career_skills/`、`backend/app/services/job_discovery/`、`backend/app/services/agent_runtime/`、`backend/app/services/deepagents_runtime/` 及相关 docs/tests。

本轮新增或更新的仅是方案工件：`canonical-skill-migration-plan.md`、`notes.md`、`task_plan.md`。Python、前端、配置、数据库迁移和测试代码均未修改。
