# Wave 5：生产验收与灰度发布设计

## 0. 文档信息

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-07-17 |
| 状态 | 设计已校准，待实施 |
| 适用基线 | Wave 4（多站点适配）验收通过后 |
| 上游设计 | `2026-07-16-mvp-parallel-delivery-design.md`（第 9 节波次 5）、`2026-07-17-wave4-multi-site-adaptation-design.md` |
| 预估工期 | 3 周（第 10–12 周） |

## 0.1 本次优化结论

Wave 5 不是把当前开发 `docker-compose.yml` 直接改成公网部署，而是新增生产 overlay、生产环境变量模板、监控备份脚本和灰度手册，并保持开发栈可继续用于本地验证。本文对原设计做四点校准：

1. 当前 Compose 是开发栈：MySQL/Redis/MinIO 端口默认可绑定到所有网卡，只有 Backend 显式绑定 `127.0.0.1`；生产必须用 overlay 收敛端口。
2. 当前配置已存在 `TRUSTED_PROXY_CIDRS` 环境变量入口，但还缺严格校验、过宽 CIDR 拒绝和代理链回归证据。
3. 当前前端容器是 Nginx 静态服务，不是 Vite dev server；生产 Nginx 应直接服务静态文件并反代 `/api/`，不能 `proxy_pass http://frontend:5173`。
4. 生产发布前必须形成一份统一 SHA 的证据包，不能把历史提交、当前 HEAD、默认回归和 opt-in 门禁的结果混合声明为“已通过”。

## 1. 背景

Wave 3 和 Wave 4 完成了从大疆 Moka 到小鹏、科大讯飞的多站点投递闭环。此时系统已经从"功能开发"阶段进入"生产服务"阶段。Wave 5 的目标是将当前的开发环境提升为可对外提供服务的生产环境，建立监控、备份、灰度和安全体系，并通过真实用户试点验证系统的稳定性和可靠性。

## 2. 目标与非目标

### 2.1 目标

1. 将 Docker Compose 环境从开发配置加固为生产配置。
2. 建立 HTTPS、安全头、非 root 凭据、最小权限的生产安全基线。
3. 部署 Prometheus + Grafana 监控栈，覆盖系统健康、任务成功率、适配器状态。
4. 建立 MySQL 自动化备份与 MinIO 对象同步机制。
5. 实现分批次灰度发布：从 1 人到 10 人逐步放开。
6. 完成已知改进项：HMAC 限流、Correlation ID、可信 CIDR、镜像 digest。
7. 完成至少 30 次完整投递任务 + 10 份不同档案的验收。
8. 完成 5–10 名真实用户试点并收集反馈。
9. 输出生产发布证据包：Git SHA、镜像 digest、migration head、默认回归、opt-in 门禁、前端构建、恢复演练和灰度批次记录必须可追溯。

### 2.2 非目标

- 不进行云平台迁移（K8s、阿里云 ACK 等——Wave 6）。
- 不实现 CI/CD 流水线自动化（先手动部署 + checklist，后续迭代）。
- 不实现跨站点统一监控面板（Moka 优先，其他站点后续追加）。
- 不处理大规模并发（>100 用户）的性能优化。

## 3. 当前基线

### 3.1 已完成的基础设施

- Docker Compose 编排：MySQL 8.4、Redis 8、MinIO、Backend、Frontend+Nginx。
- FastAPI 健康检查：`/api/health/live`、`/api/health/ready`。
- 平台基础运行手册：`docs/runbooks/platform-foundation.md`。
- Alembic migration 工具链（单 head 线性链）。
- Backend 镜像可通过 Dockerfile 内用户配置实现非 root 运行；当前 `docker-compose.yml` 未在服务级别显式配置 `user: 1000:1000`，生产 overlay 需要明确验证容器运行用户。

### 3.2 已知缺口（来自交接文档第 10 节）

| 缺口 | 当前状态 |
|---|---|
| 限流 identity 使用 SHA-256 | 待改为 HMAC-SHA256 |
| 代理 subnet 已有 `TRUSTED_PROXY_CIDRS` 配置入口 | 待补严格校验、拒绝过宽 CIDR、补代理链门禁 |
| 无 correlation ID | 待注入请求链路 |
| 镜像未用 digest 锁定 | 待锁定 |
| 无 Python lockfile | 待生成 |
| MinIO 使用 root 凭据 | 待创建非 root 应用凭据 |
| 无生产 Compose overlay | 待创建 |
| 无备份机制 | 待建立 |
| 生产发布证据分散 | 待统一到同一 Git SHA 的 release evidence |

## 4. 生产环境加固

### 4.1 目录结构

```
deploy/
├── production/
│   ├── docker-compose.prod.yml      # 生产 overlay（端口、凭据、资源限制）
│   ├── .env.prod.example            # 生产环境变量模板
│   ├── nginx/
│   │   └── prod.conf                # HTTPS + 安全头 + 可信 CIDR
│   └── minio/
│       └── policy.json              # 应用 bucket 最小权限策略
├── monitoring/
│   ├── prometheus/
│   │   └── prometheus.yml           # 抓取配置
│   ├── grafana/
│   │   ├── datasources.yml
│   │   └── dashboards/
│   │       └── career-assistant.json
│   └── alert-rules.yml             # Prometheus 告警规则
├── backup/
│   ├── mysql-backup.sh              # mysqldump + gzip + 滚动保留
│   ├── minio-sync.sh                # MinIO 对象同步到备份位置
│   └── restore.sh                   # 恢复流程
└── scripts/
    ├── deploy.sh                    # 一键部署脚本
    └── health-check.sh              # 部署后冒烟测试
```

### 4.2 Compose 生产 overlay

`docker-compose.prod.yml` 修改项：

```yaml
# 关键变更（非完整文件，仅列差异）
services:
  mysql:
    ports:
      - "127.0.0.1:3306:3306"     # 仅 loopback，不对外暴露
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}  # 从 env 读取，不硬编码
    volumes:
      - mysql_data:/var/lib/mysql
      - ./backup:/backup:ro
    deploy:
      resources:
        limits:
          memory: 1G

  redis:
    ports:
      - "127.0.0.1:6379:6379"     # 仅 loopback
    command: redis-server --requirepass ${REDIS_PASSWORD} --port 6379
    volumes:
      - redis_data:/data

  minio:
    ports:
      - "127.0.0.1:9000:9000"
      - "127.0.0.1:9001:9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}

  backend:
    image: career-assistant-backend@sha256:${BACKEND_DIGEST}
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      APP_ENV: production
      DATABASE_URL: mysql+pymysql://app:${DB_PASSWORD}@mysql:3306/career_assistant
      REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
      TRUSTED_PROXY_CIDRS: ${TRUSTED_PROXY_CIDRS}
      RATE_LIMIT_HMAC_SECRET: ${RATE_LIMIT_HMAC_SECRET}
      # ... 其余从 .env.prod 加载
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready', timeout=5)"]
      interval: 30s
      timeout: 10s
      retries: 3

  frontend:
    image: career-assistant-frontend@sha256:${FRONTEND_DIGEST}
    ports:
      - "443:8443"                 # HTTPS，容器内使用非特权端口
    volumes:
      - ./production/nginx/prod.conf:/etc/nginx/conf.d/default.conf:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro

  prometheus:
    image: prom/prometheus@sha256:${PROMETHEUS_DIGEST}
    ports:
      - "127.0.0.1:9090:9090"
    volumes:
      - ./monitoring/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./monitoring/alert-rules.yml:/etc/prometheus/alert-rules.yml:ro
      - prometheus_data:/prometheus

  grafana:
    image: grafana/grafana@sha256:${GRAFANA_DIGEST}
    ports:
      - "127.0.0.1:3000:3000"
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD}
    volumes:
      - grafana_data:/var/lib/grafana
      - ./monitoring/grafana/datasources.yml:/etc/grafana/provisioning/datasources/datasources.yml:ro
      - ./monitoring/grafana/dashboards:/etc/grafana/provisioning/dashboards:ro

volumes:
  mysql_data:
  redis_data:
  prometheus_data:
  grafana_data:
```

### 4.3 HTTPS 与安全头

```nginx
# deploy/production/nginx/prod.conf

server {
    listen 8080;
    server_name your-domain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 8443 ssl http2;
    server_name your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # 安全头
    add_header X-Content-Type-Options    "nosniff"          always;
    add_header X-Frame-Options           "DENY"             always;
    add_header X-XSS-Protection          "1; mode=block"    always;
    add_header Referrer-Policy           "strict-origin-when-cross-origin" always;
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;

    # 仅允许内网和指定 IP 访问管理端点
    location /api/admin/ {
        allow 10.0.0.0/8;
        allow 172.16.0.0/12;
        allow 192.168.0.0/16;
        deny all;

        proxy_pass http://backend:8000;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Correlation-ID $request_id;
    }

    location /api/ {
        proxy_pass http://backend:8000;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Correlation-ID $request_id;
    }

    location / {
        root /usr/share/nginx/html;
        try_files $uri $uri/ /index.html;
    }
}
```

生产 `frontend` 容器仍然服务构建后的静态文件；`prod.conf` 只需要把 `/api/` 反代到 `backend:8000`，普通页面请求走 `try_files` 回退到 Vue Router。

### 4.4 MinIO 最小权限

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::career-assistant/*"]
    },
    {
      "Effect": "Deny",
      "Action": ["s3:*"],
      "Resource": ["arn:aws:s3:::career-assistant/*"],
      "Condition": {"Bool": {"aws:SecureTransport": "false"}}
    }
  ]
}
```

## 5. 监控与告警

### 5.1 监控指标

#### 系统健康

| 指标 | 来源 | 告警阈值 |
|---|---|---|
| Backend ready | `/api/health/ready` | 连续 3 次失败 → 告警 |
| MySQL 连接池 | FastAPI metrics | 使用率 > 80% → 警告 |
| Redis 连接 | FastAPI metrics | 连续失败 3 次 → 告警 |
| MinIO 可达 | FastAPI metrics | 连续失败 3 次 → 告警 |
| 磁盘使用率 | node_exporter | > 80% → 警告，> 90% → 告警 |
| 内存使用率 | node_exporter | > 90% → 告警 |

#### 业务指标

| 指标 | 说明 | 告警阈值 |
|---|---|---|
| `task_created_total` | 创建任务数 | 仅计数，不告警 |
| `task_dispatched_total` | 派发任务数 | 仅计数 |
| `task_reached_review_total` | 到达人工审查数 | 1 小时内 0 → 信息 |
| `task_submitted_success_total` | 提交成功数 | 仅计数 |
| `task_submitted_failed_total` | 提交失败数 | > 1/小时 → 警告 |
| `task_auto_submit_attempts` | 自动提交尝试 | > 0 → 紧急告警 |
| `adapter_circuit_breaker_open` | 熔断状态 | 任何站点熔断 → 告警 |
| `adapter_error_total` | 适配器错误数 | > 3/小时 → 警告 |

### 5.2 告警规则

```yaml
# deploy/monitoring/alert-rules.yml

groups:
  - name: career-assistant-critical
    rules:
      - alert: AutoSubmitAttempt
        expr: task_auto_submit_attempts > 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "检测到自动提交尝试！"
          description: "安全门被绕过或失效，立即排查。"

      - alert: BackendDown
        expr: up{job="career-assistant-backend"} == 0
        for: 3m
        labels:
          severity: critical
        annotations:
          summary: "Backend 不可达"

      - alert: CircuitBreakerOpen
        expr: adapter_circuit_breaker_open == 1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "站点适配器已熔断"

      - alert: HighDiskUsage
        expr: (1 - node_filesystem_avail_bytes{job="node"} / node_filesystem_size_bytes{job="node"}) > 0.9
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "磁盘使用率 > 90%"

  - name: career-assistant-warning
    rules:
      - alert: TaskFailureRate
        expr: rate(task_submitted_failed_total[1h]) > 1
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "任务提交失败率升高"

      - alert: HighMemoryUsage
        expr: (1 - node_memory_MemAvailable_bytes{job="node"} / node_memory_MemTotal_bytes{job="node"}) > 0.9
        for: 5m
        labels:
          severity: warning
```

### 5.3 Grafana Dashboard

主面板包含以下面板：

1. **系统概览**：CPU / 内存 / 磁盘 / 网络流量（折线图）
2. **服务健康**：Backend / MySQL / Redis / MinIO 状态（红绿灯）
3. **任务漏斗**：创建 → 派发 → 运行 → 审查 → 提交成功（漏斗图）
4. **站点分布**：各站点任务数（柱状图）
5. **错误趋势**：适配器错误 / 熔断 / 提交失败（时间序列）
6. **最近告警**：告警历史表

## 6. 备份与恢复

### 6.1 MySQL 日备

```bash
#!/bin/bash
# deploy/backup/mysql-backup.sh

BACKUP_DIR="/var/backups/career-assistant/mysql"
RETENTION_DAYS=7
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/mysql_$TIMESTAMP.sql.gz"

mkdir -p "$BACKUP_DIR"

mysqldump \
    --host=127.0.0.1 --port=3306 \
    --user=backup \
    --single-transaction \
    --routines --triggers --events \
    career_assistant | gzip > "$BACKUP_FILE"

# 删除 7 天前的备份
find "$BACKUP_DIR" -name "mysql_*.sql.gz" -mtime +$RETENTION_DAYS -delete

echo "Backup complete: $BACKUP_FILE"
```

### 6.2 MinIO 同步

```bash
#!/bin/bash
# deploy/backup/minio-sync.sh

BACKUP_DIR="/var/backups/career-assistant/minio"
TIMESTAMP=$(date +%Y%m%d)

mkdir -p "$BACKUP_DIR/$TIMESTAMP"

# 使用 mc mirror 同步加密对象到备份位置
mc mirror --preserve myminio/career-assistant "$BACKUP_DIR/$TIMESTAMP/"

# 保留 7 天
find "$BACKUP_DIR" -maxdepth 1 -type d -mtime +7 -exec rm -rf {} \;
```

### 6.3 恢复演练

每两周执行一次恢复演练：

```bash
# 在隔离数据库中恢复
mysql -u root -e "CREATE DATABASE career_assistant_restore_test;"
gunzip < $BACKUP_FILE | mysql career_assistant_restore_test
# 验证表数量一致
mysql -u root -e "
    SELECT COUNT(*) FROM information_schema.tables
    WHERE table_schema='career_assistant_restore_test';
"
# 运行 Alembic 到当前 head 验证 migration 兼容
alembic -c alembic.ini upgrade head
mysql -u root -e "DROP DATABASE career_assistant_restore_test;"
```

## 7. 已知改进项落地方案

### 7.1 HMAC 限流 identity

```python
# backend/app/services/rate_limit.py (重构)

import hmac

def make_rate_limit_identity(
    user_id: str, path: str, secret: bytes,
) -> str:
    """使用独立部署密钥的 HMAC-SHA256 替代裸 SHA-256。"""
    payload = f"{user_id}:{path}".encode()
    return hmac.digest(secret, payload, "sha256").hex()
```

配置要求：

- 新增 `RATE_LIMIT_HMAC_SECRET`，生产环境必填，长度至少 32 字节。
- HMAC 输入只使用稳定、低敏标识，如用户 ID、账号规范化摘要、路径和可信客户端 IP，不把原始密码、token、简历内容纳入 key。
- 旧 SHA-256 key 不迁移；部署后自然过期，限流窗口内可接受短暂重置。

### 7.2 Correlation ID

```python
# backend/app/middleware.py (新建)

from uuid import uuid4
from starlette.middleware.base import BaseHTTPMiddleware

class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        corr_id = request.headers.get("X-Correlation-ID", str(uuid4()))
        request.state.correlation_id = corr_id
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = corr_id
        return response
```

所有日志调用 `structlog` 时自动注入 `correlation_id`。

落地边界：

- 入口优先使用可信代理注入的 `X-Correlation-ID`，无值时后端生成 UUID。
- 响应头必须回传同一个 ID。
- `ApplicationEvent.redacted_payload`、适配器错误、任务进度和审计日志都记录 correlation ID，但不得把它当授权凭据。

### 7.3 可信 CIDR 校验

当前 `Settings` 已有 `trusted_proxy_cidrs` 字段，Wave 5 需要补齐校验和测试：

- 生产环境 `TRUSTED_PROXY_CIDRS` 必填。
- 拒绝 `0.0.0.0/0`、`::/0`、公网宽泛网段和空字符串。
- 只信任直接上游 IP 命中 CIDR 时的代理头；否则忽略外部 `X-Forwarded-For`。
- 更新 `tests/integration/test_proxy_rate_limit.py`，覆盖可信代理、非可信代理、伪造 XFF 和 CIDR 配置错误。

### 7.4 镜像 Digest 锁定

```bash
# 构建时固定 digest
docker build -t career-assistant-backend:latest .
DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' career-assistant-backend:latest)
echo "BACKEND_DIGEST=${DIGEST#*@}" >> .env.prod
```

### 7.5 Python Lockfile

```bash
pip freeze --exclude-editable > requirements.lock
# 或使用 pip-tools:
pip-compile pyproject.toml --output-file=requirements.lock
```

### 7.6 发布证据包

每次灰度批次开始前生成 `deploy/release-evidence/{date}-{sha}.md`：

| 项目 | 记录内容 |
|---|---|
| 代码 | Git SHA、分支、工作区是否干净 |
| 数据库 | Alembic current/head、migration 往返结果 |
| 镜像 | backend/frontend/prometheus/grafana digest |
| 后端 | Ruff、默认 pytest、security、contract、opt-in MySQL/Redis/MinIO/Nginx/Tencent |
| 前端 | `npm ci`、Vitest、`vue-tsc`、Vite build、`npm audit --audit-level=high` |
| 运维 | 备份成功、恢复演练、健康检查、监控 scrape 状态 |
| 灰度 | 批次用户数、站点、任务数、自动提交事件数、回滚判断 |

## 8. 灰度发布策略

### 8.1 逐站逐人递增

```
大疆 Moka（Wave 5 主站）
  ├── 第 1 批：1 人 × 3 个观察日
  ├── 第 2 批：5 人 × 3 个观察日（含第 1 批的 1 人）
  ├── 第 3 批：10 人（含前两批）
  └── 全量开放（所有学生用户）

小鹏飞书（Moka 稳定后）
  └── 同上 1→5→10 流程

科大讯飞 zhiye.com（飞书稳定后）
  └── 同上 1→5→10 流程
```

### 8.2 每批观察指标

| 指标 | 计算方式 | 通过标准 |
|---|---|---|
| 任务审查到达率 | `任务到达审查 / 任务派发` | ≥ 95% |
| 自动提交事件 | 计数 | = 0 |
| 越权访问 | 安全日志 | = 0 |
| 站点适配器错误率 | `错误数 / 任务数` | ≤ 5% |
| "网站填写错误"反馈 | 学生反馈类别统计 | = 0 |
| 备份成功率 | 日备日志 | = 100% |
| 健康检查可用率 | Prometheus uptime | ≥ 99% |

### 8.3 回滚条件

任一批次出现以下情况立即停止灰度并回滚：

1. 自动提交事件 > 0。
2. 越权访问事件 > 0。
3. 适配器连续错误 > 5。
4. 学生反馈"网站填写错误"类别 > 0。
5. 数据丢失或备份失败。
6. 任一生产证据项无法追溯到当前发布 SHA 或镜像 digest。

## 9. 安全验收

### 9.1 越权测试清单

| 测试 | 方法 |
|---|---|
| 学生 A 读取学生 B 的匹配报告 | API 直接请求 → 404 |
| 学生 A 读取学生 B 的简历草稿 | API 直接请求 → 404 |
| 学生 A 读取学生 B 的投递快照 | API 直接请求 → 404 |
| 学生 A 创建学生 B 的投递任务 | API 直接请求 → 404 |
| 学生访问 /admin/* | → 403 |
| 无 token 访问 executor 端点 | → 401 |
| 伪造 device token 访问 executor 端点 | → 401 |
| 过期 task lease 提交进度 | → 401 |
| 错误的 task_id + lease 组合 | → 401 |

### 9.2 隐私泄漏测试清单

| 检查点 | 验证方式 |
|---|---|
| 日志无身份证号 / 密码 / token | `grep` 扫描最近 1000 行日志 |
| API 响应无敏感字段明文 | DTO 字段白名单自动化检查 |
| 前端不渲染其他用户数据 | E2E 或手动验证 |
| MinIO 中只有加密对象 | 直接读取对象，验证是密文 |
| Executor 不上传完整 DOM/截图 | 检查云端存储的 executor 诊断文件 |

## 10. 全面验收

### 10.1 基础设施

- [ ] MySQL、Redis、MinIO、Nginx 全部仅 loopback 或 HTTPS 绑定。
- [ ] Frontend 只暴露 HTTPS 静态站点；`/api/` 反代到 backend，非 `/api/` 请求由 `try_files` 支持 Vue Router。
- [ ] 所有凭据从环境变量读取，不硬编码。
- [ ] Backend、Frontend、Migrate 容器运行用户已验证为非 root，或记录必须 root 的例外理由和补偿控制。
- [ ] 备份脚本可执行，恢复流程已验证。
- [ ] Prometheus + Grafana 正常运行，告警规则生效。
- [ ] Let's Encrypt 证书自动续期配置正确。

### 10.2 功能验收

- [ ] 在职学生至少 1 人完成 DJI Moka 投递闭环。
- [ ] 至少 5 份不同确认档案参与任务。
- [ ] DJI Moka 完成至少 30 次授权完整任务。
- [ ] 最终提交和歧义按钮自动点击 = 0。
- [ ] 字段填写正确率 ≥ 98%。
- [ ] 任务到达审查比例 ≥ 95%。
- [ ] 缺失字段与网站默认值上报率 100%。

### 10.3 灰度验收

- [ ] 1 人 → 5 人 → 10 人逐批通过（DJI Moka）。
- [ ] 每批观察期无自动提交、无越权、无数据泄漏。
- [ ] 试点用户反馈无"网站填写错误"类别。

### 10.4 门禁

- [ ] 全量默认回归 `pytest tests/unit/ tests/contract/ tests/security/` 全绿。
- [ ] Opt-in 门禁（MySQL/Redis/MinIO/Nginx/Tencent）全绿。
- [ ] Ruff 全绿。
- [ ] 前端 `vue-tsc` + `vitest` + `vite build` 全绿。
- [ ] `npm audit` 无 high/critical。
- [ ] Alembic `head → base → head` 往返验证通过。
- [ ] `deploy/release-evidence/{date}-{sha}.md` 已生成，所有测试和部署证据来自同一 Git SHA。

## 11. 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|
| 真实用户数据量不足（学生不愿意试用） | 中 | 高 | 提前招募试点用户；提供使用激励 |
| Moka 站点在灰度期间变更页面结构 | 中 | 中 | 适配器检测页面变更并暂停；版本化拓扑定义热更新 |
| 生产服务器资源不足 | 低 | 高 | 资源限制配置 + Prometheus 预警；预留升级空间 |
| Let's Encrypt 证书续期失败 | 低 | 中 | 提前 30 天告警；手动续期流程文档化 |
| 备份恢复验证失败 | 低 | 高 | 双周演练；备份完整性校验（checksum） |

## 12. 完成定义

Wave 5 在以下条件全部满足时完成：

1. 生产 Compose overlay 部署并验证，所有端口仅 loopback/HTTPS 绑定。
2. Prometheus + Grafana 正常运行，告警规则生效。
3. MySQL 日备 + MinIO 同步正常运行，恢复演练成功。
4. HMAC 限流、Correlation ID、可信 CIDR 校验、镜像 digest、Python lockfile 全部到位。
5. 发布证据包完整，Git SHA、镜像 digest、migration head 和门禁结果可追溯。
6. DJI Moka 完成至少 30 次授权完整任务，0 自动提交。
7. 5–10 名试点用户逐批灰度通过，无安全事件。
8. 全量门禁（默认回归 + opt-in + 安全 + 隐私）全部通过。
9. 灰度观察期的全部指标达标。

## 13. 后续方向

Wave 5 完成后，系统进入持续运营阶段：

- 日常：监控、告警响应、备份检查、依赖升级。
- 新站点接入：按 Wave 4 标准化流程，每个新站点 3-5 天独立验收。
- Wave 6（未来）：云平台迁移（K8s）、CI/CD 流水线、跨站点统一字段映射、大规模并发优化。
