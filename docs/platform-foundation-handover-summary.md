# 平台基础与权威数据实施交接总结

> 更新时间：2026-07-17
> 对应分支：`master`
> 代码基线：`db31883`（Alembic head：`7b757ef17d3f`）
> 适用读者：后端、前端、GUI Agent、测试和运维开发人员

> 文档状态：当前交接摘要。已完成迁移 `0001` 至 `0008`（含 hotfix
> `7b757ef17d3f`），覆盖平台基础、职位同步与审核、个人资料与简历生命周期、
> 手动 JD 导入与去重、职位反馈闭环、证据化匹配、定制简历与投递快照。
> Wave 1 并行工作流（A/B/C/D）和 Wave 2 集成工作流（E）均已交付。
> 当前行为以本文、[README](../README.md)、
> [平台基础运行手册](./runbooks/platform-foundation.md)和
> [MVP 并行交付设计](./superpowers/specs/2026-07-16-mvp-parallel-delivery-design.md)
> 为准。

## 1. 一句话说明

当前项目已经形成可继续承载真实校招业务的平台基础：重要业务数据进入 MySQL，临时状态进入 Redis，并具备认证、权限、任务状态机、设备配对、加密对象存储适配器、健康检查和 Docker 环境；真实职位可从固定腾讯智能表 MCP 端点只读同步，经管理员补全与核验后进入学生职位中心。当前分析上传仍只在请求内解析，没有把简历持久化到 MinIO。

同步进入 MySQL 的新职位先处于 `pending_completion`。管理员补全后进入
`pending_review`，核验后进入 `verified`；认证学生的职位接口只返回
`verified`。`rejected`、`expired` 和其他非核验状态不会公开。手动 JD、完整简历
生命周期、基于已核验职位的匹配报告，以及招聘网站 GUI Agent 仍属于后续工作。

## 2. 当前系统结构

```mermaid
flowchart LR
    U["用户浏览器"] -->|"当前开发机访问 15173"| F["Frontend 容器<br/>Vue + Nginx"]
    F -->|"/api 请求"| B["Backend 容器<br/>FastAPI + LangGraph"]
    B --> M["MySQL 8.4<br/>权威业务数据"]
    B --> R["Redis 8 / DB 0<br/>临时状态与检查点"]
    B --> O["MinIO / S3<br/>预留的应用层加密对象存储"]
    B -->|"固定 MCP endpoint<br/>list_fields + list_records（只读）"| T["腾讯智能表<br/>外部职位来源"]
    G["未来的本地 GUI Agent"] -.->|"设备配对 + 短期任务许可"| B
```

各组件的作用：

| 组件 | 作用 | 当前开发环境绑定 |
|---|---|---:|
| Frontend + Nginx | 返回 Vue 页面，并将 `/api` 转发给后端 | `0.0.0.0:15173` |
| Backend | 认证、业务 API、状态机、设备和健康检查 | `127.0.0.1:18000` |
| MySQL 8.4 | 唯一可信的业务数据源 | `0.0.0.0:3307` |
| Redis 8 | 检查点、一次性配对码、短期许可、限流 | `0.0.0.0:6380`，固定 DB 0 |
| MinIO | 提供应用层加密对象存储适配；当前分析上传尚未持久化 | `0.0.0.0:19000/19001` |
| 腾讯智能表 MCP | 固定端点只读查询字段和分页记录；不属于 readiness 依赖 | 外部 HTTPS 服务 |

说明：上表是 2026-07-16 当前已验证的 `platform-foundation` 本机覆盖值；Compose 默认仍为前端 `5173`、后端 `8000`、MySQL `3306`、Redis `6379`、MinIO `9000/9001`。MinIO 和 Nginx 都由 Docker 提供，Nginx 位于 `frontend` 容器内部。除 Backend 外，当前端口绑定可能被局域网访问；生产部署必须删除数据库、Redis 和 MinIO 的公网端口映射，或限制到 loopback/受控内网。

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

- `20260714_0001`：平台基础表结构（用户、会话、设备、任务、审计）。
- `20260715_0002`：设备凭据过期与轮换字段。
- `20260715_0003`：职位来源、原始快照、同步运行和职位记录表。
- `20260716_0004`：职位审核生命周期、人工规范字段、乐观版本和不可变核验事件。
- `20260717_0005`：个人资料与简历生命周期（Profile、ResumeAsset、ResumeImport、ProfileFieldEvidence、ProfileFieldDecision、ConfirmedProfileVersion）。
- `20260717_0006`：手动 JD 导入与去重（UserJobSubmission、JobDuplicateCandidate、JobSourceLink）。
- `20260717_0007`：职位反馈闭环（JobFeedback、JobFeedbackEvent）。
- `20260718_0008`：证据化匹配、定制简历与投递快照（MatchReport、ResumeDraft、ApprovedResumeVersion、ApprovedResumeAttachment、ApplicationSnapshot）。
- `7b757ef17d3f`：修复 MatchReport 状态 CHECK 约束（允许 pending/running 状态）。

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

`20260715_0003` 完成了腾讯智能表只读同步基础：

1. **权威模型与迁移**：迁移 `20260715_0003` 建立 `job_sources`、不可变 `raw_job_records`、`job_sync_runs` 和 `job_postings`；MySQL 是来源配置、原始快照、同步运行和职位记录的唯一权威数据源，Redis 不保存职位真相。
2. **固定端点只读腾讯 MCP 网关**：`TencentSmartsheetGateway` 只调用固定端点的 `smartsheet.list_fields` 和 `smartsheet.list_records`，包含超时、有限重试、协议校验和稳定错误码，不调用新增、更新或删除工具，也不依赖生产 `mcporter` 子进程。
3. **来源 schema 校验与映射**：两个内置来源分别校验所需字段并映射为规范职位；不完整记录被计数跳过，来源 URL、列表字段和根域职位链接经过边界校验。
4. **持久化与同步并发**：原始载荷按 hash 保存不可变历史，职位按来源记录身份 upsert；同步按 source → posting 的固定锁序执行，并通过来源租约防止同一来源并发写入和审核死锁。
5. **分页同步与安全审计**：同步从 page 0 逐页读取，每页独立提交；中途失败保留已提交页并标记 `PARTIAL`，首屏失败标记 `FAILED`，审计和 API 只记录脱敏计数及稳定错误码，不泄漏令牌、原始载荷或上游响应。
6. **认证 API**：管理员可调用 `POST /api/admin/job-sources/{source_key}/sync`；同步冲突和上游失败映射为稳定的 409/502/503/504 响应。
7. **真实依赖与安全门禁**：覆盖 MySQL JSON 过滤、并发 lease、两来源真实只读同步的 opt-in 测试，以及日志/响应脱敏；真实来源测试只允许连接库名以 `_test` 结尾的专用 MySQL 测试库。
8. **平台边界**：腾讯来源不是 readiness 依赖；未配置 `TENCENT_DOCS_TOKEN` 不影响应用启动、职位读取或基础健康检查，只影响管理员主动同步。

### 3.12 职位补全、审核与学生职位中心

`20260716_0004` 和功能完成提交 `4bc4979` 已交付完整人工审核纵向闭环：

1. **状态与权威数据**：职位使用 `pending_completion`、`pending_review`、`verified`、`expired`、`rejected` 生命周期；`JobPosting` 保存人工规范值和最新 `source_candidate`，`job_verifications` 追加记录每次成功操作的快照、版本、动作和稳定原因码。
2. **补全与决策服务**：管理员可保存补全稿、核验、拒绝或失效职位。所有写操作同时使用 posting 行锁和 `review_version` 乐观并发控制；成功操作与一条 `JobVerification` 在同一事务提交，旧版本请求返回 `stale_job_review`。
3. **来源重同步保护**：职位进入人工流程后，来源变化只更新 `source_candidate` 并设置 `source_changed_since_review=true`，不会覆盖人工确认字段；来源身份变化会使旧审核版本失效。
4. **稳定原因码契约**：拒绝仅接受 `invalid_source`、`wrong_company`、`insufficient_job_details`、`unsafe_or_invalid_apply_channel`；失效仅接受 `closed_on_official_site`、`deadline_passed`、`application_channel_unavailable`。未知值和跨决策错配在 API 层返回 422，服务层也不能绕过。
5. **渠道安全**：邮箱、二维码、扫码、微信等人工渠道可以核验，但必须保持 `gui_eligible=false`；核验不会授予 GUI Agent 最终提交权限。
6. **学生 API 与界面**：认证学生的 `GET /api/jobs` 和 `GET /api/jobs/{job_id}` 只返回 `verified` 白名单字段；学生职位中心覆盖加载、空结果、错误和并发响应丢弃。
7. **管理员 API 与界面**：管理员具备审核队列、补全、核验、拒绝和失效操作；界面处理 dirty/busy 状态、请求串行化和 409 重新加载，普通学生不显示管理员入口，后端 `require_admin` 仍是最终权限边界。
8. **隐私与门禁**：公开/管理员 DTO 均采用字段白名单，不返回 RawJobRecord、payload hash、腾讯令牌或 MCP trace；破坏性 MySQL 测试必须显式设置 `ALLOW_DESTRUCTIVE_MYSQL_TESTS=1`，且数据库名必须以 `_test` 结尾。

## 4. 当前没有完成的功能

后续开发人员不要把下列能力视为已经交付：

- 公众号图片招聘信息分类和 OCR；当前要求此类来源先进入人工审核。
- 真实招聘网站 GUI Agent 适配（大疆 Moka、小鹏、科大讯飞）。
- 验证码、人机验证和 CAPTCHA 处理。
- 执行器本地敏感字段从 Windows Credential Manager 解析密码/身份证号等。
- 生产环境部署（镜像 digest、Python lockfile、MinIO 非 root 凭据、production Compose overlay）。
- `npm audit` 的 high severity 前端依赖已通过 lockfile 升级关闭。

### 4.1 已交付（自平台基础完成后新增）

- 个人资料与简历生命周期：PDF/DOCX/文本上传、加密存储、解析、证据化字段确认、不可变版本。
- 手动 JD 导入与统一去重：学生提交 URL/JD 文本、管理员提升、来源关系链。
- Windows Executor 骨架与模拟安全门：Playwright 引擎、页面观察/填写/回读、本地检查点、安全决策、9 个模拟页面。
- 职位反馈闭环：学生四项反馈类别、管理员处置、幂等事件。
- 证据化匹配与定制简历：MatchReport、ResumeDraft、ApprovedResumeVersion、ApplicationSnapshot。
- Web 工作台：Vue Router 分域路由（匹配、职位、个人资料、快照、设备、管理）。
- 旧 `POST /api/analysis/run` 已移除，Web 匹配入口统一为 `POST /api/matches`。
- `data/jobs.json` 演示职位文件已删除。

这些能力建立在平台基础（用户、会话、ApplicationTask、设备配对、加密存储）之上。

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
$env:MINIO_HOST_PORT = '19000'
$env:MINIO_CONSOLE_HOST_PORT = '19001'
$env:BACKEND_HOST_PORT = '18000'
$env:FRONTEND_HOST_PORT = '15173'

docker compose -p platform-foundation up -d --build
docker compose -p platform-foundation ps -a
Invoke-RestMethod http://127.0.0.1:18000/api/health/ready
Invoke-WebRequest http://127.0.0.1:15173/ -UseBasicParsing
```

注意：

- 用户已有的 `redis-custom` 使用宿主机 `6379`，不要停止或改动它。
- 本项目 Redis 因此使用宿主机 `6380`，容器内部仍为 `6379`。
- 同一 `platform-foundation` 项目后续启动必须继续设置上述六个宿主端口变量；遗漏变量会回退到 Compose 默认端口，并可能与本机服务冲突。
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

以下示例默认检查 5.2 的当前开发机栈。新电脑若按 5.3 使用 `career-assistant`，先把
`$composeProject` 改为对应项目名。

```powershell
$repoRoot = (Resolve-Path '.').Path
$python = (Resolve-Path (Join-Path $repoRoot '.venv\Scripts\python.exe')).Path
$composeProject = 'platform-foundation'
$backendPort = ((docker port "${composeProject}-backend-1" 8000/tcp | Select-Object -First 1) -split ':')[-1]
$frontendPort = ((docker port "${composeProject}-frontend-1" 80/tcp | Select-Object -First 1) -split ':')[-1]
if (-not $backendPort -or -not $frontendPort) { throw "$composeProject stack is not running" }

# 查看容器
docker compose -p $composeProject ps -a

# 检查迁移和后端依赖
docker compose -p $composeProject run --rm migrate alembic current
Invoke-RestMethod "http://127.0.0.1:$backendPort/api/health/ready"

# 检查前端
Invoke-WebRequest "http://127.0.0.1:$frontendPort/" -UseBasicParsing

# Python 静态检查
& $python -m ruff check backend src tests scripts

# 职位同步、补全和审核核心回归
& $python -m pytest tests/unit/test_tencent_smartsheet.py tests/unit/test_job_mappers.py tests/unit/test_job_repository.py tests/unit/test_job_sync_service.py tests/unit/test_job_review_service.py tests/contract/test_jobs_api.py tests/security/test_no_sensitive_logging.py -q

# 前端依赖、测试、类型检查和生产构建
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build
```

完整真实依赖测试需要设置 `TEST_MYSQL_URL`、`TEST_REDIS_URL`、MinIO 测试变量；真实腾讯只读门禁还需要 `TEST_TENCENT_DOCS_TOKEN`。禁止把带密码的完整 URL 或令牌输出到终端日志。

完整门禁使用的变量包括：

- `ALLOW_DESTRUCTIVE_MYSQL_TESTS=1`：破坏性 MySQL 门禁的显式开关；缺失时按精确变量名 skip，其他值不得启用测试。
- `TEST_MYSQL_URL`：只能指向专用测试库，例如 `career_assistant_test`；guard 会拒绝空 URL、非 MySQL 后端和库名不以 `_test` 结尾的连接。migration 测试会执行 downgrade/upgrade，严禁指向业务库。
- `TEST_REDIS_URL`：使用密码保护的 Redis 8，固定 DB 0。
- `TEST_S3_ENDPOINT`、`TEST_S3_ACCESS_KEY`、`TEST_S3_SECRET_KEY`、`TEST_S3_BUCKET`：使用独立测试 bucket。
- `TEST_TENCENT_DOCS_TOKEN`：仅用于两类内置来源的 opt-in 真实只读测试，不得填写令牌值到文档、命令历史或仓库；测试同时要求 `TEST_MYSQL_URL` 的数据库名以 `_test` 结尾。
- `TEST_PROXY_RATE_LIMIT=1`、`TEST_COMPOSE_NETWORK`、`TEST_BACKEND_IMAGE`：启用真实 Nginx→Uvicorn 代理链测试。

通用设置方法见[运行手册：测试和发布前门禁](./runbooks/platform-foundation.md#测试和发布前门禁)。在当前开发电脑复现完整门禁时，先执行 5.1 将六个基础变量加载到当前 PowerShell 进程，并重新执行 5.2 的六个宿主端口赋值，再执行：

```powershell
$mysqlHostPort = if ($env:MYSQL_HOST_PORT) { $env:MYSQL_HOST_PORT } else { '3306' }
$redisHostPort = if ($env:REDIS_HOST_PORT) { $env:REDIS_HOST_PORT } else { '6379' }
$minioHostPort = if ($env:MINIO_HOST_PORT) { $env:MINIO_HOST_PORT } else { '9000' }
$env:TEST_MYSQL_URL = & $python -c "import os,sys,urllib.parse; print('mysql+pymysql://root:'+urllib.parse.quote(os.environ['DB_PASSWORD'],safe='')+'@127.0.0.1:'+sys.argv[1]+'/career_assistant_test?charset=utf8mb4')" $mysqlHostPort
$env:TEST_REDIS_URL = & $python -c "import os,sys,urllib.parse; print('redis://:'+urllib.parse.quote(os.environ['REDIS_PASSWORD'],safe='')+'@127.0.0.1:'+sys.argv[1]+'/0')" $redisHostPort
$env:TEST_S3_ENDPOINT = "http://127.0.0.1:$minioHostPort"
$env:TEST_S3_ACCESS_KEY = [Environment]::GetEnvironmentVariable('MINIO_ROOT_USER', 'User')
$env:TEST_S3_SECRET_KEY = [Environment]::GetEnvironmentVariable('MINIO_ROOT_PASSWORD', 'User')
$env:TEST_S3_BUCKET = 'career-assistant-storage-test'
$env:TEST_TENCENT_DOCS_TOKEN = [Environment]::GetEnvironmentVariable('TEST_TENCENT_DOCS_TOKEN', 'User')
$env:ALLOW_DESTRUCTIVE_MYSQL_TESTS = '1'
$env:TEST_PROXY_RATE_LIMIT = '1'
$env:TEST_COMPOSE_NETWORK = "${composeProject}_proxy"
$env:TEST_BACKEND_IMAGE = "${composeProject}-backend:latest"

& $python -m ruff check backend src tests scripts
& $python -m pytest -q -rs
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build
```

上述命令的最新证据与真实依赖边界见第 9 节。测试会创建并清理临时 Redis key、测试对象和代理客户端容器；如果测试被强制中断，应检查残留的 `rate-client-*` 容器和测试 bucket 对象。

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
| 职位审核原因码领域契约 | `backend/app/domain/job_review.py` |
| 职位补全与审核服务 | `backend/app/services/job_review.py` |
| 职位公开/管理员 DTO | `backend/app/api/job_schemas.py` |
| 同步、职位查询和审核 API | `backend/app/api/routes/jobs.py` |
| 学生职位中心 | `frontend/src/features/jobs/JobCenter.vue` |
| 管理员职位审核 | `frontend/src/features/jobs/AdminJobReview.vue` |
| 职位前端 DTO 与 API | `frontend/src/features/jobs/jobTypes.ts`、`frontend/src/features/jobs/jobsApi.ts` |
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

## 9. 验收结果与证据来源

以下证据区分默认回归、真实 MySQL 门禁和当前 Compose 运行态；不同 SHA 的结果不得合并成一次“最终全门禁通过”。

### 9.1 当前功能代码的默认环境验证

- 当前 `HEAD db31883`，Alembic head `7b757ef17d3f`。
- 完整 Python 默认回归：`907 passed, 6 skipped`；使用根目录 `.venv` 的 Python 3.12.5。
- 核心单元/合约/安全测试：907 passed。
- Ruff：`All checks passed!`（`ruff check backend src tests scripts`）。
- 前端：13 个测试文件、`78 passed`；`vue-tsc` 类型检查通过；Vite production build 通过。
- `git diff --check` 通过，有 CRLF 警告（.gitattributes 已配置 `*.sh text eol=lf`）。

默认回归中的 6 个 skip 属于需要外部环境或显式 opt-in 的门禁。

### 9.2 真实 MySQL 与并发门禁

- 历史记录：在提交 `a8008cb` 上使用隔离库执行了 24 个真实 MySQL/guard 用例全部通过。
- 当前 HEAD 新增了迁移 `0005` 至 `0008` 和 hotfix `7b757ef17d3f`；真实 MySQL 套件未在 `db31883` 上重跑。
- 必须在当前 HEAD 上重新执行 MySQL destructive guard、migration 往返（`head → base → head`）、并发同步和并发审核测试。

### 9.3 当前 Compose 运行态

- 当前 `docker-compose.yml` schema-revision 标签为 `20260718_0008`。
- `platform-foundation` 当前使用 MySQL `3307`、Redis `6380`、MinIO `19000/19001`、Backend `18000`、Frontend `15173`。
- `GET /api/health/live`、`GET /api/health/ready` 和前端首页均返回 HTTP 200。
- 当前开发库 `job_postings` 为 0 条；需要管理员触发腾讯同步并核验后方可验收真实职位匹配。

### 9.4 尚未关闭的外部验证缺口

- MySQL、Redis、MinIO 和 Nginx→Uvicorn 专门 opt-in 套件没有在当前 HEAD 上重跑。
- 真实腾讯双来源只读门禁现在可读取 `TEST_TENCENT_DOCS_TOKEN`，未配置时回退使用 `TENCENT_DOCS_TOKEN`；当前 HEAD 仍需在真实测试库上重跑并记录结果。
- 前端 `npm audit --audit-level=high --registry=https://registry.npmjs.org` 当前为 0 vulnerabilities。
- 个人资料、匹配、快照和执行器集成测试（`tests/integration/`）未包含在默认回归中，需要 MySQL/MinIO 测试环境。

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
7. 持续关注前端依赖升级；当前 `npm audit` high severity 报告已关闭，后续升级仍需重跑前端测试、类型检查和 production build。
8. 在当前 HEAD 上重跑 MySQL、Redis、MinIO、Nginx 代理链和真实腾讯只读 opt-in 门禁，形成统一可追溯的发布证据。

## 11. 下一步建议

Wave 1（个人资料、手动 JD、执行器骨架、职位反馈）和 Wave 2（证据化匹配、定制简历、
投递快照）已交付。下一阶段进入 Wave 3：大疆 Moka 纵向闭环。

硬前置条件：

1. 在当前 HEAD 上重跑 MySQL/Redis/MinIO/Nginx opt-in 门禁。
2. 在当前进程加载 `TENCENT_DOCS_TOKEN` 或 `TEST_TENCENT_DOCS_TOKEN` 后，使用真实测试库重跑腾讯双来源只读门禁。
3. 完成执行器本地敏感字段从 Windows Credential Manager 解析。
4. 至少一个 `verified` 职位和至少一个 `ConfirmedProfileVersion` 用于真实匹配验收。

Wave 3 实施要点（参见
[MVP 并行交付设计](./superpowers/specs/2026-07-16-mvp-parallel-delivery-design.md)
第 9 节波次 3）：

- 实现 Moka 页面拓扑、常用控件、重复经历组件和附件上传适配。
- 实现单页停止、多页安全中间保存和末页停止。
- 验证网络中断、Chrome 重启、页面变化和任务恢复不重复添加经历。
- 硬门禁：最终提交和歧义组合按钮自动点击 = 0。

大疆安全门通过后方可并行扩展小鹏和科大讯飞。

## 12. 开发时必须遵守的规则

- 不得把 MySQL、Redis、MinIO、JWT 或加密密钥写入仓库。
- 不得在日志中输出密码、token、验证码、身份证、完整简历或带密码的连接 URL。
- 不得把 Redis 当成 ApplicationTask 权威状态源。
- 不得让 GUI Agent 自动点击招聘网站最终提交按钮。
- 不得只用长期 device token 执行具体任务动作；必须验证 task lease 和 scope。
- 不得让用户通过请求参数读取或修改其他用户的数据。
- 学生职位 API 不得返回 `pending_completion`、`pending_review`、`rejected` 或 `expired`；只有 `verified` 可进入职位中心和后续匹配快照。
- 腾讯重同步不得覆盖已经进入人工流程的规范字段；只能更新 `source_candidate` 并使旧审核版本失效。
- 管理员职位写操作必须校验 `review_version`，并在同一事务追加一条 `JobVerification`；原因码只能使用领域 allowlist。
- 单页招聘表单不得误判为多页后自动点击保存/提交。
- 缺失字段和不确定字段应继续填写确定内容，最后统一上报用户，不应阻塞整个自动填写流程。

## 13. 相关文档

- [产品与技术设计](./superpowers/specs/2026-07-14-campus-recruitment-career-assistant-design.md)
- [平台基础与权威数据实施计划](./superpowers/plans/2026-07-14-platform-foundation-authoritative-data.md)
- [真实职位同步纵向闭环设计](./superpowers/specs/2026-07-15-real-job-sync-vertical-slice-design.md)
- [真实职位同步纵向闭环实施计划](./superpowers/plans/2026-07-15-real-job-sync-vertical-slice.md)
- [MVP 并行交付设计](./superpowers/specs/2026-07-16-mvp-parallel-delivery-design.md)
- [职位补全与审核实施计划](./superpowers/plans/2026-07-16-job-completion-review-vertical-slice.md)
- [平台基础运行手册](./runbooks/platform-foundation.md)
- [项目 README](../README.md)
