# Discovery Supervisor Agent 重构：策略路由 + 轨迹自进化

> 日期：2026-07-20
> 状态：设计已确认，待实现
> 关联文档：[[2026-07-18-job-discovery-agent-design]], [[2026-07-20-discovery-supervisor-agent-architecture]]

## 1. 动机

### 当前问题

| 痛点 | 表现 |
|------|------|
| 速度极慢 | 微信文章任务 4.3 小时（预期 3-4 分钟），大量时间消耗在 LLM 规划 |
| 重复探索 | 同类 URL（同一公众号、同一 SPA 站点）每次都要 LLM 从头规划 |
| SPA 快捷方式不可扩展 | 阿里系硬编码在 deepagents_runner.py 中（~8 秒），新增站点需要改代码 |
| 代码膨胀 | deepagents_runner.py 2106 行，含 agent 构建、20 个工具、HTTP 抓取、XHR 拦截、阿里 API 解析等 |

### 根因分析

80-90% 的任务中，Supervisor Agent 的 LLM 规划没有产生有价值的决策——工具调用序列是确定性的（微信文章：triage → open_url → parse_wechat → extract_jd → verify → package）。但每次都要为这个"伪规划"付费（tokens + 延迟）。

## 2. 核心思路

**策略路由匹配 → 快速执行（确定性）→ 失败回退 Supervisor Agent（LLM）→ 轨迹入库标注（LLM 小模型）。**

```
          已知路径（>80%）                 未知/失败路径（<20%）
─────────────────────────────────┬────────────────────────────────
  Adapter / SnapshotExecutor     │   Supervisor Agent (deepagents)
  无 LLM，确定性，秒级            │   有 LLM，智能，慢但灵活
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
                          │  - 重试循环 / 错误 /    │
                          │    干净路径 / 决策 / 分数 │
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

| 路径 | 触发 | 执行方式 | LLM 调用 | 典型耗时 |
|------|------|---------|---------|---------|
| **快车道** | 策略命中 + adapter 存在 | DomainAdapter 代码执行 | 0 次 | ~8 秒（阿里 SPA） |
| **快路径** | 策略命中 + 无 adapter | SnapshotExecutor 回放 | 0 次（成功时） | ~30-60 秒（微信） |
| **回退路** | 未命中 / 快照失败 | Supervisor Agent | 6-12 次 | 3-5 分钟 |
| **标注路** | 所有路径结束后 | TrajectoryAnnotator | 1 次（小模型） | ~2-5 秒 |

快车道和快路径步骤失败时，均以 snapshot_context 形式注入 Supervisor Agent，从断点继续。Supervisor 也失败时 → `needs_manual_review`。

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
  avg_duration_seconds: 45
```

### 5.2 匹配流程

```
URL 输入 → 规范化(strip query string, 标准化协议)
  → 精确匹配 full_url → 未命中
  → 模式匹配 url_pattern (按 priority 降序)
  → 命中则取最高 priority；多人同时命中同 priority 则取 success_count 最高的
  → 检查 status: active/degraded 可用, unavailable 跳过
```

### 5.3 策略状态机

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

## 6. SnapshotExecutor

### 6.1 执行模型

按策略 YAML 中的 `plan` 步骤逐条执行。步骤参数使用 `{{}}` 模板变量，运行时替换（不依赖 LLM）。调用和 Supervisor Agent 同一批 tool 函数。

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
  3. 将 snapshot_context 注入 Supervisor Agent
  4. Supervisor 从断点接管，重新规划执行
  5. 最终结果正常返回
```

### 6.3 模板变量

支持以下变量，运行时不依赖 LLM，用 string.Template 替换：

| 变量 | 来源 |
|------|------|
| `{{task.url}}` | DiscoveryTaskInput.url |
| `{{task.source_key}}` | DiscoveryTaskInput.source_key |
| `{{task.evidence_hash}}` | 任务级 evidence hash |
| `{{task.record_fields.xxx}}` | RawJobRecord 字段 |
| `{{prev.result}}` | 上一步的完整输出 |
| `{{prev.result.xxx}}` | 上一步输出的特定字段 |

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

## 8. 轨迹标注 & 存储

### 8.1 标注流水线

TrajectoryAnnotator 使用小模型（deepseek-v4-flash，和现有同款），对原始轨迹进行结构化标注：

1. **识别重试循环** — 标记 `[RETRY_LOOP]` 起止 + 原因
2. **识别错误步骤** — 标记 `[ERROR]` + 分类原因
3. **提取干净路径** — 去除重试/错误后保留的成功步骤链
4. **总结关键决策点** — 如"第 3 步选择 OCR 而非文本提取，因为页面是纯图片"
5. **评估可复用性** — reusability_score 0~1，预测是否适合提炼为策略

### 8.2 错误分类

`error_classifier.py` 将原始错误归为固定类别：

```
network_timeout | http_blocked | captcha | wechat_blocked |
site_changed | empty_text | parse_error | ocr_failed | unknown
```

### 8.3 数据库 Schema

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
| failed_error_reason | VARCHAR(50) (nullable) | 错误分类 |
| completed_steps | JSON | 失败前已成功的步骤 |
| fallback_trace | JSON (nullable) | Supervisor 接管后的步骤 |
| clean_path | JSON | 标注后的干净路径 |
| annotations | JSON | 标注结果（retry_loops, errors, decisions, score） |
| url | TEXT | |
| url_pattern | VARCHAR(500) INDEXED | |
| created_at | DATETIME INDEXED | |

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
| created_at | DATETIME | |
| updated_at | DATETIME | |

## 9. Supervisor Agent 改动

三处轻量改动，核心逻辑不变：

1. **接受可选 `snapshot_context`**：有上下文时从断点继续，参考 suggested_action，可选用更好的方案。Prompt 拆分为三个模板文件（base / clean_start / snapshot_fallback），按情况拼接。

2. **工具调用上限动态调整**：有 snapshot_context → 8 次，无 → 12 次（现有值）。

3. **Prompt 文件化**：`prompts/supervisor_base.txt`、`prompts/supervisor_clean_start.txt`、`prompts/supervisor_snapshot_fallback.txt`，不再放在 `deepagents_runner.py` 的超长字符串中。

不改动：10 个 Supervisor 工具、Web Navigation SubAgent、所有 tool 函数签名和返回格式。

## 10. 文件结构变更

### 新增文件

```
backend/app/services/job_discovery/
  strategy/
    __init__.py
    strategy_store.py         # 策略 CRUD
    strategy_router.py        # URL 匹配 + 路由
    snapshot_executor.py      # 快照回放执行器
    trajectory_buffer.py      # adapter/snapshot 共用轨迹缓冲区
    trajectory_store.py       # 轨迹入库
    trajectory_annotator.py   # LLM 标注轨迹
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
| `deepagents_runner.py` | 🟡 中 | 移除阿里 SPA 硬编码；prompt 从文件加载；接受 snapshot_context |
| `worker.py` | 🟡 中 | run_once() 加入 StrategyRouter 调用；增加轨迹/策略写入 |
| `schemas.py` | 🟢 轻 | 增加 StrategyRecord、AnnotatedTrajectory 等 dataclass |
| `config.py` | 🟢 轻 | 增加 strategy_degradation_threshold、trajectory_retention_days 等 |
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

### Step 3: 积累轨迹 + 迭代策略

- `job_discovery_strategy_enabled = True`
- 运行 1-2 周积累轨迹
- 定期人工审查轨迹库 → 提炼新策略 → 写入策略库
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
| 网站改版导致策略大面积失效 | 连续 3 次失败自动标记 unavailable，不影响整体可用性 |
| LLM 标注质量不足 | 标注仅影响轨迹可读性和后续人工提炼，不影响实时执行结果 |

## 14. 预期效果

| 指标 | 当前 | 预期 |
|------|------|------|
| 已知路径耗时 | 3-5 分钟（含 LLM 规划） | ~8-60 秒（纯确定性） |
| Supervisor token 消耗 | 所有任务都付 LLM 税 | 仅未命中/失败任务（<20%） |
| 阿里 SPA 扩展 | 改代码、加 hardcode | 写 YAML 策略或 adapter 类 |
| 新增站点成本 | 无积累、每次从头探索 | 首次探索后入轨迹库，人工提炼策略 |
| 调试可追溯性 | LLM 黑盒 | 轨迹库含每步输入/输出/错误分类 |
