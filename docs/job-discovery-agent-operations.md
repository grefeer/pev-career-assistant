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
