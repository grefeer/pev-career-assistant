# Job Discovery Agent — 运维指南

> 本文档说明如何启动后端、前端、发现 Worker，以及配置项参考和常见问题排查。

---

## 1. 启动顺序

### 1.1 基础设施（Docker）

```powershell
docker compose up -d mysql redis minio
```

| 服务 | 端口 | 用途 |
|---|---|---|
| MySQL | 3307 | 持久化任务、候选、审核数据 |
| Redis | 6380 | Worker 任务队列锁 |
| MinIO | 19000 | 证据文件（截图、OCR 结果）存储 |

### 1.2 后端

```powershell
# 激活虚拟环境
.\.venv\Scripts\activate

# 确保 .env 包含必要配置
# 主要变量见下方配置参考

# 数据库迁移
alembic upgrade head

# 启动后端 API
python -m uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```

健康检查端点：`http://127.0.0.1:8000/api/health/live`

### 1.3 发现 Worker

```powershell
# 单独启动 Worker（轮询任务队列）
python -c "
from backend.app.db import SessionLocal
from backend.app.config import get_settings
from backend.app.services.job_discovery.worker import JobDiscoveryWorker

worker = JobDiscoveryWorker(SessionLocal, get_settings())
worker.run_loop(poll_interval=10.0)
"
```

Worker 日志会输出到 stdout。首次启动前确认 `job_discovery_enabled=true`。也可用 `worker.run_once()` 单次运行。

### 1.4 前端

```powershell
cd frontend
npm install
npm run dev
```

前端默认在 `http://127.0.0.1:5173`。管理端路径：`/admin/job-discovery`

## 2. 配置参考

所有配置项在 `backend/app/config.py` 的 `Settings` 类中：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `job_discovery_enabled` | `false` | 是否启用发现功能（Worker 启动前设为 `true`） |
| `job_discovery_agent_version` | `"1.0.0"` | Agent 版本，影响任务幂等 key |
| `job_discovery_model` | `"deepseek-v4-flash"` | Agent 使用的 LLM 模型 |
| `job_discovery_max_pages_per_task` | `20` | 每个任务最大浏览页数（1-100） |
| `job_discovery_max_candidates_per_task` | `10` | 每个任务最大候选数（1-50） |
| `job_discovery_task_timeout_seconds` | `600` | 任务 lease 超时（30-3600s） |
| `job_discovery_browser_headless` | `true` | 浏览器是否为无头模式（预留，暂未集成浏览器） |
| `job_discovery_ocr_enabled` | `false` | 是否启用 OCR（需配置 OCR 服务） |

PEV 灰度迁移 flags（默认全灰：PEV 关、legacy 开）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `job_discovery_strategy_enabled` | `false` | 是否查询 Strategy Router |
| `job_discovery_pev_enabled` | `false` | 启用 PEV（PATH A / PATH B）；关闭时 `CompleteCrawlAdapter` 站点回退 legacy |
| `job_discovery_planner_enabled` | `false` | 启用 PATH C planner agent（生成 / 修复 CrawlPlan） |
| `job_discovery_legacy_path_c_enabled` | `true` | 允许 Legacy Supervisor 作为覆盖率未验证兜底 |
| `job_discovery_planner_max_inspection_pages` | `3` | planner 巡检页预算（1-5） |

执行路径与 PEV PASS 定义见 `backend/app/services/job_discovery/README.md`。

智能表相关配置（通过 `.env` 设置）：

| 变量 | 说明 |
|---|---|
| `TENCENT_DOCS_TOKEN` | 腾讯文档 API 令牌（User scope） |
| `TEST_TENCENT_DOCS_TOKEN` | 测试环境的腾讯文档令牌 |

## 3. API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/job-discovery/tasks` | 列出发现任务（可传 `?status=queued` 过滤） |
| GET | `/admin/job-discovery/groups` | 列出审核分组（按相似度 key 聚合） |
| POST | `/admin/job-discovery/tasks/{id}/retry` | 重试失败/阻塞任务 |
| POST | `/admin/job-discovery/candidates/{id}/approve` | 审批通过候选，自动创建 `JobPosting` |
| POST | `/admin/job-discovery/candidates/{id}/reject` | 拒绝候选 |

所有端点需要 `require_admin` 权限和 `Bearer` token。

## 4. 同步触发

Tencent 智能表同步通过独立端点触发：

```powershell
# 触发 27 届内推同步
curl -X POST "http://127.0.0.1:8000/api/admin/sync/tencent-27-referrals" \
  -H "Authorization: Bearer <token>"

# 触发实习内推同步
curl -X POST "http://127.0.0.1:8000/api/admin/sync/tencent-intern-referrals" \
  -H "Authorization: Bearer <token>"
```

同步完成后自动创建 `JobDiscoveryTask`（如果 `job_discovery_enabled=true`）。

## 5. 数据库表

| 表 | 用途 |
|---|---|
| `job_discovery_tasks` | 发现任务队列 |
| `job_discovery_evidence` | 发现的证据记录 |
| `discovered_job_candidates` | 候选岗位（待审核） |
| `job_sources` | 数据源配置 |
| `raw_job_records` | 同步的原始记录 |

## 6. 常见问题

### 6.1 Worker 不消费任务

检查：
1. `job_discovery_enabled` 是否为 `true`
2. 是否有 `queued` 状态的任务（`GET /admin/job-discovery/tasks?status=queued`）
3. MySQL 和 Redis 是否可达
4. 日志是否有 `claim_next_task` 调用记录

### 6.2 腾讯智能表同步返回 502

可能原因：
- `TENCENT_DOCS_TOKEN` 未设置或已过期
- 智能表结构字段类型发生变化（字段漂移）
- 腾讯 MCP 协议版本不匹配

查看后端日志确认 `tencent_protocol_error` 详情。

### 6.3 候选一直 pending_review

这是正常状态——Agent 只负责发现，审核需要管理员通过管理端操作。管理员进入 `/admin/job-discovery`，切换到"审核分组"标签页审批。

### 6.4 任务一直 running

检查 Worker 是否正在运行。如果 Worker 崩溃，任务的 lease 会在 `job_discovery_task_timeout_seconds`（默认 600s）后过期，其他 Worker 可以重新领取。如果长期 stuck，使用 retry API 重置。

### 6.5 幂等 key 冲突

任务创建使用 `_idempotency_key()` 生成 key，包含 `source_id`、`external_record_id`、`url_hash`、`payload_hash`、`agent_version`。如果修改了 Agent 版本，同一 URL 会创建新任务（不会重复消费同一数据）。

### 6.6 `JOBS_AUTOMATIC_DISCOVERY` 环境变量

旧版部署依赖 `JOBS_AUTOMATIC_DISCOVERY` 环境变量。当前版本统一用 `job_discovery_enabled` 配置项。

## 7. PEV 灰度发布与 live smoke

### 7.1 执行路径

| 路径 | 含义 | 覆盖率 |
|------|------|--------|
| PATH A | 认证站点 adapter（Moka / 飞书 / 汇川 / 小红书 / 阿里 SPA） | 已验证 |
| PATH B | `SnapshotPlan` + `CrawlPlan` 确定性执行器 | 已验证 |
| PATH C | `CrawlPlan` 生成 / 修复 agent | 已验证 |
| Legacy PATH C | Supervisor Agent | **未验证**（兼容兜底） |

CoverageVerifier 是唯一的完成权威。Legacy 结果保存候选但 `coverage-unverified`，
worker summary 以 `execution_path` / `coverage_verified` / `legacy_fallback_reason`
标记，**不计入 PEV pass rate**。

### 7.2 单站启停（灰度）

四个站点 adapter 在 `scripts/seed_strategies.py` 中 `enabled=False` 发布。按
`GRAY_ROLLOUT_ORDER`（Moka -> 飞书 -> 汇川 -> 小红书）逐站启用，每次只切该站
`JobDiscoveryStrategy.enabled=True`：

```sql
-- 启用 Moka（举例）；其余站点保持 enabled=false
UPDATE job_discovery_strategies SET enabled = 1 WHERE url_pattern = 'app.mokahr.com/*';
```

回滚：单站出现计数漂移 / 正向终止字段消失 / `failed_detail_count>0` /
`count_apply_url_is_listpage>0` / 新 blocked marker / 三次计数不一致
（`GRAY_ROLLBACK_TRIGGERS`）时，只把该站 `enabled` 切回 0，不删契约、不改全局
invariant、不影响其他站。

### 7.3 Checkpoint 与 manual review

- PATH B 的 `CrawlExecutor` 从 checkpoint 恢复，不会重复抓已完成详情。
- 微信 snapshot 不进 CrawlPlan agent，硬 deadline 终止 hang。
- 北森 / 会话鉴权墙（401/403 + SPA session auth）稳定返回
  `needs_manual_review`（`permission_denied`），不进 repair / legacy 循环。
- 永不自动点提交；最终提交始终人工控制。

### 7.4 live smoke 命令

```powershell
# 10-URL 评估门禁（Step 9）：直接 supervisor 跨 10 个公开 URL，按 PEV PASS 门禁分桶。
# 缺 gate env 或 DEEPSEEK_API_KEY 时 SKIP（绝不报为 PASS）。
$env:RUN_TEN_URL_EVAL='1'
$env:JOB_DISCOVERY_PEV_ENABLED='1'
.\.venv\Scripts\python.exe tests/integration/job_discovery/test_supervisor_ten_url_eval.py -v

# 单站晋升 smoke：把某灰度站走 PATH A（其策略 enabled），需连续 3 次 PASS 才晋升。
$env:FLAGS_use_onednn='0'
.\.venv\Scripts\python.exe tests/manual/test_pev_live_smoke.py --site moka      # 1/4
.\.venv\Scripts\python.exe tests/manual/test_pev_live_smoke.py --site feishu    # 2/4
.\.venv\Scripts\python.exe tests/manual/test_pev_live_smoke.py --site inovance  # 3/4
.\.venv\Scripts\python.exe tests/manual/test_pev_live_smoke.py --site xiaohongshu # 4/4
```

结果写 `tests/manual/_ten_url_eval_*.json` 与 `_pev_live_smoke_<site>.json`。


---

## 8. 个性化发现（Personalized Discovery v1）

个性化发现是 **预审核（pre-review）** 通道：把 worker 已完成的共享 `JobDiscoveryTask` 中「证据核验 + 覆盖完整 + URL 安全 + 去重 + 相关性达标」的候选，以 owner-scoped 推荐的形式直接送达单个用户，跳过管理员审核。它**独立于** verified-only 的 `/api/jobs` 路径，绝不修改 `JobPosting`、`JobRelevanceScore` 或 `review_version`。推荐卡片固定标注「自动发现，建议自行确认」。

### 8.1 初始覆盖范围（v1）

- 仅 **4 个已迁移的完整抓取 adapter** 可作为自动推荐来源：Moka、Feishu、Inovance、Xiaohongshu。
- **初始不注册任何 `single_source_complete` 契约**；WeChat 文章、PDD、SnapshotExecutor、Alibaba SPA 以及所有 legacy / PATH C 结果，在保留窗口内只产出 owner-scoped 状态（不推荐）。
- 来源池只读 **retained shared tasks**（终端态 + `finished_at` 在 `personalized_discovery_retention_days`（默认 30 天）窗口内 + 同 `(source_id, external_record_id)` 最新一条）。不触发任何 URL / site / adapter / crawl-plan 请求。

### 8.2 两道完整性证明（任一即可放行 task）

1. `coverage_verified`（PEV 全量覆盖已确认）。
2. 注册的 `single_source_complete` 契约（`single_source_proof.py` 中的 `SingleSourceProofRegistry`）。

候选级另有三道门：**JD body 非空**（`responsibilities` 或 `requirements`）+ **证据存在**（`evidence_refs` 或 task 有 `JobDiscoveryEvidence.content_hash`）+ **apply URL 安全校验**。

### 8.3 单来源契约注册清单

注册一个新 `SingleSourceContract` 前，逐项确认：

- [ ] 有 fixture 测试覆盖该 adapter 的终端抓取。
- [ ] 记录一个稳定的 `evidence_hash`（首个非空证据 content_hash）。
- [ ] 终端信号 `terminal_signal` 精确（如 `job_list_complete`），不能是泛化文案。
- [ ] `application_hosts` 为 ATS 申请域 allowlist（apply URL 必须 exact-match 其中之一或 source host）。

### 8.4 状态码（closed enum）

`SourceStatusReason` 是闭合枚举，状态行只存来源标识 + 枚举 + 固定文案，**绝不**存原始 wall 文本 / cookie / token / anti-bot 细节：

| reason_code | 触发 |
|---|---|
| `login_required` / `captcha` / `anti_bot` | 对应墙 |
| `authentication_required` | `permission_denied`（401/403 + SPA session auth） |
| `coverage_incomplete` | 终端但无 coverage 证明且无单来源契约 |
| `url_unsafe` | `invalid_url` |
| `needs_manual_review` | 其余（`wechat_unavailable` / `timeout` / `budget_exceeded` / `parse_failed` / `unknown`） |

用户可自行访问被拦截来源人工查看，但 **worker 永不绕过任何墙**。

### 8.5 用户级限额与回滚

- 每用户每日（中国自然日，按 UTC 边界核算）最多 `personalized_discovery_runs_per_day`（默认 **5**）次 run；超限 `POST /runs` 返回 **429**。
- 用户相关性阈值 `personalized_discovery_min_score`（0..100）；低于阈值的候选不送达。
- **回滚**：如需停用，关闭个性化发现端点（feature flag）即可——不会影响 verified `/jobs` 与 worker 抓取。

### 8.6 RESTRICT 保留顺序（删数据时）

`personalized_discovery_recommendations.candidate_id` / `task_id` 为 `ON DELETE RESTRICT`。清理时**必须先删个性化送达行，再删 candidate / task**：

```
DELETE personalized_discovery_recommendations  -- 先
DELETE discovered_job_candidates              -- 后
DELETE job_discovery_tasks                     -- 后
```

反向删除会被 RESTRICT 阻断（`UserDiscoverySourceStatus.task_id` 为 CASCADE，无需单独处理）。

### 8.7 API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/PATCH/DELETE | `/api/personalized-discovery/preferences` | 角色 prefs（`desired_roles` / `role_synonyms` / `excluded_roles` / `personalized_discovery_min_score`），PATCH 提供→替换，DELETE 清空 |
| POST | `/api/personalized-discovery/runs` | 触发一次 run（空 body，`extra="forbid"` 拒绝 `url` 等爬虫输入） |
| GET | `/api/personalized-discovery/recommendations?limit=&offset=` | 推荐卡片（标题/公司/地点/安全 apply URL/分值/原因/信号/证据链接/固定 label/状态/时间戳） |
| GET | `/api/personalized-discovery/source-statuses?run_id=&limit=&offset=` | 来源状态（闭合 reason + 固定文案） |
| POST | `/api/personalized-discovery/recommendations/{id}/interactions` | 交互：`viewed` / `saved` / `dismissed` / `apply_clicked`（非属主 → 404） |
