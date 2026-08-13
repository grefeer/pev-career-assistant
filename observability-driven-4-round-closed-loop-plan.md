# Agent 5：可观测性驱动的 4 轮闭环优化方案

## 结论

建议采用独立的 **OD4（Observability-Driven 4-round closed loop）** 方法：把每个失败案例还原成脱敏的 Planner → Executor → Tool → Verifier 调用链，在固定 3 分钟节拍上统计，单批次失败超过 30 立即熔断；每一轮只允许一个最高优先级簇进入修复，自动从该簇生成可重放回归用例，最后用同一批次、同一口径和缓存证据完成验收。

OD4 的核心产物不是日志，而是四个可复用对象：

1. `case_trace`：一个题目/链路的完整调用链。
2. `failure_cluster`：一组归一化后具有相同根因的失败链。
3. `diagnostic_report`：某轮的证据、判断、风险和修复建议。
4. `regression_manifest`：由失败簇自动生成的确定性回归场景。

它不以依赖升级或 Skill 迁移为前提；外部站点阻断也不会被伪装成代码成功。

## 1. 当前代码可直接利用的观测面

当前运行时已经具备足够的持久化骨架：

- `AgentRun`、`AgentPlan`、`AgentStep`、`AgentTurn`、`AgentEvent`、`AgentArtifact` 已在 [`backend/app/db/models.py`](D:/Program%20Files/JetBrains/PyCharm%20Community%20Edition%202024.2.2/proj/langgraph-multi-agent-career-assistant-main/backend/app/db/models.py:1054) 定义。
- 事件通过 [`append_event`](D:/Program%20Files/JetBrains/PyCharm%20Community%20Edition%202024.2.2/proj/langgraph-multi-agent-career-assistant-main/backend/app/repositories/agent_runtime.py:323) 按 `run_id + sequence` 持久化，并由 [`events/stream`](D:/Program%20Files/JetBrains/PyCharm%20Community%20Edition%202024.2.2/proj/langgraph-multi-agent-career-assistant-main/backend/app/api/routes/agent_runtime.py:279) 按游标回放。
- Runtime 已记录工具失败、结构化产物、Verifier 重试和同构重规划等关键节点，例如 [`executor_tool_failed`](D:/Program%20Files/JetBrains/PyCharm%20Community%20Edition%202024.2.2/proj/langgraph-multi-agent-career-assistant-main/backend/app/services/agent_runtime/runtime.py:1398) 和 [`verification_retry_executor`](D:/Program%20Files/JetBrains/PyCharm%20Community%20Edition%202024.2.2/proj/langgraph-multi-agent-career-assistant-main/backend/app/services/agent_runtime/runtime.py:742)。
- `ToolRegistry` 已把异常归一为稳定错误码，并对消息脱敏；现有预算包括 agent turns、tool calls、replans、model requests、tokens 和 wall-clock。

现有缺口决定了 OD4 的第一轮必须先补齐观测契约：[`/api/metrics`](D:/Program%20Files/JetBrains/PyCharm%20Community%20Edition%202024.2.2/proj/langgraph-multi-agent-career-assistant-main/backend/app/api/routes/metrics.py:13) 目前只报告依赖 readiness，不报告 AgentRun 失败率、工具链、Verifier 决策或熔断状态；评测 runner 也只保留工具聚合和错误码，明确不保存失败调用原始 payload 或完整错误信息（见 [`83-question-full-eval-report-2026-08-12.md`](D:/Program%20Files/JetBrains/PyCharm%20Community%20Edition%202024.2.2/proj/langgraph-multi-agent-career-assistant-main/docs/83-question-full-eval-report-2026-08-12.md:65)）。

## 2. OD4 的统一数据和计数口径

### 2.1 失败案例单位

以一个评测题目或链路 link 为一个 `case_id`，不要按事件数计数；同一个 case 的三次重复工具调用只能计为一个失败案例，但要保留 `attempt_count=3`。

每个 case 关联：

```text
campaign_id → case_id → run_id → link_id → plan_revision → step_sequence → turn/event sequence
```

### 2.2 脱敏调用链节点

每个节点只保留：

```json
{
  "seq": 12,
  "role": "executor",
  "step": 2,
  "skill": "job-matching",
  "tool": "match-observed-jobs",
  "outcome": "failed",
  "error_code": "invalid_tool_input",
  "schema_fields": ["limit"],
  "artifact_types_before": ["structured_job_details"],
  "verifier_decision_after": "RETRY_EXECUTOR",
  "input_fingerprint": "sha256:..."
}
```

禁止保存 token、密码、完整简历、完整网页、原始工具 payload 和带用户信息的 URL。URL 只保留域名、路径模板和 content hash；输入只保存字段路径、类型和 hash。

### 2.3 三类结果计数

- `hard_failure`：顶层 `failed`，或因确定性内部错误进入 `waiting_user`，包括 `invalid_tool_input`、`invalid_tool_output`、`target_evidence_not_found`、`tool_skill_forbidden`、`unknown_tool`、`duplicate_tool_call`/stall、`model_request_failed` 等。
- `external_blocked`：登录、验证码、反爬、SPA 空壳、微信不可达、`public_page_content_insufficient` 等外部阻断。它不计入内部硬失败，但必须单独统计。
- `success`：完成确定性交付契约并通过 Verifier 与 Skill completion gate。

主停机计数定义为：

```text
F_total = 去重后的 hard_failure case 数
F_3m    = 最近 3 分钟新增的 hard_failure case 数
```

熔断条件为 `F_total > 30 OR F_3m > 30`。若团队要求“所有非 succeeded 都是失败”，可额外启用 `N_total = hard_failure + external_blocked` 作为更严格的批次门禁；默认口径不把正确的人工降级误判成代码故障。

## 3. 每 3 分钟监控流程

监控器可以先作为评测控制面/sidecar 运行，读取 MySQL 的 AgentRun、AgentTurn、AgentEvent、AgentArtifact 和评测 JSON；不依赖业务 Agent 自己判断是否异常。

每个 `T=180s` tick 按以下顺序执行：

1. 读取 `campaign_state`。若已是 `HALTED`，只收集尾部证据，不再提交新 case。
2. 以 `created_at`、`finished_at` 和事件 sequence 增量读取上一 tick 之后的 run、step、turn、event。
3. 通过事件序列还原新增或终止的 `case_trace`，按 case 去重。
4. 计算 `F_total`、`F_3m`、`external_blocked`、成功率、P95 wall time、每工具失败率、每错误码占比、重复调用率、Verifier RETRY/REPLAN 比例、artifact 转化率和 token 累计。
5. 对新失败链执行两级聚类：先精确 hash，再近似合并；输出簇大小、首次出现时间、最近出现时间、代表 case、主要工具、主要错误码和影响技能。
6. 写入本 tick 快照和增量诊断报告：

```text
output/observability/<campaign_id>/tick-YYYYMMDD-HHMM.json
output/observability/<campaign_id>/clusters.json
output/observability/<campaign_id>/diagnostic_report.md
```

7. 最后执行阈值判断。不能先继续投放、再异步判断，否则会在越过 30 后继续污染样本。

建议的告警级别：

| 级别 | 条件 | 动作 |
|---|---|---|
| Green | `F_3m=0` 且无簇快速增长 | 继续当前轮 |
| Amber | 任一簇占新增失败 ≥20%，或同一工具连续 2 个 tick 上升 | 降低并发，冻结该簇新样本，优先生成回归 |
| Red/HALTED | `F_total>30` 或 `F_3m>30` | 立即熔断，停止新任务和新评测进程 |

### 3.1 “立即停止”的边界

熔断动作必须原子化：

1. 写入批次锁 `HALTED`、时间、阈值、计数快照和触发簇。
2. 停止 evaluator/worker 的新 case 提交，拒绝队列中的新任务。
3. 对已运行 case：优先通过受控取消接口转为 `cancelled`；当前路由文件已提供 create/resume/recover/stream，但未发现现成的 cancel handler，因此在现状下应停止评测 worker 进程并让已进入 Runtime 的有界 run 收敛，不应由监控器直接改写业务表。
4. 保存 halt 前后各一个 tick 的事件窗口，生成诊断报告。
5. 只有人工确认根因、修复分支通过针对性回归后，才创建新的 `campaign_id` 解锁；不得在原批次上续跑。

## 4. 失败案例工具调用链聚类

### 4.1 归一化规则

对每个 case 把事件压缩为：

```text
Planner(plan revision/skill sequence)
→ Executor(tool, input schema fingerprint, result/error)
→ Verifier(tool/decision/feedback class)
→ Runtime( retry | replan | waiting_user | failed | succeeded )
```

去掉 run UUID、时间、随机 artifact UUID 和 URL 查询参数；保留 skill、step 序号、工具名、错误码、字段路径、artifact 类型、Verifier 决策、重试次数和预算耗尽原因。

### 4.2 两级聚类

1. **Exact cluster**：对归一化节点序列做 SHA-256。适合 `match-observed-jobs → invalid_tool_input → RETRY×3 → waiting_user` 这类完全重复链。
2. **Near cluster**：使用加权 Jaccard/编辑距离合并仅在 URL、候选数量或工具参数 hash 上不同、但“技能 + 工具顺序 + 错误码 + 终止路径”相同的链。

簇的主键建议为：

```text
(first_failing_tool, terminal_error, verifier_decision, step_skill, normalized_chain_hash)
```

簇优先级：

```text
priority = case_count × reproducibility × internal_recoverability × business_impact
```

外部阻断簇不与内部契约簇合并，只在报告中作为环境基线；否则会把 `public_page_content_insufficient` 和 `target_evidence_not_found` 错误地归为同一个“抓取失败”。

## 5. 自动诊断报告

每个 tick 生成增量报告，每轮结束生成汇总报告，固定包含：

1. **结论**：本轮是否继续、是否熔断、最影响结果的一个簇。
2. **样本口径**：campaign、题目数、并发、模型/版本、缓存模式、时间窗口。
3. **结果表**：success、hard failure、external blocked、waiting_user、平均/P95 时间、token。
4. **失败簇表**：簇 ID、数量、占比、代表链、首个/最近 case、工具、错误码、可恢复性。
5. **证据链**：从抓取、结构化 JD、匹配报告到下游准备计划的产物转化漏斗。
6. **根因判断**：事实、推断、尚未证实的假设分栏；每个假设必须有一个可执行验证。
7. **修复候选**：只允许一项 P0/P1 进入下一轮，列出影响面和不应修改的范围。
8. **回归清单**：本轮自动生成的 fixture、预期状态、预期事件序列和安全断言。
9. **限制**：缺失的原始字段、外部服务不稳定、不能直接比较的历史批次。

## 6. 自动生成回归测试

不要从失败 payload 直接生成可执行 Python；生成声明式 `regression_manifest.json`，由固定的通用 runner 读取，防止把敏感输入或任意代码带入测试。

每个失败簇自动生成一个最小场景：

```json
{
  "cluster_id": "C-invalid-input-match-001",
  "seed": "fixed",
  "plan": ["job-discovery", "job-matching"],
  "tool_trace": [
    {"tool": "fetch-public-job-pages", "outcome": "succeeded"},
    {"tool": "extract-observed-job-details-batch", "outcome": "succeeded"},
    {"tool": "match-observed-jobs", "outcome": "invalid_tool_input", "field": "limit"}
  ],
  "expected": {
    "max_identical_calls": 1,
    "terminal_status": "waiting_user_or_succeeded",
    "must_emit": ["executor_tool_failed"],
    "must_not_emit": ["runaway_retry"],
    "secrets_in_trace": 0
  }
}
```

通用 runner 使用已有的 SQLite fixture、`RoleScriptedGateway`、`ToolRegistry` 和 Runtime，生成四类断言：

- **契约断言**：错误字段明确、输入修正后可成功，或在不可修正时有界地进入人工。
- **证据断言**：上游 artifact 的 `artifact_id/content_hash` 能被下游正确解析；不能把 `candidate_id`、`source_url` 等不同指针静默混用。
- **收敛断言**：同一工具/同一参数不得无限重复；三次无进展必须有稳定终止事件。
- **安全断言**：事件和报告中不出现 token、密码、完整网页、完整简历或原始敏感 payload。

每个簇至少生成：一个原始失败用例、一个最小修复成功用例、一个边界/阻断用例。回归执行失败时，自动把新链挂回原 cluster；若出现新 `chain_hash`，标记为潜在回归，不允许只看总成功率放行。

## 7. 四轮闭环执行表

### 第 1 轮：基线观测（Observe）

**目标**：不改变业务行为，只建立完整的 case trace、计数口径和 3 分钟控制面。

- 固定 campaign、题目清单、并发、模型、预算和缓存策略。
- 从 `AgentEvent`、`AgentTurn`、`AgentArtifact` 和评测 JSON 还原链路。
- 先以历史可用字段运行；缺失的 schema 字段、完整错误信息、input fingerprint 记为观测缺口。
- 每 3 分钟产出 tick 快照；一旦 `F_total/F_3m>30`，按红色规则停机。

**退出条件**：至少 95% 的非成功 case 能还原到最后一个工具/Verifier 决策；剩余 case 有明确 `trace_incomplete` 标签；报告不泄露敏感信息。

### 第 2 轮：聚类诊断（Diagnose）

**目标**：把“失败数量”转成可验证的根因簇。

- 运行 exact + near 聚类。
- 把内部失败、外部阻断、模型/上下文异常分层。
- 对每个 Top 簇生成代表链、最小 fixture、假设和验证命令。
- 冻结外部阻断样本作为环境基线，不让其驱动内部契约修复。

**退出条件**：Top 簇覆盖 ≥90% 的 `hard_failure` case；每个 Top 簇均有一条自动回归 manifest；无法解释的链不得进入修复轮。

### 第 3 轮：定向修复与影子验证（Fix/Replay）

**目标**：一次只处理一个最高优先级内部簇，先用历史 trace 重放，再做小规模 canary。

- 先运行原始 fixture，确认回归能稳定重现。
- 只改与该簇直接相关的边界/决策逻辑；同时运行原始失败、最小成功和外部阻断三个场景。
- 每 3 分钟监控；任一新簇快速增长或 `>30` 立即停机。
- 通过门槛后再进入下一簇；不允许在同一轮同时混入契约、抓取和提示词多项变量。

**退出条件**：目标簇失败率下降 ≥50%，原有成功案例无回退，重复调用次数不增加，安全断言全通过。

### 第 4 轮：全量回归与发布验收（Verify）

**目标**：证明改进来自修复而不是实时站点、并发或随机模型波动。

- 先跑全量声明式回归 manifest。
- 再用固定题集、固定并发上限、固定模型配置和缓存证据进行 83 题重放。
- 最后单独跑公网 live smoke；live 结果与缓存重放分开统计。
- 比较的是同一口径的 `hard_failure`、`external_blocked`、链路成功率、P95、token 和 Top cluster 分布，不只比较顶层 succeeded 数。

**放行条件**：无新增 P0 簇；内部 hard failure 不高于基线且目标簇下降 ≥50%；外部 blocked 只作为环境指标；所有 generated regression 通过；报告能解释每个非成功 case。

## 8. 结合当前代码的三类最可能根因和修复顺序

### P0：跨步骤工具/证据契约断裂

**证据**：当前报告记录 `C002-L2` 在抓取和结构化提取成功后，`match-observed-jobs` 连续三次 `invalid_tool_input`；`C015-L3` 已产出 `job_matching_report`，但下游 `build-preparation-plan` 连续 `target_evidence_not_found`；评测 JSON 又没有原始 payload，因此当前只能确定“指针/字段链断裂”，不能确定具体传了哪个 ID（见报告 [`:49-50`](D:/Program%20Files/JetBrains/PyCharm%20Community%20Edition%202024.2.2/proj/langgraph-multi-agent-career-assistant-main/docs/83-question-full-eval-report-2026-08-12.md:49)）。

**修复顺序**：第一优先级。先将 schema 字段错误、artifact 指针类型和 source/content hash 关系纳入 trace；再做输入归一化、下游 ID 解析和契约回归。理由是它确定性高、可离线重放、对成功率杠杆最大。

### P1：Verifier 质量门槛与 Executor 无进展控制没有共享“新信息”信号

**证据**：已有报告记录“聚合级报告被 Verifier 拒收 → 相同调用被 duplicate 去重 → stall”；当前 Runtime 虽有重复调用去重、三次 stall、RETRY/REPLAN 和同构重规划保护，但这些是终止/限流机制，不等于判断下一次调用是否产生了新的证据。

**修复顺序**：第二优先级。为每次 retry 计算 artifact/content hash、输入指纹和可验证字段的变化；只有产生新证据或结构化差异才允许 retry，否则直接进入 replan/人工。把 Verifier 反馈转为结构化缺口（缺哪个 artifact、哪个字段、哪个质量阈值），避免只传自然语言反馈。

### P2：公网证据可用性波动与长上下文/模型接口波动混在同一成功率里

**证据**：83 题两轮结果为 34/49/0 和 30/52/1；大量 `public_page_content_insufficient` 发生在匹配前，另有 `model_request_failed`；报告还记录一条链累计 `input_tokens=134,398`，但明确不能据此断言单次 context overflow（见 [`:52-69`](D:/Program%20Files/JetBrains/PyCharm%20Community%20Edition%202024.2.2/proj/langgraph-multi-agent-career-assistant-main/docs/83-question-full-eval-report-2026-08-12.md:52)）。

**修复顺序**：第三优先级，且必须在 P0/P1 之后验证。把“缓存证据重放”“固定低并发 live smoke”“外部阻断”三种结果分开；以域名、渲染模式、并发和 wall time 做分层监控。只有同一簇在缓存重放中仍失败，才升级为代码/上下文问题；只在 live 失败则归为环境基线，不用业务修复掩盖站点限制。

## 9. 本次只读边界

本次没有修改任何 Python、前端、配置、数据库迁移或测试实现。仅新增本方案的 Markdown 规划/证据文件；仓库原有未提交改动保持不动。上述 `output/observability/...`、`regression_manifest.json` 和监控器均为后续实现目标，不声称已存在或已运行。

## 10. 最小下一步

先实现第 1 轮的只读 collector 和 `case_trace` 重建器，使用现有 `tests/question/eval_results` 做离线回放；在补充任何业务修复前，确认 83 题中每个非成功 case 都能归入 `hard_failure`、`external_blocked` 或 `trace_incomplete`，并能在 3 分钟 tick 中重算出相同计数。
