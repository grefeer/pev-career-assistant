# DeepAgents Runtime：基于 langchain deepagents 的 PEV 运行时设计

日期：2026-08-07

状态：设计方向已确认，书面设计等待用户审阅

## 1. 决策摘要

在 `backend/app/services/deepagents_runtime/` 下新建一个基于 **langchain `deepagents`（0.6.12）** 的 PEV 运行时，与自研 `agent_runtime` **并行构建、对比评测**；对比通过后再议替换。核心决策：

- **三个 deep agent**（Planner / Executor / Verifier），由**外部 LangGraph 状态图**串联（planner → executor → verifier → route 循环），不采用 supervisor + SubAgent 层级（预算硬顶、每 step 单 skill、验证路由是 harness 不变量，不交给 LLM 调度）。
- **skill 业务逻辑以工作流形态存在**：将 `skill/job-discovery` 的 SKILL.md/references 业务流程编码为 **LangGraph 子图**（节点 = SKILL.md 阶段），再**包装成 `@tool`** 供 Executor 调用。机械阶段全确定性（复用 skill 脚本），LLM 只留在提取节点（regex 优先、低置信/空结果才 LLM）。
- **持久化**：`langgraph-checkpoint-redis`（Redis AOF 已开）作为执行态 checkpoint；每次用户问题运行结束后 `flush_run` 落 MySQL（`deepagents_runs` + `deepagents_artifacts`），**MySQL 保持完成记录与证据的权威副本**（铁律 #5 例外条款见 §12）。
- **新包纳入 100% 分支覆盖门**；单测用 InMemorySaver + mock LLM，不依赖真实栈。
- **工具来源两源并用**：career_skills 的 9 个 tool 原样复用（适配为 `@tool`）；job-discovery 走 skill 工作流子图。其余 skill（job-matching / career-planning / resume-tailoring 确定性版 / company-research / interview-prep / application-tracking）按业务需求、准确性、健壮性挑选（§4.4）。

## 2. 背景与动机

现状：自研 `agent_runtime`（Planner/Executor/Verifier + ToolRegistry + 预算 + MySQL 持久化）已稳定（100% 覆盖、150 题评测收敛），但自研了整套 LLM 调用循环（model_gateway、json_mode 补丁、消息历史拼接），维护成本高且与 DeepSeek 漂移对抗（记忆：2026-08-05 drift 事件）。

目标：用 deepagents 接管 agent 循环（消息管理、工具调用解析、todo 列表、checkpoint），保留自研框架被证明有效的**不变量**（预算硬顶、去重、证据绑定、skill scoping、可恢复降级），"取其精华弃其糟粕"。

用户否决过的方案及其理由（记录备查）：
- 只包装 browse + coverage 成 tool（丢弃 skill 文档业务逻辑，需手工重编）—— 否决。
- LLM 自由编排 skill 脚本（eval 模式照搬）—— 保留为 **parity 基准**，不作为主路径（token/轮次高、覆盖门难）。
- 纯 Python 管道（无 checkpoint 中途失败恢复弱）—— 用户选择图 + checkpointer 直接上。

## 3. 架构总览

```
外部 LangGraph 状态图（harness, thread_id = run_id, checkpointer = RedisSaver）
  │  强制: 预算硬顶 / 每step单skill / 证据绑定 / 去重 / waiting_user 降级
  ├─ planner节点  = create_deep_agent(无工具, 产出 ExecutionPlan)
  ├─ executor节点 = create_deep_agent(每step只见该skill的工具目录)
  │                 ├─ career_skills 9 tool 适配器（@tool）
  │                 └─ skill 工作流子图包装 @tool（job-discovery）
  └─ verifier节点 = create_deep_agent(无业务工具, 仅基于证据与产出做独立判定,
                                    路由 PASS/RETRY/REPLAN/NEED_USER/FAIL)

持久化:
  RedisSaver（langgraph-checkpoint-redis, AOF 持久化）← 执行态（3 agent + harness 图共用）
  MySQL sink（flush_run, 运行结束时）→ deepagents_runs + deepagents_artifacts（业务权威）
```

## 4. 工具层

### 4.1 career_skills 工具适配器（tools/adapters.py）

- 9 个已注册 tool（registry.py）的 handler 原样复用，**不改 career_skills 代码**。
- 适配方式：`@tool` 函数，输入 = payload JSON 字符串；从图状态构造 `ToolContext`（`observed_public_evidence`、`confirmed_profile_facts`、`structured_job_candidates`）；调 handler；输出 = output model JSON，evidence 同步写回图状态（evidence store）。
- 错误折叠：handler 异常 → `ToolObservation(status=failed, error_code=...)` + 去敏感（迁移 `_sanitize_error_message` 语义）。
- 去重：包装层记录"上一次成功调用签名"，连续相同调用 → 返回 `duplicate_tool_call` 观察（与现框架语义一致）。

### 4.2 skill 工作流子图 → @tool（tools/skill_graphs/）

**job-discovery 工作流子图**（`job_discovery_graph.py`）—— 将 SKILL.md 6 阶段流程编码为 LangGraph 子图节点：

```
[fetch: requests 快路径 → browse.py(parallel-fetch/list) 回退]  ← 确定性（subprocess seam）
→ [extract: regex → 低置信/空结果才 LLM]                       ← 唯一 LLM 节点
→ [validate/dedup: 脚本或纯函数]                               ← 确定性
→ [coverage_gate]                                             ← 确定性
→ 输出: per_url_results + evidence + coverage（结构化部分结果契约）
```

- 子图编译时挂 checkpointer（thread = f(run_id, step_index, role="workflow")，与 §5 线程约定一致）→ **中段崩溃 resume 后从断点 URL 继续，不重爬**。
- 子图包装为 `@tool` 注册进 executor 工具目录（仅 job-discovery skill 的 step 可见）。
- 脚本执行通道唯一化：白名单 subprocess（仿 `run_skill_ten_url_eval.py` 的 `run_skill_script` 白名单与超时上限 900s、PYTHONUTF8、cwd=skill 目录）；`normalize/deduplicate/state/ocr_image/validate/write_candidates` **不直接暴露给 LLM 编排**，其逻辑由子图节点内部调用。
- deepagents 默认 `execute`/文件工具：**不提供 backend → 全部失效**（唯一执行通道是白名单包装）。

### 4.3 提取门控（tools/extract_gate.py）

```
extract-observed-job-details(-batch)（regex，已有，输出含 confidence）
  ├─ confidence ≥ 阈值 或结果非空 → 直接产出（0 token）
  └─ 低置信/空结果 且 job_discovery_llm_extraction_enabled=true
       → LLM 提取（只读失败证据全文）→ 合并产出
```

- 复用遗留 flag `job_discovery_llm_extraction_enabled`（config.py:118，当前为死配置），默认 False，评测时开关对比。

### 4.4 其余 skill 的挑选

| skill | 来源 | 形态 |
|---|---|---|
| job-matching | career_skills（确定性） | 直接适配为 @tool，无状态不进图 |
| career-planning | career_skills（确定性） | 直接适配为 @tool，无状态不进图 |
| resume-tailoring | 小图（LLM 生成节点 + validate 确定性节点） | 评测期与 career_skills 确定性版并排对比 |
| company-research / interview-prep / application-tracking | skill/ 脚本 | 评测期可选注册（脚本包装），业务场景定后再加权 |

## 5. Harness 状态图与不变量

- **状态 schema**（state.py）：run 元数据、ExecutionPlan、step 索引、evidence store、预算计数器（turns/tool_calls/replans）、决策历史。
- **路由**：确定性条件边 —— Verifier PASS → 下一 step 或 succeeded；RETRY_EXECUTOR → 预算内重入 executor；REPLAN → planner（replans 预算内）；NEED_USER → waiting_user；FAIL → failed。
- **预算硬顶**（budgets.py，迁移自 agent_runtime）：turn / tool_call / replan / wall-clock，在**每个 agent 节点边界**检查；超限 → 降级 `waiting_user`（可恢复，永不硬失败）。
- **stall-breaker**：连续 3 次无进展决策（duplicate_tool_call 或 blocked）→ 转 `needs_user`。
- **每 step 单 skill**：图状态记录 `current_skill`，Executor 的 invoke 只绑定该 skill 工具目录。
- **证据绑定**：只有工具产出的 evidence（content_hash + source_url）写入 evidence store；模型提议的 URI 不信任（沿用现框架语义）。
- **agent 线程映射**：每 step 的 agent 线程 = `f(run_id, step_index, role)`，节点重入时 agent 从自身 checkpoint 续跑，不重复已完成 LLM 调用。
- **恢复语义**：崩溃/waiting_user → 同一 thread_id 从最后完成的节点续跑；预算计数跨恢复**不重置**（存 channel values），wall-clock 窗口在 resume 时刷新。

## 6. 持久化

### 6.1 Redis checkpointer（执行态）

- `checkpoints/factory.py`：按 `checkpoint_backend` 配置切换 —— `redis`（`langgraph.checkpoint.redis.RedisSaver`，sync）/ `inmemory`（单测）。
- Redis AOF 已开（docker/redis/start.sh `appendonly yes` + `redis-data` 卷）→ 崩溃不丢 checkpoint。
- `thread_id = run_id`；harness 图 + 3 个 deep agent 共用同一 saver。
- 激活遗留配置：`checkpoint_backend: Literal["sqlite","redis"]`（config.py:70）与生产校验 `CHECKPOINT_BACKEND=redis`（config.py:258-259）——本设计重新启用 `redis` 分支。

### 6.2 MySQL sink（业务权威，最终一致）

- 每次用户问题运行结束（succeeded / failed / waiting_user / cancelled）→ `flush_run(run_id, snapshot)`：
  - `deepagents_runs`：run_id（PK）、thread_id、status、plan、steps、决策历史、时间戳；**UPSERT 幂等**。
  - `deepagents_artifacts`：evidence 明细（artifact_id、run_id、source_url、content_hash、kind、payload）——证据权威副本。
  - 单事务提交（SQLAlchemy session）；失败重试 + 退避。
- alembic 新迁移（当前 head 之后）。

### 6.3 残余风险（诚实标注）

1. 运行进行中状态只存在于 Redis：完成瞬间崩溃且落库失败 → MySQL 缺该记录。缓解：落库重试 + 可选启动时对"终态未落库"线程的补救清扫（评测期暂不做，标注）。
2. langgraph-checkpoint-redis 0.5.0 与 langgraph-checkpoint 4.1.1 的 API 兼容需实施期 smoke 验证（导入正常 ≠ 全 API 行为一致）。

## 7. 评测与 parity

- **对比评测**（`eval/compare_runner.py`）：同一组问题分别跑 agent_runtime 与 deepagents_runtime，输出对比报告（JSON + Markdown）：成功率分布（succeeded/waiting_user/failed）、平均轮次、token 估算、wall-clock、恢复次数。
  - 题集：83-doc 真实简历集（链式 + 按公司类型）+ 20 题 harness；job-discovery 专项用 10-URL 集。
- **parity 门（防翻译漂移）**：job-discovery 工作流子图 vs B 模式基准（`run_skill_ten_url_eval.py` 已验证行为）跑同一 10-URL 集，成功数/提取质量**不劣化才通过**。
- 评测跑真实栈（docker compose：Redis + MySQL）；单测不依赖。

## 8. 测试与 100% 覆盖门

| 层 | 单测手段 |
|---|---|
| LLM 调用 | `FakeListChatModel` / `GenericFakeChatModel` 注入 `create_deep_agent(model=...)`，断言工具调用序列与路由决策 |
| checkpointer | `InMemorySaver`（factory seam）；RedisSaver 仅集成冒烟（可选，需 Redis） |
| subprocess（browse.py 等） | 注入 seam 模拟脚本输出（仿 `career_sheets.py` 的 `_list_records_impl` 模式） |
| flush_run 落库 | mock SQLAlchemy session，断言幂等 UPSERT 与重试路径 |
| 图节点/预算门/降级 | 确定性断言：预算超限→waiting_user、连续无进展→needs_user、路由分支全覆盖 |

新包纳入 `[tool.coverage.run]` 覆盖门（100% 分支），测试文件 `tests/unit/test_deepagents_*.py`。

## 9. 文件结构

```
backend/app/services/deepagents_runtime/
├── __init__.py
├── state.py            # harness 图状态（evidence store、预算计数、计划、决策）
├── harness.py          # 外部状态图：planner→executor→verifier→route + 预算门/降级
├── agents.py           # 3× create_deep_agent（模型注入；无 backend→execute/file 失效；subagents=None）
├── budgets.py          # 预算硬顶、去重、stall-breaker（迁移）
├── checkpoints/
│   ├── factory.py      # saver 工厂（redis/inmemory 按 checkpoint_backend）
│   └── sink.py         # flush_run: deepagents_runs + deepagents_artifacts（幂等+重试）
├── tools/
│   ├── adapters.py     # career_skills 9 tool → @tool 适配
│   ├── skill_graphs/
│   │   ├── job_discovery_graph.py  # SKILL.md 阶段→节点的工作流子图
│   │   └── __init__.py             # 子图→@tool 包装（挂 checkpointer）
│   └── extract_gate.py # regex→LLM 门控提取
└── eval/
    └── compare_runner.py
alembic 迁移（deepagents_runs / deepagents_artifacts）
tests/unit/test_deepagents_*.py
CLAUDE.md #5 例外条款（见 §12）
```

## 10. 分阶段实施（每阶段独立提交、测试全绿）

- **P0 骨架**：目录 + state.py + budgets.py + checkpoints/factory.py + alembic 迁移（InMemory 测试）
- **P1 harness**：harness.py + agents.py —— 3 agent 图 + 路由 + 预算门（mock LLM 单测，100% 覆盖）
- **P2 工具层**：tools/adapters.py（9 tool 适配）+ extract_gate.py
- **P3 job-discovery 子图**：skill_graphs/job_discovery_graph.py + @tool 包装 + 10-URL parity
- **P4 落库**：checkpoints/sink.py + flush_run + 崩溃恢复冒烟
- **P5 评测收尾**：eval/compare_runner.py + 覆盖收口 + CLAUDE.md 更新

## 11. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 工作流编码与 skill 文档行为漂移 | 10-URL parity 门（§7），不劣化才通过 |
| Redis 丢失进行中运行 | AOF 已开；flush 重试 + 启动补救清扫（后置） |
| langgraph-checkpoint-redis 兼容性 | P0 期 smoke 验证，不兼容则回退 MySQL saver 方案 |
| deepagents 循环行为非确定性 | 预算硬顶兜底；评测量化轮次/token |
| 100% 覆盖门对 deepagents 包装层挑战 | InMemorySaver + mock LLM + subprocess seam |

## 12. 铁律 #5 例外条款（CLAUDE.md 修改，需用户批准）

CLAUDE.md「Security Hard Gates」第 5 条新增例外：

> **例外**：`deepagents_runtime` 的 agent 执行态 checkpoint（LangGraph 线程）可存于 Redis（AOF 持久化），但**完成的运行记录与证据的权威副本始终在 MySQL**（运行结束时 `flush_run` 落库，幂等 + 重试）。本例外仅为执行态短生命周期状态，不改变 MySQL 对业务状态的权威性。
