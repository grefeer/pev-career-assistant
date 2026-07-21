# Discovery Supervisor Agent 重构：策略路由 + 轨迹辅助策略迭代

> 日期：2026-07-20
> 状态：设计已确认，待实现
> 关联文档：[[2026-07-18-job-discovery-agent-design]], [[2026-07-20-discovery-supervisor-agent-architecture]]

## 1. 动机

### 当前问题

| 痛点 | 表现 |
|------|------|
| 速度可优化 | 微信文章任务 3-4 分钟（含 LLM 规划），同类 URL 每次重复规划 |
| 重复探索 | 同类 URL（同一公众号、同一 SPA 站点）每次都要 LLM 从头规划 |
| SPA 快捷方式不可扩展 | 阿里系硬编码在 deepagents_runner.py 中（~8 秒），新增站点需要改代码 |
| 代码膨胀 | deepagents_runner.py 2106 行，含 agent 构建、20 个工具、HTTP 抓取、XHR 拦截、阿里 API 解析等 |

### 根因分析

多数已知 URL 类型的任务中，Supervisor Agent 的 LLM 规划没有产生有价值的决策——工具调用序列是确定性的（微信文章：triage → open_url → parse_wechat → extract_jd → verify → package）。但每次都要为这个"伪规划"付费（tokens + 延迟）。

> **性能拆解**：当前 WeChat 路径 3-4 分钟的总耗时中，Supervisor LLM 规划往返约占 20-40 秒（5-8 轮工具选择决策）；`extract_jd_candidates` 和 `verify_evidence` 内部 LLM 调用各占 30-60 秒，这部分是真正的业务逻辑，无论走哪条路径都无法消除。因此 SnapshotExecutor 的预期收益是省掉 Supervisor 规划税，WeChat 快路径预期 **2-3 分钟**（而非秒级）。纯 Adapter 快车道（如阿里 SPA API 直调，完全绕开 LLM）才能达到 ~8 秒。

## 2. 核心思路

**策略路由匹配 → 快速执行（确定性）→ 失败回退 Supervisor Agent（LLM）→ 轨迹入库标注（LLM 小模型，按需触发）。**

```
          已知路径                            未知/失败路径
─────────────────────────────────┬────────────────────────────────
  Adapter / SnapshotExecutor     │   Supervisor Agent (deepagents)
  无 Supervisor LLM 规划，确定性  │   有 LLM，智能，慢但灵活
```

## 3. 架构

```
                          ┌─────────────────────────┐
                          │   JobDiscoveryWorker     │
                          └───────────┬─────────────┘
                                      │ task
                                      ▼
                          ┌─────────────────────────┐
                          │   StrategyRouter (NEW)   │
                          │   URL → Pattern Match    │
                          └───────────┬─────────────┘
                                      │
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
   │ 命中 + adapter   │   │ 命中 + 无adapter │   │    未命中        │
   │ (快车道)         │   │ (快路径)         │   │  (回退路)        │
   └────────┬─────────┘   └────────┬─────────┘   └────────┬─────────┘
            │                      │                       │
            ▼                      ▼                       ▼
   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
   │ DomainAdapter    │   │ SnapshotExecutor │   │ SupervisorAgent  │
   │ .execute()       │   │ 按 YAML plan 回放│   │ 自主规划+执行    │
   │ 实时写 Trajectory │   │ 实时写 Trajectory │   │ (现有流程)       │
   │ Buffer           │   │ Buffer           │   │                  │
   └────────┬─────────┘   └────────┬─────────┘   └────────┬─────────┘
            │                      │                       │
            │  步骤失败              │  步骤失败              │
            │  ▼                    │  ▼                    │
            ├──── Supervisor 接管 ──┴──── Supervisor 接管    │
            │  (注入 snapshot_context)                      │
            │                                              │
            └──────────────────────┬───────────────────────┘
                                   │ 结果 (DiscoveryRunResult)
                                   ▼
                          ┌─────────────────────────┐
                          │  TrajectoryAnnotator    │
                          │  LLM 标注 (小模型)       │
                          │  仅回退路 + 高复用潜力   │
                          │  轨迹触发               │
                          └───────────┬─────────────┘
                                      │
                                      ▼
                          ┌─────────────────────────┐
                          │  MySQL 持久化             │
                          │  - trajectories (轨迹)   │
                          │  - strategies (策略状态)  │
                          │  - evidence + candidates  │
                          │    (现有表，不变)          │
                          └─────────────────────────┘
```

## 4. 三条执行路径

| 路径 | 触发 | 执行方式 | Supervisor LLM | 典型耗时 |
|------|------|---------|---------------|---------|
| **快车道** | 策略命中 + adapter 存在 | DomainAdapter 代码执行 | 0 次 | ~8 秒（阿里 SPA） |
| **快路径** | 策略命中 + 无 adapter | SnapshotExecutor 回放（工具内 LLM 仍会调用） | 0 次 | ~2-3 分钟（微信） |
| **回退路** | 未命中 / 快照失败 | Supervisor Agent | 6-12 次 | 3-5 分钟 |
| **标注路** | 仅回退路 + 高复用潜力轨迹 | TrajectoryAnnotator | 1 次（小模型） | ~2-5 秒 |

快车道和快路径步骤失败时，均以 snapshot_context 形式注入 Supervisor Agent，从断点继续。Supervisor 也失败时 → `needs_manual_review`。

**说明**：快路径（SnapshotExecutor）省掉的是 Supervisor 的规划 LLM 往返（工具选择决策），但 `extract_jd_candidates`、`verify_evidence` 等工具内部的 LLM 调用仍然存在，因此耗时从 3-4 分钟降至 2-3 分钟，而非秒级。只有快车道（DomainAdapter，如阿里 SPA API 直调）才能完全绕开 LLM 达到秒级。

## 5. 策略库设计

### 5.1 策略快照格式 (YAML)

```yaml
id: "wechat-article-standard"
url_pattern: "mp.weixin.qq.com/s/*"
site_type: "wechat"
description: "微信公众号文章 → 文本提取 → JD 提取"
enabled: true
priority: 10
adapter: null                     # 可选: "adapters.wechat.WechatAdapter"

plan:
  - tool: open_url
    params:
      url: "{{task.url}}"
    expect: "获取文章 HTML"
    on_error: "retry_with_fallback"

  - tool: parse_wechat_article
    params:
      html: "{{prev.result.html}}"
      url: "{{task.url}}"
    expect: "提取文章正文"
    on_error: "mark_manual_review"

  - tool: extract_jd_candidates
    params:
      page_text: "{{prev.result.text}}"
      url: "{{task.url}}"
    expect: "结构化 JD 列表"
    on_error: "retry_then_skip"

  - tool: verify_evidence
    params:
      candidates_json: "{{prev.result}}"
      evidence_json: "{{evidence_json}}"
    expect: "验证候选 JD"
    on_error: "skip"

  - tool: package_candidates
    params:
      candidates_json: "{{prev.result}}"
      evidence_hash: "{{task.evidence_hash}}"
      source_key: "{{task.source_key}}"
    expect: "打包最终输出"

meta:
  created: "2026-07-15"
  updated: "2026-07-20"
  source_trajectory_ids: ["tid-001", "tid-015"]
  success_count: 42
  avg_duration_seconds: 150
```

### 5.2 模板变量语法（严格定义）

使用 Python `string.Template` 的安全子集，**禁止任意表达式求值**。运行时变量绑定仅限以下来源：

| 变量 | 来源 | 示例 |
|------|------|------|
| `{{task.url}}` | DiscoveryTaskInput.url | `https://mp.weixin.qq.com/s/abc` |
| `{{task.source_key}}` | DiscoveryTaskInput.source_key | `tencent-xxx` |
| `{{task.evidence_hash}}` | 任务级 evidence hash | `sha256:...` |
| `{{task.record_fields.xxx}}` | RawJobRecord 顶层字段 | `{{task.record_fields.company_name}}` |
| `{{prev.result}}` | 上一步完整输出（JSON 序列化） | `[{"title": "..."}]` |
| `{{prev.result.xxx}}` | 上一步输出的一级字段 | `{{prev.result.text}}` |

**限制规则**：

1. **仅支持一级嵌套访问**：`{{prev.result.html}}` 合法，`{{prev.result.data.html}}` 非法（不支持多级）。需要深层字段时通过上一步工具返回扁平字典。
2. **列表访问不支持**：`{{prev.result.candidates[0]}}` 非法。需要列表元素时在上一步工具中做预处理。
3. **缺失字段行为**：运行时缺失字段 → 替换为 `None`（Python None → JSON null），工具内部必须处理 `None` 入参。不静默替换为空字符串。
4. **不支持的条件/循环**：YAML plan 是纯线性序列，无 `if` / `for` / 流水线控制流。需要分支逻辑的场景应编写 DomainAdapter。
5. **策略入库校验**：策略写入 `strategy_store.py` 时，对 `plan` 中所有 `{{}}` 引用做静态校验：变量名必须在允许列表中，嵌套层级不超过 1，否则拒绝写入。

### 5.3 匹配流程

```
URL 输入 → 规范化(strip query string, 标准化协议)
  → 精确匹配 full_url → 未命中
  → 模式匹配 url_pattern (按 priority 降序)
  → 命中则取最高 priority；多人同时命中同 priority 则取 success_count 最高的
  → 检查 status: active/degraded 可用, unavailable 跳过
```

### 5.4 策略状态机 & 并发安全

```
active ──────────────────────────────► active
  │ （连续 N 次成功）
  │
  │ 单次偶发失败 → error_count += 1
  │ error_count < 3
  ▼
degraded ────────────────────────────► active
  │ （仍可匹配，priority 降低）           （连续 2 次成功后恢复）
  │
  │ 连续失败达到 3 次 threshold
  │ 或：根本性错误（网站改版、接口永久下线）
  ▼
unavailable ─────────────────────────► active
  （不再匹配）                            （人工修复策略后重新启用）
```

**并发安全**：策略状态计数器（`error_count`、`consecutive_ok`、`total_runs`、`success_runs`、`fallback_runs`）通过数据库原子更新操作修改，不使用"读取 → 修改 → 写入"模式：

```python
# strategy_store.py — 原子递增，避免多 Worker 竞态
def increment_error_count(db: Session, strategy_id: str, last_error: dict) -> None:
    db.execute(
        update(JobDiscoveryStrategy)
        .where(JobDiscoveryStrategy.id == strategy_id)
        .values(
            error_count=JobDiscoveryStrategy.error_count + 1,
            last_error_tool=last_error["tool"],
            last_error_reason=last_error["reason"],
            last_error_message=last_error["message"],
            last_error_at=func.now(),
            # 原子判断阈值
            status=case(
                (JobDiscoveryStrategy.error_count + 1 >= JobDiscoveryStrategy.degradation_threshold,
                 "unavailable"),
                else_=JobDiscoveryStrategy.status,
            ),
        )
    )

def increment_success_count(db: Session, strategy_id: str) -> None:
    db.execute(
        update(JobDiscoveryStrategy)
        .where(JobDiscoveryStrategy.id == strategy_id)
        .values(
            success_runs=JobDiscoveryStrategy.success_runs + 1,
            total_runs=JobDiscoveryStrategy.total_runs + 1,
            consecutive_ok=JobDiscoveryStrategy.consecutive_ok + 1,
            error_count=0,  # 成功后重置连续失败计数
            # 从 degraded 恢复
            status=case(
                (and_(
                    JobDiscoveryStrategy.status == "degraded",
                    JobDiscoveryStrategy.consecutive_ok + 1 >= JobDiscoveryStrategy.degradation_threshold,
                ), "active"),
                else_=JobDiscoveryStrategy.status,
            ),
        )
    )
```

`strategy_store.py` 不提供"读取 status 字段 → 应用层判断 → 写回"的接口，所有状态转换均通过原子 SQL 完成。

### 5.5 策略健康检查（回归检测）

为避免策略随站点改版静默失效，实现周期性健康检查：

```python
# strategy_store.py
def get_strategies_due_for_health_check(db: Session, interval_hours: int = 24) -> list[JobDiscoveryStrategy]:
    """返回距上次健康检查超过 interval_hours 的 active/degraded 策略。"""
    ...

def record_health_check_result(db: Session, strategy_id: str, ok: bool, detail: str) -> None:
    """记录健康检查结果。失败时递增 error_count（通过原子更新）。"""
    ...
```

- `JobDiscoveryWorker` 在队列空闲时（`run_once()` 返回 0），以低频率（每 10 个空闲周期一次）对到期策略做 dry-run：打开 URL → 检查响应状态/关键 DOM 元素 → 不执行完整 JD 提取
- 健康检查失败触发 §5.4 的原子错误递增，连续 3 次失败自动标记 unavailable
- 不做完整 JD 提取以节省 LLM token；仅验证页面可达性和结构完整性
- 检查频率通过 `config.py` → `strategy_health_check_interval_hours: int = 24` 控制

## 6. SnapshotExecutor

### 6.1 执行模型

按策略 YAML 中的 `plan` 步骤逐条执行。步骤参数使用 `{{}}` 模板变量（语法见 §5.2），运行时用 `string.Template` 的安全子类替换（不依赖 LLM）。调用和 Supervisor Agent 同一批 tool 函数。

执行伪代码：

```python
def execute_snapshot(strategy: StrategyRecord, task: DiscoveryTaskInput,
                     trajectory: TrajectoryBuffer) -> DiscoveryRunResult:
    context = {"task": task, "prev": None}
    completed_steps = []
    for step in strategy.plan:
        params = _resolve_template(step.params, context)   # §5.2 语法
        try:
            result = _call_tool(step.tool, **params)       # 同 Supervisor 的 tool 函数
            context["prev"] = {"result": result}
            completed_steps.append({"tool": step.tool, "params": params, "ok": True})
            trajectory.record_step(step.tool, "ok", params, result)
        except Exception as exc:
            trajectory.record_step(step.tool, "failed", params, None, error=exc)
            snapshot_context = _build_snapshot_context(completed_steps, step, strategy.id)
            # 回退 Supervisor，见 §7
            return run_supervisor_with_context(task, snapshot_context)
    return _build_result(completed_steps)
```

### 6.2 失败处理

```python
步骤失败时：
  1. 保存当前轨迹（含已成功的步骤）到 TrajectoryBuffer
  2. 构建 snapshot_context = {
       completed_steps: [...],
       failed_step: {...},
       source: "snapshot" | "adapter",
       strategy_id: "..."
     }
  3. 将 snapshot_context 注入 Supervisor Agent（Prompt 格式见 §9.2）
  4. Supervisor 从断点接管，重新规划执行
  5. 最终结果正常返回
```

## 7. DomainAdapter 接口

```python
class DomainAdapter(ABC):
    """域名适配器基类。为特定域名/站点提供最优执行路径。"""

    url_pattern: str

    @abstractmethod
    def execute(self, task: DiscoveryTaskInput, strategy: StrategyRecord,
                trajectory: TrajectoryBuffer) -> DiscoveryRunResult:
        """执行职位发现。每完成一步写入 trajectory。失败时 TrajectoryBuffer 中已有部分轨迹。"""
        ...

    @abstractmethod
    def validate(self, url: str) -> bool:
        """快速校验 URL 是否仍然有效/可访问。用于策略过期检测。"""
        ...
```

现有阿里 SPA 快捷方式从 `deepagents_runner.py` 迁移为 `adapters/alibaba_spa.py` 的 `AlibabaSPAAdapter`。adapter 和 snapshot 共用 `TrajectoryBuffer` 写入错误/断点信息，失败处理完全对称。

**从 Snapshot 到 Adapter 的演进路径**（人工操作）：当一个站点的 SnapshotExecutor 轨迹积累显示明确的 API 调用模式（如 XHR 拦截发现内部 JSON API），且回退率低 → 人工编写 DomainAdapter 替代 Snapshot → 更新策略 YAML 的 `adapter` 字段 → 策略进入快车道（0 次 LLM，秒级）。此过程不在自动化闭环内。

## 8. 轨迹标注 & 存储

### 8.1 标注触发策略（按需，非全量）

TrajectoryAnnotator 不针对所有轨迹运行。仅在以下条件**同时满足**时触发：

1. **执行路径为回退路**（`executor_type = 'supervisor'`，即未命中策略或 Snapshot/Adapter 失败后接管）；或 **SnapshotExecutor 步骤失败且触发了 Supervisor 接管**
2. **轨迹包含至少一个完整的成功工具调用链**（空轨迹不标注）

快车道和快路径的完全成功执行**不触发标注**——它们的干净路径已知且无增量信息。

触发条件在 `worker.py` 中实现：

```python
# worker.py — run_once() 末尾
if result.executor_type == "supervisor" or result.status == "partial_fallback":
    trajectory_store.schedule_annotation(result.trajectory_id)
```

### 8.2 标注流水线

TrajectoryAnnotator 使用小模型（deepseek-v4-flash，和现有同款），对符合条件的原始轨迹进行结构化标注：

1. **识别重试循环** — 标记 `[RETRY_LOOP]` 起止 + 原因
2. **识别错误步骤** — 标记 `[ERROR]` + 分类原因（分类见 §8.3）
3. **提取干净路径** — 去除重试/错误后保留的成功步骤链
4. **总结关键决策点** — 如"第 3 步选择 OCR 而非文本提取，因为页面是纯图片"
5. **评估可复用性** — reusability_score 0~1，预测是否适合提炼为策略

### 8.3 错误分类

`error_classifier.py` 将原始错误归为固定类别：

```
network_timeout | http_blocked | captcha | wechat_blocked |
site_changed | empty_text | parse_error | ocr_failed | unknown
```

### 8.4 数据库 Schema

#### job_discovery_trajectories（每次执行一行）

| 列 | 类型 | 说明 |
|----|------|------|
| id | UUID PK | |
| task_id | FK → tasks | |
| strategy_id | FK → strategies (nullable) | 未命中时为 NULL |
| executor_type | VARCHAR(20) | adapter / snapshot / supervisor |
| overall_status | VARCHAR(30) | completed / partial_fallback / failed |
| failed_at_step | INT (nullable) | 第几步失败 |
| failed_tool | VARCHAR(100) (nullable) | 失败时的工具名 |
| failed_params | JSON (nullable) | 失败时的入参 |
| failed_error_type | VARCHAR(100) (nullable) | Exception 类名 |
| failed_error_message | TEXT (nullable) | 错误信息 |
| failed_error_reason | VARCHAR(50) (nullable) | 错误分类（§8.3） |
| completed_steps | JSON | 失败前已成功的步骤 |
| fallback_trace | JSON (nullable) | Supervisor 接管后的步骤 |
| clean_path | JSON | 标注后的干净路径 |
| annotations | JSON | **LLM 语义标注**（retry_loops, decisions, reusability_score） |
| url | TEXT | |
| url_pattern | VARCHAR(500) INDEXED | |
| created_at | DATETIME INDEXED | |

> **列设计说明**：`failed_*` 列与 `annotations` JSON 分工明确——`failed_*` 列是确定性字段（直接从异常对象提取，无 LLM 参与），支持高效 SQL 聚合查询（`SELECT failed_error_reason, COUNT(*) ... GROUP BY failed_error_reason`）；`annotations` JSON 存储 LLM 生成的语义标注（重试循环分析、决策总结、复用评分），仅供人工审查时阅读。两者不冗余。

#### job_discovery_strategies（每条策略一行）

| 列 | 类型 | 说明 |
|----|------|------|
| id | UUID PK | |
| url_pattern | VARCHAR(500) NOT NULL INDEXED | 匹配模式 |
| site_type | VARCHAR(50) INDEXED | wechat / career_portal / spa / other |
| description | TEXT | |
| priority | INT DEFAULT 0 | 同 pattern 命中多条时的优先级 |
| adapter | VARCHAR(500) (nullable) | 适配器代码路径 |
| plan_yaml | MEDIUMTEXT NOT NULL | YAML 执行计划 |
| status | VARCHAR(20) DEFAULT 'active' | active / degraded / unavailable |
| enabled | BOOLEAN DEFAULT TRUE | 开关 |
| total_runs | INT DEFAULT 0 | |
| success_runs | INT DEFAULT 0 | |
| fallback_runs | INT DEFAULT 0 | 策略失败、Supervisor 补救 |
| error_count | INT DEFAULT 0 | 连续失败次数 |
| consecutive_ok | INT DEFAULT 0 | 连续成功次数 |
| last_error_tool | VARCHAR(100) (nullable) | |
| last_error_reason | VARCHAR(50) (nullable) | |
| last_error_message | TEXT (nullable) | |
| last_error_at | DATETIME (nullable) | |
| success_count | INT DEFAULT 0 | |
| avg_duration_s | FLOAT (nullable) | |
| degradation_threshold | INT DEFAULT 3 | 连续失败 N 次标记 unavailable |
| last_health_check_at | DATETIME (nullable) | 最近一次健康检查时间（§5.5） |
| created_at | DATETIME | |
| updated_at | DATETIME | |

## 9. Supervisor Agent 改动

三处轻量改动，核心逻辑不变：

1. **接受可选 `snapshot_context`**：有上下文时从断点继续，参考 suggested_action，可选用更好的方案。Prompt 拆分为三个模板文件（base / clean_start / snapshot_fallback），按规则拼接（见 §9.2）。

2. **工具调用上限动态调整**：有 snapshot_context → 8 次，无 → 12 次（现有值）。

3. **Prompt 文件化**：`prompts/supervisor_base.txt`、`prompts/supervisor_clean_start.txt`、`prompts/supervisor_snapshot_fallback.txt`，不再放在 `deepagents_runner.py` 的超长字符串中。

不改动：10 个 Supervisor 工具、Web Navigation SubAgent、所有 tool 函数签名和返回格式。

### 9.1 Prompt 模板职责

| 模板文件 | 职责 | 始终加载 |
|---------|------|---------|
| `supervisor_base.txt` | 角色定义、工具列表、输出格式、安全约束 | ✅ 是 |
| `supervisor_clean_start.txt` | "你是初次执行任务，请从 triage_link 开始规划完整流程" | 仅无 snapshot_context 时 |
| `supervisor_snapshot_fallback.txt` | "以下步骤已由 SnapshotExecutor 完成…第 N 步失败…请从断点继续" | 仅有 snapshot_context 时 |

### 9.2 Prompt 拼接规则

```python
def build_supervisor_prompt(snapshot_context: dict | None) -> str:
    """组装 Supervisor system prompt。"""
    parts = [load_prompt("supervisor_base.txt")]

    if snapshot_context is None:
        parts.append(load_prompt("supervisor_clean_start.txt"))
    else:
        # 注入断点上下文
        fallback_template = load_prompt("supervisor_snapshot_fallback.txt")
        ctx = {
            "completed_steps": _format_steps(snapshot_context["completed_steps"]),
            "failed_step_tool": snapshot_context["failed_step"]["tool"],
            "failed_step_params": json.dumps(snapshot_context["failed_step"]["params"], ensure_ascii=False),
            "failed_step_error": str(snapshot_context["failed_step"].get("error", "")),
            "source": snapshot_context["source"],
            "strategy_id": snapshot_context["strategy_id"],
        }
        parts.append(fallback_template.format(**ctx))

    return "\n\n".join(parts)
```

`supervisor_snapshot_fallback.txt` 的关键内容：

```
## 当前状态：从断点继续

以下步骤已由 {source}（策略 {strategy_id}）成功执行，你无需重复：

{completed_steps}

第 {failed_step_count} 步执行失败：
- 工具：{failed_step_tool}
- 入参：{failed_step_params}
- 错误：{failed_step_error}

请从该断点继续执行。你可以：
1. 重试失败步骤（如果错误是临时性的）
2. 选择替代工具链跳过该步骤
3. 如果无法恢复，调用 finish_with_manual_review

注意：已完成步骤的输出结果已保存在上下文中，你可以直接引用。不要重复执行已成功的步骤以节省 token 和时间。
```

两个模板文件（`clean_start` / `snapshot_fallback`）是互斥的，不存在同时拼接两个的情况。拼接后的 token 总量适应模型上下文窗口（DeepSeek v4 128K，实际 prompt < 4K tokens）。

## 10. 文件结构变更

### 新增文件

```
backend/app/services/job_discovery/
  strategy/
    __init__.py
    strategy_store.py         # 策略 CRUD + 原子状态更新
    strategy_router.py        # URL 匹配 + 路由
    snapshot_executor.py      # 快照回放执行器
    trajectory_buffer.py      # adapter/snapshot 共用轨迹缓冲区
    trajectory_store.py       # 轨迹入库 + 标注调度
    trajectory_annotator.py   # LLM 标注轨迹（按需触发）
    error_classifier.py       # 错误分类器
  adapters/
    __init__.py
    base.py                   # DomainAdapter ABC
    alibaba_spa.py            # 阿里 SPA 适配器（从 deepagents_runner.py 迁移）
  prompts/
    supervisor_base.txt
    supervisor_clean_start.txt
    supervisor_snapshot_fallback.txt
```

### 改动文件

| 文件 | 程度 | 内容 |
|------|------|------|
| `deepagents_runner.py` | 🟡 中 | 移除阿里 SPA 硬编码；prompt 从文件加载；接受 snapshot_context；Prompt 拼接逻辑 |
| `worker.py` | 🟡 中 | run_once() 加入 StrategyRouter 调用；增加轨迹/策略写入；标注按需调度；空闲时触发策略健康检查 |
| `schemas.py` | 🟢 轻 | 增加 StrategyRecord、AnnotatedTrajectory 等 dataclass |
| `config.py` | 🟢 轻 | 增加 strategy_degradation_threshold、trajectory_retention_days、strategy_health_check_interval_hours 等 |
| `models.py` | 🟢 轻 | 新增 2 张表 ORM 模型 |

### 不改动

`tools/` 全部、Web Nav SubAgent、API routes、repository、`tasks.py`

## 11. 配置

```python
# config.py 新增
job_discovery_strategy_enabled: bool = False       # 策略路由总开关（可随时回退）
strategy_degradation_threshold: int = 3            # 连续失败 N 次标记 unavailable
strategy_recovery_threshold: int = 2               # 连续成功 N 次恢复 active
trajectory_retention_days: int = 90                # 轨迹保留天数
strategy_health_check_interval_hours: int = 24     # 策略健康检查间隔
trajectory_annotation_enabled: bool = True         # 轨迹标注总开关（可独立关闭）
```

## 12. 迁移路径

### Step 1: 建基础设施（不影响现有流程）

- 新增 `strategy/`、`adapters/`、`prompts/` 目录及文件
- 运行 migration 建 `job_discovery_strategies` + `job_discovery_trajectories` 表
- Worker 末尾增加轨迹旁路写入
- **验证**: 现有测试套件全量通过

### Step 2: 策略路由上线 + 种子策略

- StrategyRouter 插入 Worker.run_once() 最前面
- `job_discovery_strategy_enabled = False` 默认，手动开启
- 种子策略 (手动写入 2 条)：
  - `wechat-article-standard` (mp.weixin.qq.com/s/* → SnapshotExecutor)
  - `alibaba-spa` (campus*.alibaba.com/* → AlibabaSPAAdapter)
- **验证**: 全量测试 + 手动用 #1-4 四个 URL 验证两种模式结果一致

### Step 3: 积累轨迹 + 人工迭代策略

- `job_discovery_strategy_enabled = True`
- 运行 1-2 周积累轨迹
- 定期人工审查轨迹库（重点关注 `reusability_score > 0.7` 且 `executor_type = 'supervisor'` 的轨迹）→ 提炼新策略 → 写入策略库
- 对 SnapshotExecutor 策略积累足够的成功轨迹后，人工评估是否可升级为 DomainAdapter（快车道）
- 监控: 策略命中率、SnapshotExecutor 成功率、fallback 率、平均耗时

### Step 4: 清理

- 确认系统稳定 2 周+ 后，移除 deepagents_runner.py 中被 adapter 替代的阿里硬编码逻辑
- 清理 `_run_via_subagent_delegation` 等死代码
- 统一 `_web_nav_*` / `_nav_*` 两套全局状态

## 13. 风险 & 回退

| 风险 | 缓解 |
|------|------|
| 策略路由导致结果劣化 | `job_discovery_strategy_enabled` 环境变量开关，关闭即回到纯 Supervisor |
| 种子策略有问题 | 种子策略手动审核后才写入；策略步骤失败自动回退 Supervisor |
| 网站改版导致策略大面积失效 | 连续 3 次失败自动标记 unavailable + 定期健康检查（§5.5）提前发现 |
| LLM 标注质量不足 | 标注仅影响轨迹可读性和后续人工提炼，不影响实时执行结果；可独立关闭 |
| 多 Worker 并发修改策略状态 | 所有计数器通过数据库原子 UPDATE 修改（§5.4），无竞态 |
| SnapshotExecutor 模板变量缺失 | 策略入库时静态校验变量合法性（§5.2），运行时缺失字段传 `None` |

## 14. 预期效果

| 指标 | 当前 | 预期 |
|------|------|------|
| 已知路径 Supervisor LLM 规划 | 5-8 轮往返（20-40 秒） | 0 轮（省掉规划税） |
| Supervisor token 消耗 | 所有任务都付 LLM 税 | 仅未命中/失败任务 |
| WeChat 快路径总耗时 | 3-4 分钟 | 2-3 分钟（工具内 LLM 仍占大头） |
| 阿里 SPA 耗时 | ~8 秒（硬编码） | ~8 秒（Adapter，可扩展） |
| 新增站点成本 | 无积累、每次从头探索 | 首次探索后入轨迹库，人工提炼策略 |
| 调试可追溯性 | LLM 黑盒 | 轨迹库含每步输入/输出/错误分类 |
| 策略扩展方式 | 改代码、加 hardcode | 写 YAML 策略或 adapter 类 |
