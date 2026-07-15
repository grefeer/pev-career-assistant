# 平台基础与权威数据实施交接总结

> 更新时间：2026-07-15
> 对应分支：`master`
> 完成版本：`75463aa`
> 适用读者：后端、前端、GUI Agent、测试和运维开发人员

## 1. 一句话说明

本阶段把项目从“演示性质的本地程序”升级成了可继续承载真实校招业务的基础平台：重要业务数据进入 MySQL，临时状态进入 Redis，简历等文件加密后进入 MinIO，并补齐了认证、权限、任务状态机、设备配对、健康检查、Docker 环境和自动化测试；同时交付了通过固定腾讯智能表 MCP 端点只读同步真实职位的后端垂直切片。

同步进入 MySQL 的职位统一处于 `pending_completion`，只是待补全、待审核的候选记录，不代表职位已经核验、可直接投递或获得提交授权。用户侧补全审核、手动 JD、完整简历管理、匹配报告和招聘网站 GUI Agent 仍属于后续工作。

## 2. 当前系统结构

```mermaid
flowchart LR
    U["用户浏览器"] -->|"访问 5173"| F["Frontend 容器<br/>Vue + Nginx"]
    F -->|"/api 请求"| B["Backend 容器<br/>FastAPI + LangGraph"]
    B --> M["MySQL 8.4<br/>权威业务数据"]
    B --> R["Redis 8 / DB 0<br/>临时状态与检查点"]
    B --> O["MinIO / S3<br/>Backend 上传前加密的文件"]
    B -->|"固定 MCP endpoint<br/>list_fields + list_records（只读）"| T["腾讯智能表<br/>外部职位来源"]
    G["未来的本地 GUI Agent"] -.->|"设备配对 + 短期任务许可"| B
```

各组件的作用：

| 组件 | 作用 | 当前开发环境绑定 |
|---|---|---:|
| Frontend + Nginx | 返回 Vue 页面，并将 `/api` 转发给后端 | `0.0.0.0:5173` |
| Backend | 认证、业务 API、状态机、设备和健康检查 | `127.0.0.1:8000` |
| MySQL 8.4 | 唯一可信的业务数据源 | `0.0.0.0:3307` |
| Redis 8 | 检查点、一次性配对码、短期许可、限流 | `0.0.0.0:6380`，固定 DB 0 |
| MinIO | 保存加密后的简历和附件等对象 | `0.0.0.0:9000/9001` |
| 腾讯智能表 MCP | 固定端点只读查询字段和分页记录；不属于 readiness 依赖 | 外部 HTTPS 服务 |

说明：MinIO 和 Nginx 都由 Docker 提供，并未作为 Windows 软件单独安装。Nginx 位于 `frontend` 容器内部，因此 `docker ps` 中不会出现独立的 Nginx 容器。除 Backend 外，当前端口绑定是本机开发配置，可能被局域网访问；生产部署必须删除数据库、Redis 和 MinIO 的公网端口映射，或限制到 loopback/受控内网。

## 3. 已完成的能力

### 3.1 配置与密钥边界

- 使用类型化 `Settings` 统一读取配置。
- 生产环境拒绝示例密钥、模板凭据和 SQLite checkpoint。
- `OBJECT_ENCRYPTION_KEY` 必须是 Base64 编码的 32 字节密钥。
- 密码和密钥不得写入源码、命令行参数、日志或 Git。
- 本机约定：
  - MySQL 用户固定为 `root`，密码读取用户环境变量 `DB_PASSWORD`。
  - Redis 密码读取用户环境变量 `REDIS_PASSWORD`。
  - MinIO、JWT 和对象加密密钥读取各自的用户环境变量。

密钥的首次安全生成、用户级环境变量设置、备份边界和轮换前置检查不要自行简化，分别遵循[运行手册：生成和设置环境变量](./runbooks/platform-foundation.md#生成和设置环境变量)、[备份边界](./runbooks/platform-foundation.md#备份边界)和[密钥轮换前置检查](./runbooks/platform-foundation.md#密钥轮换前置检查)。轮换章节明确覆盖 `APP_AUTH_SECRET`、`DB_PASSWORD`、`REDIS_PASSWORD`、MinIO/S3 凭据和 `OBJECT_ENCRYPTION_KEY`。对象加密密钥丢失会导致既有密文无法恢复，轮换前必须先设计旧密钥兼容或完整重加密流程。

### 3.2 MySQL 权威数据模型

已建立 Alembic 迁移和核心业务表，包含用户、求职会话、分析记录、投递任务、投递事件和设备等数据。

核心原则：

- MySQL 是 ApplicationTask 等业务状态的唯一权威来源。
- Redis 丢失或重启不能改变任务的业务状态，也不能导致任务自动重跑。
- 状态更新和事件记录在同一个数据库事务中完成。
- MySQL Repeatable Read 下使用带锁的当前读，避免并发状态覆盖。
- 旧的 `data/app_users.json` 不迁移为权威数据源。

迁移版本：

- `20260714_0001_platform_foundation.py`：平台基础表结构。
- `20260715_0002_device_credentials.py`：设备凭据过期与轮换字段。
- `20260715_0003_real_job_sync.py`：职位来源、原始快照、同步运行和职位记录表。

### 3.3 用户认证和授权

已实现：

- 注册和登录 API。
- Argon2 密码哈希，数据库不保存明文密码。
- 固定 HS256 算法的 JWT，并验证 issuer、audience、subject、role 和过期时间。
- 普通用户与管理员角色。
- 管理员创建脚本，密码通过交互输入，不进入 argv 或环境变量。
- 登录兼容历史 6～7 字符密码；新注册密码要求至少 8 字符。
- 密码内容原样保留，不会错误删除首尾空格。
- 注册和登录 Redis 限流。

### 3.4 求职会话与分析 API

已将用户求职会话和分析结果的所有权关系持久化到 MySQL。接口通过认证用户确定数据归属，不能依赖调用方自行传入用户身份，也不能读取其他用户的数据。

### 3.5 ApplicationTask 安全状态机

已实现确定性的任务状态转换矩阵，并将每条状态边绑定到允许的角色：

- `SYSTEM`：只负责等待设备和派发等协调动作。
- `EXECUTOR`：只负责执行推进、暂停、失败和观察后的结果报告。
- `HUMAN`：独占用户取消，以及确认“最终按钮由本人点击”的状态转换。

重要安全边界：GUI Agent 不得替用户点击最终“提交/确认”按钮。用户取消和“已由用户本人点击最终按钮”的确认也不能由 Executor 冒充执行。

本阶段还没有真正的招聘网站提交 API。后续协议必须拆成两条明确分支：

- 提交分支：Agent 填完并暂停 → HUMAN 接管浏览器并审查 → HUMAN 自己点击提交 → HUMAN 将任务从 `READY_FOR_REVIEW` 推进到 `OBSERVING_USER_SUBMISSION` → Executor 此后只能观察页面结果，并使用 `task:result` 上报成功、失败或未知 → 服务端按 actor 矩阵更新状态。
- 取消分支：HUMAN 选择取消 → 任务进入 `CANCELLED` → Executor 不得继续观察或上报提交结果。

不得把“提交前人工审查”和“提交后结果观察”混为一谈，也不得把“观察到提交成功”等同于 Agent 获得提交权限。

### 3.6 GUI Agent 设备配对基础

已完成服务端基础协议，但尚未实现真正的浏览器自动填写动作：

- 一次性设备配对码。
- 配对码消费失败的补偿和并发保护。
- 长期设备凭据过期、撤销和轮换。
- 短期 task lease，绑定 `device_id + user_id + task_id + exp + scope`。
- lease scope 分为 `task:progress` 和 `task:result`。
- 永远不签发 `task:submit` 权限。

后续新增任何 Executor 动作 API 时，必须使用 task lease 校验，不能只验证长期 device token。

### 3.7 加密对象存储

已实现 S3/MinIO 对象存储适配层：

- 文件由 Backend 的对象存储客户端使用 AES-256-GCM 加密后再上传。
- 使用随机 12 字节 nonce。
- 对象 key 作为 AAD，防止密文被移动到另一个 key 后继续解密。
- 已覆盖篡改、错误密钥和错误 AAD 测试。
- 支持非 `us-east-1` region 的建桶参数。
- 多副本并发建桶时能够幂等处理 already-owned 竞态。
- readiness 实际构造加密对象存储，避免“桶可用但加密配置已坏”的假健康状态。

这里的“加密”是 Backend 在调用 S3/MinIO API 上传前执行的应用层加密，密钥由 Backend 持有，下载后也由 Backend 解密；它不是浏览器端端到端加密，也不是 MinIO/S3 自带的服务端加密。

### 3.8 Redis 8 与 LangGraph checkpoint

- 生产 checkpoint 使用 Redis 8，固定 DB 0。
- Redis 保存的是可恢复的临时执行数据，不是业务权威数据。
- 应用 lifespan 创建并关闭 Redis/checkpointer 资源。
- Redis 不可用时，认证限流采用 fail-closed，返回服务不可用而不是绕过保护。

### 3.9 Nginx、代理和限流

- Uvicorn 禁止通用 proxy-header 自动信任。
- Nginx 覆盖 `X-Real-IP`，应用只信任专用 proxy 网络中的直接上游。
- 外部提供的 `X-Forwarded-For` 不作为可信身份。
- Backend 的宿主机端口只绑定 `127.0.0.1`。
- 登录同时使用账号摘要桶和较高阈值的可信 IP 总量桶，避免同一校园网/NAT 下一个用户拖累所有人。
- 当前 Compose proxy 子网为 `172.30.250.0/28`，后续修改网络拓扑时必须同步调整可信 CIDR 并重跑代理集成测试。

### 3.10 健康检查和容器生命周期

已提供：

- `/api/health/live`：进程是否存活。
- `/api/health/ready`：MySQL、Redis 和对象存储是否可用。
- Docker healthcheck 和服务启动依赖。
- 独立 `migrate` 容器执行 Alembic 升级。
- lifespan 负责关闭自有数据库 engine、Redis、S3 和 checkpointer 资源；外部注入资源不被误关闭。

### 3.11 真实职位同步垂直切片

完成版本 `75463aa` 包含以下九类已交付工作：

1. **权威模型与迁移**：迁移 `20260715_0003` 建立 `job_sources`、不可变 `raw_job_records`、`job_sync_runs` 和 `job_postings`；MySQL 是来源配置、原始快照、同步运行和职位记录的唯一权威数据源，Redis 不保存职位真相。
2. **固定端点只读腾讯 MCP 网关**：`TencentSmartsheetGateway` 只调用固定端点的 `smartsheet.list_fields` 和 `smartsheet.list_records`，包含超时、有限重试、协议校验和稳定错误码，不调用新增、更新或删除工具，也不依赖生产 `mcporter` 子进程。
3. **来源 schema 校验与映射**：两个内置来源分别校验所需字段并映射为规范职位；不完整记录被计数跳过，来源 URL、列表字段和根域职位链接经过边界校验。
4. **持久化、去重与查询**：原始载荷按 hash 保存不可变快照，职位按来源记录身份 upsert；MySQL 行锁和租约防止同一来源并发同步，查询只返回白名单字段和 `pending_completion` 职位。
5. **分页同步与安全审计**：同步从 page 0 逐页读取，每页独立提交；中途失败保留已提交页并标记 `PARTIAL`，首屏失败标记 `FAILED`，审计和 API 只记录脱敏计数及稳定错误码，不泄漏令牌、原始载荷或上游响应。
6. **认证 API**：管理员可调用 `POST /api/admin/job-sources/{source_key}/sync`；认证用户可调用 `GET /api/jobs` 和 `GET /api/jobs/{job_id}`，同步冲突和上游失败映射为稳定的 409/502/503/504 响应。
7. **真实依赖与安全门禁**：覆盖 MySQL JSON 过滤、并发 lease、两来源真实只读同步的 opt-in 测试，以及日志/响应脱敏；真实来源测试只允许连接库名以 `_test` 结尾的专用 MySQL 测试库。
8. **Compose、运行手册与发布门禁**：Backend Compose 显式传入可选 `TENCENT_DOCS_TOKEN`，迁移升级到 `20260715_0003`；运行手册说明来源键、同步方式、状态/错误含义和测试变量，readiness 仍只检查 MySQL、Redis 和对象存储。
9. **最终全局审查与加固**：完成跨层审查并修复 token 传递、transport 异常分组、lease 失败、内置来源原子初始化、URL 校验、审计和集成门禁边界；最终实现提交为 `75463aa`。

## 4. 当前没有完成的功能

后续开发人员不要把下列能力视为已经交付：

- 面向用户的职位补全与审核工作流；同步记录仍为 `pending_completion`，尚未核验。
- 用户手动添加 JD 链接或文本，并与已同步职位执行统一去重。
- PDF/Word 简历上传、复杂格式解析和在线纠错。
- 职位匹配、简历优化和建议报告的完整产品流程。
- Playwright/GUI Agent 真正打开招聘官网并填写表单。
- 验证码、人机验证和 Human-in-the-loop 控制权交接界面。
- 单页/多页招聘表单识别，以及“中间页保存、末页不提交”的执行策略。
- 缺失字段汇总、可疑字段上报和最终人工审查界面。
- 公众号图片招聘信息分类和 OCR；当前要求此类来源先进入人工审核。

这些能力应建立在本阶段的用户、会话、ApplicationTask、设备和加密存储基础之上。

## 5. 本地启动方式

完整步骤见[平台基础运行手册](./runbooks/platform-foundation.md)。

### 5.1 标准启动前预检

先确认 Docker 正常，并确保必需的用户级变量存在。下面的检查只显示变量是否缺失，不输出秘密值：

```powershell
docker version
docker compose version

$required = @(
  'DB_PASSWORD', 'REDIS_PASSWORD', 'MINIO_ROOT_USER',
  'MINIO_ROOT_PASSWORD', 'APP_AUTH_SECRET', 'OBJECT_ENCRYPTION_KEY'
)
foreach ($name in $required) {
  $value = [Environment]::GetEnvironmentVariable($name, 'User')
  if ([string]::IsNullOrWhiteSpace($value)) {
    throw "Missing required user environment variable: $name"
  }
  Set-Item -Path "Env:$name" -Value $value
}
```

需要执行腾讯同步时，另从 User-scope 环境变量加载生产只读令牌；只检查是否存在，不输出值：

```powershell
$token = [Environment]::GetEnvironmentVariable('TENCENT_DOCS_TOKEN', 'User')
if ([string]::IsNullOrWhiteSpace($token)) {
  throw 'Missing TENCENT_DOCS_TOKEN user environment variable'
}
Set-Item -Path Env:TENCENT_DOCS_TOKEN -Value $token
```

未配置该变量不影响应用启动、职位查询或 `/api/health/ready`；只有管理员触发腾讯同步时才需要它。

### 5.2 当前开发电脑的启动命令

这台电脑当前已经存在名为 `platform-foundation` 的 Compose 项目，并且 `redis-custom` 占用了 `6379`。继续维护现有栈时使用：

```powershell

$env:MYSQL_HOST_PORT = '3307'
$env:REDIS_HOST_PORT = '6380'

docker compose -p platform-foundation up -d --build
docker compose -p platform-foundation ps -a
```

注意：

- 用户已有的 `redis-custom` 使用宿主机 `6379`，不要停止或改动它。
- 本项目 Redis 因此使用宿主机 `6380`，容器内部仍为 `6379`。
- 不要执行 `docker compose down -v`，除非明确接受删除 MySQL、Redis 和 MinIO 卷中的数据。
- 新环境若不需要沿用现有项目名，可按运行手册使用普通 `docker compose`，但必须避免创建两套占用相同端口的栈。

### 5.3 新电脑或干净环境

新环境不应假设存在 `redis-custom`、固定项目名或上述端口。先用 `docker ps` 和 `Get-NetTCPConnection` 检查冲突。如果默认端口均空闲，可在完成 5.1 的环境变量导入后执行：

```powershell
$env:MYSQL_HOST_PORT = '3306'
$env:REDIS_HOST_PORT = '6379'

docker compose -p career-assistant up -d --build
docker compose -p career-assistant ps -a

Invoke-RestMethod http://127.0.0.1:8000/api/health/ready
Invoke-WebRequest http://127.0.0.1:5173/ -UseBasicParsing
```

成功标准：`migrate` 以 0 退出，MySQL/Redis/MinIO/Backend healthy，readiness 返回 HTTP 200 且三个依赖均为 `up`，Frontend 返回 HTTP 200。如果端口冲突，按[运行手册：Compose 启停和可配置端口](./runbooks/platform-foundation.md#compose-启停和可配置端口)修改宿主端口；同一套栈后续必须继续使用相同的 `-p career-assistant` 项目名。

## 6. 常用检查命令

```powershell
# 查看容器
docker compose -p platform-foundation ps -a

# 检查后端依赖
Invoke-RestMethod http://127.0.0.1:8000/api/health/ready

# 检查前端
Invoke-WebRequest http://127.0.0.1:5173/ -UseBasicParsing

# Python 静态检查
.\.venv\Scripts\python.exe -m ruff check backend src tests scripts

# 职位同步核心回归
.\.venv\Scripts\python.exe -m pytest tests/unit/test_tencent_smartsheet.py tests/unit/test_job_mappers.py tests/unit/test_job_repository.py tests/unit/test_job_sync_service.py tests/contract/test_jobs_api.py tests/security/test_no_sensitive_logging.py -q

# 前端构建
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
```

完整真实依赖测试需要设置 `TEST_MYSQL_URL`、`TEST_REDIS_URL`、MinIO 测试变量；真实腾讯只读门禁还需要 `TEST_TENCENT_DOCS_TOKEN`。禁止把带密码的完整 URL 或令牌输出到终端日志。

完整门禁使用的变量包括：

- `TEST_MYSQL_URL`：只能指向专用测试库，例如 `career_assistant_test`；migration 测试会执行 downgrade/upgrade，严禁指向生产库。
- `TEST_REDIS_URL`：使用密码保护的 Redis 8，固定 DB 0。
- `TEST_S3_ENDPOINT`、`TEST_S3_ACCESS_KEY`、`TEST_S3_SECRET_KEY`、`TEST_S3_BUCKET`：使用独立测试 bucket。
- `TEST_TENCENT_DOCS_TOKEN`：仅用于两类内置来源的 opt-in 真实只读测试，不得填写令牌值到文档、命令历史或仓库；测试同时要求 `TEST_MYSQL_URL` 的数据库名以 `_test` 结尾。
- `TEST_PROXY_RATE_LIMIT=1`、`TEST_COMPOSE_NETWORK`、`TEST_BACKEND_IMAGE`：启用真实 Nginx→Uvicorn 代理链测试。

通用设置方法见[运行手册：测试和发布前门禁](./runbooks/platform-foundation.md#测试和发布前门禁)。在当前开发电脑复现完整门禁时，先执行 5.1 将六个基础变量加载到当前 PowerShell 进程，再执行：

```powershell
$dbPassword = [Uri]::EscapeDataString($env:DB_PASSWORD)
$redisPassword = [Uri]::EscapeDataString($env:REDIS_PASSWORD)

$env:TEST_MYSQL_URL = "mysql+pymysql://root:$dbPassword@127.0.0.1:3307/career_assistant_test?charset=utf8mb4"
$env:TEST_REDIS_URL = "redis://:$redisPassword@127.0.0.1:6380/0"
$env:TEST_S3_ENDPOINT = 'http://127.0.0.1:9000'
$env:TEST_S3_ACCESS_KEY = $env:MINIO_ROOT_USER
$env:TEST_S3_SECRET_KEY = $env:MINIO_ROOT_PASSWORD
$env:TEST_S3_BUCKET = 'career-assistant-storage-test'
$env:TEST_TENCENT_DOCS_TOKEN = [Environment]::GetEnvironmentVariable('TEST_TENCENT_DOCS_TOKEN', 'User')
$env:TEST_PROXY_RATE_LIMIT = '1'
$env:TEST_COMPOSE_NETWORK = 'platform-foundation_proxy'
$env:TEST_BACKEND_IMAGE = 'platform-foundation-backend:latest'

.\.venv\Scripts\python.exe -m pytest -q
```

完成版本 `75463aa` 的最终本地结果是 `540 passed, 9 skipped`。`pytest -q -rs` 显示九个 skip 均为环境门禁：五个仅要求 `TEST_MYSQL_URL`，对象存储、Compose Nginx→Uvicorn 代理链和 Redis 8 集成各一个，另一个真实腾讯只读测试同时要求 `TEST_MYSQL_URL` 与 `TEST_TENCENT_DOCS_TOKEN`。因此真实腾讯来源读取仍是尚待具备专用测试库和有效测试令牌后完成的外部验证缺口，不能描述为已经通过。测试会创建并清理临时 Redis key、测试对象和代理客户端容器；如果测试被强制中断，应检查残留的 `rate-client-*` 容器和测试 bucket 对象。

## 7. 管理员创建

必须让命令经过容器 entrypoint，entrypoint 会安全构造数据库和 Redis URL：

```powershell
docker compose -p platform-foundation run --rm backend `
  python scripts/create_admin.py --account admin --nickname Administrator
```

不要将管理员密码放在命令参数或环境变量中；脚本会交互式询问密码。

### 7.1 数据保护与恢复提醒

- 使用 `docker volume ls --filter label=com.docker.compose.project=platform-foundation` 识别当前项目卷。
- MySQL 业务数据必须备份；迁移失败时先判断数据影响，再按运行手册执行受控 downgrade，不能直接删除卷重建。
- MinIO 中保存的是密文对象，但必须同时备份对象、metadata 和对应的对象加密密钥；只有其中一部分无法完成恢复。
- Redis 只保存可丢弃的临时状态。Redis 丢失后不从它推断或重写 ApplicationTask，恢复步骤见[运行手册：Redis 丢失恢复](./runbooks/platform-foundation.md#redis-丢失恢复)。
- 当前文档给出的是备份边界，不等同于生产灾难恢复方案。生产发布前必须为实际部署环境补充 MySQL dump/restore、MinIO 版本化备份、密钥托管和定期恢复演练。

## 8. 关键代码位置

| 内容 | 位置 |
|---|---|
| 类型化配置 | `backend/app/config.py` |
| FastAPI 应用与 lifespan | `backend/app/main.py` |
| 数据库模型 | `backend/app/db/models.py` |
| 数据库迁移 | `alembic/versions/` |
| 腾讯只读 MCP 网关 | `backend/app/services/tencent_smartsheet.py` |
| 来源 schema 与映射 | `backend/app/services/job_mappers.py` |
| 职位同步编排 | `backend/app/services/job_sync.py` |
| 职位持久化、lease 与查询 | `backend/app/repositories/jobs.py` |
| 同步和职位查询 API | `backend/app/api/routes/jobs.py` |
| 认证服务 | `backend/app/services/auth.py` |
| 设备配对与 task lease | `backend/app/services/devices.py` |
| ApplicationTask 状态机 | `backend/app/services/applications.py` |
| 加密对象存储 | `backend/app/services/storage.py` |
| Redis checkpoint | `src/checkpointing.py` |
| API 路由 | `backend/app/api/routes/` |
| 容器 entrypoint | `backend/entrypoint.py` |
| Docker 编排 | `docker-compose.yml` |
| Redis 安全启动脚本 | `docker/redis/start.sh` |
| 管理员脚本 | `scripts/create_admin.py` |
| 前端 Nginx 配置 | `frontend/nginx.conf` |
| 测试 | `tests/contract/`、`tests/unit/`、`tests/integration/`、`tests/security/` |

## 9. 验收结果

合并到 `master` 后的最终验证结果：

- Ruff：通过。
- Python 测试：`540 passed, 9 skipped`；其中五个仅缺 `TEST_MYSQL_URL`，对象存储、Compose 代理链、Redis 集成各一个，真实腾讯只读测试一个（同时缺 `TEST_MYSQL_URL` 和 `TEST_TENCENT_DOCS_TOKEN`）。
- 真实 MySQL 8.4：通过，包含 migration upgrade/downgrade 往返。
- 真实 Redis 8 DB 0：通过。
- 真实 MinIO：通过。
- Nginx 到 Uvicorn 的双客户端限流测试：通过。
- 前端 Vite production build：通过。
- Compose migration：exit 0。
- readiness：MySQL、Redis、object store 均为 `up`。
- 腾讯真实双来源只读验证：未执行；当前开发环境未设置 `TEST_TENCENT_DOCS_TOKEN`，属于明确的外部验证缺口。
- 前端 HTTP：200。
- 独立 senior review：无 Critical、无 Important，Approve。

Windows 合并后曾发现 shell 脚本被 `core.autocrlf=true` 转成 CRLF。现已通过根目录 `.gitattributes` 强制 `*.sh text eol=lf`，相关 Docker Redis shell 测试已纳入并通过。

本次验证工具链：Python `3.12.5`、Node.js `24.14.0`、npm `11.4.2`、Docker `28.1.1`、Docker Compose `2.35.1-desktop.1`。升级工具链后应重新运行完整门禁。

readiness 成功时 HTTP 状态应为 200，核心响应含义如下：

```json
{
  "status": "ready",
  "dependencies": {
    "mysql": "up",
    "redis": "up",
    "object_store": "up"
  }
}
```

## 10. 已知非阻断改进项

以下事项不阻塞当前基础平台，但建议在生产发布前处理：

1. 限流 identity 从普通 SHA-256 改为使用独立部署密钥的 HMAC-SHA256。
2. 将固定 proxy subnet 环境变量化，并在 Settings 中严格验证可信 CIDR、拒绝 `0.0.0.0/0` 等过宽配置。
3. 为请求、task 和 audit event 增加 correlation ID。
4. 使用镜像 digest 和带 hash 的 Python lockfile，提高构建可复现性。
5. 生产环境使用 MinIO/S3 非 root 应用凭据、最小 bucket policy 和单独的 production Compose overlay。
6. 新增任何 Executor API 时，补齐 principal/actor 权限测试并强制 task lease。

## 11. 下一步建议

按依赖顺序推进以下工作包，避免匹配或自动填写建立在未经用户确认的数据上：

1. **用户侧职位补全与审核工作流**：先提供 `pending_completion` 列表、缺失/可疑字段展示、编辑、确认与驳回能力；只有明确完成人工审核后，职位才能进入后续匹配候选集。
2. **手动 JD 导入与统一去重**：支持粘贴 JD 文本或链接，复用来源映射、规范化和 MySQL 权威存储规则，并与腾讯同步记录执行可解释的去重/合并。
3. **职位匹配与报告生成**：基于已审核职位和用户资料生成匹配分数、证据、缺口与建议报告，保留输入版本和可追溯依据。
4. **简历生命周期管理**：补齐 PDF/Word 上传、解析、纠错、版本管理、加密对象引用和与匹配报告的版本绑定。
5. **GUI Agent 表单填写**：最后实现招聘站点识别、字段填写、缺失字段汇总和 Human-in-the-loop 接管；继续沿用现有边界：Agent 只能填写并暂停，最终提交必须由 HUMAN 本人完成，永远不签发 `task:submit`。

## 12. 开发时必须遵守的规则

- 不得把 MySQL、Redis、MinIO、JWT 或加密密钥写入仓库。
- 不得在日志中输出密码、token、验证码、身份证、完整简历或带密码的连接 URL。
- 不得把 Redis 当成 ApplicationTask 权威状态源。
- 不得让 GUI Agent 自动点击招聘网站最终提交按钮。
- 不得只用长期 device token 执行具体任务动作；必须验证 task lease 和 scope。
- 不得让用户通过请求参数读取或修改其他用户的数据。
- 单页招聘表单不得误判为多页后自动点击保存/提交。
- 缺失字段和不确定字段应继续填写确定内容，最后统一上报用户，不应阻塞整个自动填写流程。

## 13. 相关文档

- [产品与技术设计](./superpowers/specs/2026-07-14-campus-recruitment-career-assistant-design.md)
- [平台基础与权威数据实施计划](./superpowers/plans/2026-07-14-platform-foundation-authoritative-data.md)
- [真实职位同步纵向闭环设计](./superpowers/specs/2026-07-15-real-job-sync-vertical-slice-design.md)
- [真实职位同步纵向闭环实施计划](./superpowers/plans/2026-07-15-real-job-sync-vertical-slice.md)
- [平台基础运行手册](./runbooks/platform-foundation.md)
- [项目 README](../README.md)
