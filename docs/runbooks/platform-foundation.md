# 平台基础运行手册

本文覆盖 MySQL、Redis 8、MinIO、FastAPI 和 Vue 前端的开发与运维操作。所有命令均在仓库根目录执行。不得把 `.env`、命令历史、日志或终端输出中的密钥提交到版本库。

## 数据权威与恢复原则

MySQL 是账户、分析会话、设备、投递任务和审计记录的唯一权威库。Redis DB 0 同时承载 Redis Search/LangGraph checkpoint、一次性配对票据和设备在线状态，但不是投递任务状态的权威来源。删除或丢失 Redis 后，任何 `ApplicationTask` 都不得自动重跑，且其 MySQL `status` 不得被修改；需要恢复的任务必须由操作员依据 MySQL 和审计记录单独判定。

旧版 `data/app_users.json` 不迁移、不导入 MySQL，也不得作为回退数据源。SQLite checkpoint 仅用于本地开发，生产环境必须使用 Redis。

## 生成和设置环境变量

先从模板创建本地文件：

```powershell
Copy-Item .env.example .env
```

使用密码管理器保存下列命令生成的值。命令只生成随机值；不要把输出粘贴到聊天、工单或日志中。

```powershell
$appAuthSecret = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
$dbPassword = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$redisPassword = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$minioRootUser = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(16))"
$minioRootPassword = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
$objectEncryptionKey = & .\.venv\Scripts\python.exe -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

开发机使用 User-scope 的 `DB_PASSWORD`（MySQL `root` 账号）和 `REDIS_PASSWORD`，避免把密码写进脚本或仓库：

```powershell
[Environment]::SetEnvironmentVariable('DB_PASSWORD', $dbPassword, 'User')
[Environment]::SetEnvironmentVariable('REDIS_PASSWORD', $redisPassword, 'User')
[Environment]::SetEnvironmentVariable('MINIO_ROOT_USER', $minioRootUser, 'User')
[Environment]::SetEnvironmentVariable('MINIO_ROOT_PASSWORD', $minioRootPassword, 'User')
[Environment]::SetEnvironmentVariable('APP_AUTH_SECRET', $appAuthSecret, 'User')
[Environment]::SetEnvironmentVariable('OBJECT_ENCRYPTION_KEY', $objectEncryptionKey, 'User')
```

重新打开终端后再启动服务。`.env` 中其余变量以 `.env.example` 为准。`DATABASE_URL` 和 `REDIS_URL` 若需在进程内构造，必须对密码做 URL encode，且不得打印结果：

```powershell
$env:DATABASE_URL = & .\.venv\Scripts\python.exe -c "import os,urllib.parse; print('mysql+pymysql://root:'+urllib.parse.quote(os.environ['DB_PASSWORD'],safe='')+'@127.0.0.1:3306/career_assistant?charset=utf8mb4')"
$env:REDIS_URL = & .\.venv\Scripts\python.exe -c "import os,urllib.parse; print('redis://:'+urllib.parse.quote(os.environ['REDIS_PASSWORD'],safe='')+'@127.0.0.1:6379/0')"
```

这些赋值命令的 stdout 仅进入当前进程环境变量，不要单独运行 `print` URL 的命令，也不要回显变量。

## Compose 启停和可配置端口

默认宿主端口为 MySQL `3306`、Redis `6379`、MinIO API/Console `9000/9001`、后端 `8000`、前端 `5173`。冲突时只在当前终端覆盖宿主端口；容器内部端口不变：

```powershell
$env:MYSQL_HOST_PORT = '3307'
$env:REDIS_HOST_PORT = '6380'
$env:MINIO_HOST_PORT = '9000'
$env:MINIO_CONSOLE_HOST_PORT = '9001'
$env:BACKEND_HOST_PORT = '8000'
$env:FRONTEND_HOST_PORT = '5173'
docker compose up --build -d
docker compose ps
```

开发 Compose 固定使用 `APP_ENV=development`，并把后端宿主端口只绑定到 `127.0.0.1`。浏览器流量应走 Nginx 前端：前端与后端共享独立的固定 `proxy` 网络，Uvicorn 关闭通用 proxy-header 信任，应用只在连接对端属于该网络的 `TRUSTED_PROXY_CIDRS` 时读取由 Nginx 覆写的 `X-Real-IP`，并忽略客户端提供的 `X-Forwarded-For`。不得把 `TRUSTED_PROXY_CIDRS` 设置为 `0.0.0.0/0` 或在公网开放后端 8000 端口。

生产部署必须使用独立 Compose overlay 或编排配置显式设置 `APP_ENV=production`，注入真实对象存储凭据并配置实际反向代理的最小 CIDR；不能直接把本开发 Compose 当作生产配置。登录限流使用“账号摘要低阈值 + 可信客户端 IP 高阈值”，注册使用 IP 桶，Redis key 不保存原账号。反向代理拓扑变化时须先通过伪造 header 和多客户端隔离测试。

停止容器但保留卷：

```powershell
docker compose down
```

`docker compose down -v` 会删除 MySQL、Redis 和 MinIO 卷，只能在确认备份且明确接受数据丢失时使用。

## 数据库迁移 upgrade/downgrade

服务启动时 `migrate` 会先执行 `alembic upgrade head`。手工升级：

```powershell
docker compose run --rm migrate alembic upgrade head
```

`20260716_0004` 会修改 `job_postings` 的状态约束、回填 `source_candidate` 并创建
`job_verifications`。升级必须安排维护窗口：先停止 Backend 和所有写入作业，完成并
校验 MySQL 备份，再观察 `performance_schema.metadata_locks` 与长事务，确认没有阻塞
`job_postings` 的 metadata lock 后执行迁移。迁移期间不得恢复同步或管理员写入。

```powershell
docker compose stop backend
docker compose exec -T mysql sh -c `
  'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -e "SHOW PROCESSLIST; SELECT OBJECT_SCHEMA,OBJECT_NAME,LOCK_TYPE,LOCK_STATUS FROM performance_schema.metadata_locks WHERE OBJECT_SCHEMA = DATABASE();" career_assistant'
docker compose run --rm migrate alembic upgrade 20260716_0004
$revision = docker compose run --rm migrate alembic current
if ($revision -notmatch '20260716_0004') {
  throw 'Unexpected Alembic revision after migration'
}
```

备份必须写入仓库外受控目录，并使用组织批准的加密 `mysqldump` 或物理备份流程；不得
把数据库密码放进 argv、命令历史或输出。确认 revision 与抽样数据后才恢复写入。

从 `20260716_0004` 降到 `20260715_0003` 会永久删除全部 `job_verifications` 和审核
新增列，并把所有职位状态重置为 `pending_completion`；人工审核历史、版本、来源变化
标记、GUI 资格和终态时间均不可由 0003 恢复。只有在已验证备份且明确接受这些损失的
开发或灾难恢复场景才可执行：

```powershell
docker compose stop backend
docker compose run --rm migrate alembic downgrade 20260715_0003
```

回退前必须进入维护窗口、停止后端写入并完成 MySQL 备份。回退到空基线会删除平台表：

```powershell
docker compose stop backend
docker compose run --rm migrate alembic downgrade base
```

验证完回退后，重新升级并启动：

```powershell
docker compose run --rm migrate alembic upgrade head
docker compose up -d backend frontend
```

## 创建管理员

管理员密码通过容器内脚本的隐藏交互提示输入；保持终端交互开启，不要使用 `-T`，也不要把密码放在 argv 或环境变量中：

```powershell
docker compose run --rm backend python scripts/create_admin.py --account admin --nickname Administrator
```

若账号已存在且不是管理员，脚本会拒绝静默提权。

## 腾讯智能表职位同步

职位同步只读访问腾讯智能表，仅调用字段和记录查询能力；后端不会新增、更新或删除腾讯源表数据，也不依赖 `mcporter` 子进程。腾讯是外部内容源，不属于核心平台就绪依赖，因此不会出现在 `/api/health/ready` 的检查项或响应中；未配置腾讯令牌不影响应用启动、登录、已有职位查询或核心就绪状态。

生产同步令牌只从 User-scope 环境变量加载到当前 PowerShell 进程，不要写入 `.env`、命令参数、日志或版本库：

```powershell
$token = [Environment]::GetEnvironmentVariable('TENCENT_DOCS_TOKEN', 'User')
if ([string]::IsNullOrWhiteSpace($token)) {
  throw 'Missing TENCENT_DOCS_TOKEN user environment variable'
}
Set-Item -Path Env:TENCENT_DOCS_TOKEN -Value $token
```

内置来源键固定为：

- `tencent-27-referrals`
- `tencent-intern-referrals`

使用管理员 JWT 手动同步一个来源。JWT 只放在 `Authorization` header 中，不要放入 URL、请求体或命令行参数；以下交互式读取不会回显 JWT：

```powershell
$secureAdminJwt = Read-Host 'Administrator JWT' -AsSecureString
$adminJwt = [System.Net.NetworkCredential]::new('', $secureAdminJwt).Password
$headers = @{ Authorization = "Bearer $adminJwt" }
$sourceKey = 'tencent-intern-referrals'
$result = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/admin/job-sources/$sourceKey/sync" `
  -Headers $headers
$result | Select-Object run_id, source_key, status, pages_read, records_read,
  raw_snapshots_created, postings_created, postings_updated,
  records_skipped_incomplete
$adminJwt = $null
$secureAdminJwt.Dispose()
```

同步状态与 HTTP 状态解释：

- `SUCCEEDED`：全部分页已读取并提交，运行计数可用于核对本次结果。
- `PARTIAL`：至少一页已经提交，后续页失败；已提交的不可变快照和职位保留。修复错误后重新调用同一同步端点，恢复过程会从 page 0 全量重跑，并由唯一约束和幂等更新跳过相同快照。
- `FAILED`：第一页成功提交前同步失败；根据响应中的稳定 `error_code` 修复问题后重试。
- HTTP `409`：同一来源存在未过期同步租约，或来源当前不可同步；等待现有运行结束或租约到期后再试。
- HTTP `502`：腾讯响应协议不符合预期或来源字段结构改变；检查来源结构和后端映射版本，不能通过盲目重试绕过。
- HTTP `503`：令牌缺失、鉴权失败、限流重试耗尽、腾讯服务不可用或数据库写入失败；按稳定 `error_code` 检查配置与依赖状态。
- HTTP `504`：腾讯请求在重试后仍超时；检查网络和上游状态后重新运行。

对于已创建同步运行后由 `JobSyncFailedError` 返回的 5xx 失败响应，`detail` 只应包含稳定 `error_code` 和 `run_id`；404、409 和认证授权错误遵循各自的标准响应。任何响应都不得包含腾讯原始响应、令牌或原始记录载荷，也不得把这些内容复制到日志或工单。`GET /api/jobs` 和 `GET /api/jobs/{job_id}` 需要已认证用户，仅返回 `verified` 职位；待补全、待审核、已拒绝和已失效职位只允许管理员读取。

## 职位补全与核验

职位状态依次使用 `pending_completion`、`pending_review`、`verified`、`expired` 和
`rejected`。学生职位中心只读取 `verified`；待补全、待审核、已拒绝和已失效记录只
通过管理员接口访问。

管理员在前端“职位审核”页完成以下操作：

1. 对照最新来源候选值补全公司、具体岗位、完整 JD、地点、投递入口和截止日期。
2. 保存草稿，使记录进入 `pending_review`。
3. 明确选择“允许 GUI 辅助填写”或“仅人工投递”。
4. 核验并发布，或填写稳定原因后拒绝；失效操作同样必须填写稳定原因。

每次写操作携带 `review_version`。收到 HTTP 409 和 `stale_job_review` 时必须丢弃本地
旧版本、重新加载记录并让管理员重新确认，不得自动重放或重复提交旧内容。来源候选值
只要发生实质变化就递增 `review_version`，因此管理员读取 `pending_completion@v0` 后
发生的来源变化也会使旧 v0 写入变 stale。

在记录尚无任何 `JobVerification` 时，`pending_completion` 的规范字段可随来源候选
刷新；一旦存在人工审核事件，即使状态后来被重置，重同步也只更新
`source_candidate`、`source_changed_since_review=true` 和版本，不会覆盖人工确认字段。
每次成功管理员写入在同一事务中追加且只追加一条不可变 `JobVerification`；失败或 stale
事务不得产生事件。不得编辑或删除核验事件来“修复”当前状态。

邮箱、二维码、扫码、微信和其他人工渠道可以被核验，但必须保持
`gui_eligible=false`。`verify` 请求的 `reason_code` 必须为 `null`；`reject` 和
`expire` 必须显式提交非空白稳定 `reason_code`，服务端不会静默生成默认原因。官网职位
关闭后由管理员执行失效操作；已失效记录不再出现在学生职位中心。

迁移 `20260716_0004` 增加审核状态、版本和 `job_verifications`。降级会删除核验记录并
把全部职位重置为 `pending_completion`，因此只能在确认不需要保留审核历史的开发或恢复
场景执行。

## 撤销设备

先以用户 Bearer token 获取设备列表，再由设备所有者撤销。令牌使用隐藏输入并只保留在当前 PowerShell 进程中：

```powershell
$secureToken = Read-Host 'Bearer token' -AsSecureString
$token = [System.Net.NetworkCredential]::new('', $secureToken).Password
$headers = @{ Authorization = "Bearer $token" }
$devices = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/devices' -Headers $headers
$deviceId = Read-Host 'Device id to revoke'
Invoke-RestMethod -Method Delete -Uri "http://127.0.0.1:8000/api/devices/$deviceId" -Headers $headers
$token = $null
$secureToken.Dispose()
```

撤销后设备 token 立即失效，Redis 中对应在线 key 会删除；设备及撤销审计仍由 MySQL 保存。

## 备份边界

- **MySQL（必须备份）**：权威业务数据和审计记录。使用组织批准的加密备份方案执行一致性 `mysqldump` 或物理备份，并定期演练恢复。凭据只通过环境变量或受保护的客户端配置注入。
- **Redis 8（可恢复性备份）**：checkpoint、Search 索引、配对票据和在线 TTL。RDB/AOF 备份可以缩短会话恢复时间，但不能覆盖或推导 MySQL 的投递任务状态。Redis Search/checkpoint 固定使用 DB 0。
- **对象存储（必须与密钥配套备份）**：备份 bucket 中的密文对象、metadata 和版本；对象存储不含可直接读取的简历明文。`OBJECT_ENCRYPTION_KEY` 必须在独立密钥系统中备份，不能与对象备份放在同一位置。
- Compose 本地卷不是备份。数据库卷、Redis 数据目录、MinIO 数据目录和运行日志都不得提交 Git。

## Redis 丢失恢复

1. 停止后端，记录事故时间范围，并保护 MySQL 和对象存储不被更改。
2. 恢复 Redis 8 服务；如有可信 RDB/AOF，恢复到 DB 0 并验证 Redis Search 可用。没有备份时从空 DB 0 启动。
3. 配对票据和在线状态属于短期数据，允许自然失效/重新签发；设备权威状态仍从 MySQL 读取。
4. checkpoint 丢失的分析会话由用户重新发起分析，不得据此重置、推进或重跑任何 MySQL `ApplicationTask`。
5. 对照 MySQL 审计记录人工处理状态不确定的投递任务；禁止编写“扫描 MySQL 后自动重放任务”的恢复脚本。
6. 运行就绪检查和 Redis integration test 后再恢复流量。

## 密钥轮换前置检查

轮换 `APP_AUTH_SECRET`、`REDIS_PASSWORD`、`DB_PASSWORD`、对象存储凭据或 `OBJECT_ENCRYPTION_KEY` 前必须：

1. 进入维护窗口并确认可回滚版本；完成 MySQL、Redis 和对象密文备份。
2. 确认新旧密钥都保存在受控密钥系统，验证恢复负责人和访问权限。
3. 盘点影响：认证密钥轮换会使现有 JWT 失效；数据库/Redis 密码需同步更新服务端与应用；对象加密密钥不能直接替换，否则旧对象不可解密。
4. 对对象加密密钥制定逐对象“旧密钥解密、新密钥加密”的迁移和断点续传方案，并在隔离副本验证认证标签。
5. 验证日志、进程参数、Compose 配置输出和备份中不出现明文密钥；准备旧密钥回滚窗口。
6. 轮换后运行健康检查、全量测试和抽样对象解密，再撤销旧密钥。

## 健康检查

存活检查不依赖 MySQL、Redis 或对象存储：

```powershell
$backendPort = if ($env:BACKEND_HOST_PORT) { $env:BACKEND_HOST_PORT } else { '8000' }
Invoke-RestMethod "http://127.0.0.1:$backendPort/api/health/live"
```

就绪检查逐项报告 MySQL、Redis 和对象存储，但不会返回连接 URL 或凭据：

```powershell
$backendPort = if ($env:BACKEND_HOST_PORT) { $env:BACKEND_HOST_PORT } else { '8000' }
$frontendPort = if ($env:FRONTEND_HOST_PORT) { $env:FRONTEND_HOST_PORT } else { '5173' }
$revision = docker compose run --rm migrate alembic current
if ($revision -notmatch '20260716_0004') {
  throw 'Compose database is not at 20260716_0004'
}
Invoke-RestMethod "http://127.0.0.1:$backendPort/api/health/ready" |
  ConvertTo-Json -Depth 4
Invoke-WebRequest "http://127.0.0.1:$frontendPort/" -UseBasicParsing
docker compose ps
```

Compose 验证不得假定宿主端口为 8000/5173。检查命令必须使用与启动栈相同的
`BACKEND_HOST_PORT` 和 `FRONTEND_HOST_PORT`；MySQL、Redis 与 MinIO 的宿主端口也分别
由 `MYSQL_HOST_PORT`、`REDIS_HOST_PORT`、`MINIO_HOST_PORT` 和
`MINIO_CONSOLE_HOST_PORT` 控制。`migrate` 容器退出 0 不是 revision 证明，必须额外执行
`alembic current` 并看到 `20260716_0004`。

## 测试和发布前门禁

先在当前进程构造真实 integration URL。MySQL 使用 `root` 与 User-scope `DB_PASSWORD`；Redis 8 使用 User-scope `REDIS_PASSWORD` 和 DB 0。以下赋值不会回显 URL：

```powershell
$env:TEST_MYSQL_URL = & .\.venv\Scripts\python.exe -c "import os,urllib.parse; print('mysql+pymysql://root:'+urllib.parse.quote(os.environ['DB_PASSWORD'],safe='')+'@127.0.0.1:3307/career_assistant_test?charset=utf8mb4')"
$env:TEST_REDIS_URL = & .\.venv\Scripts\python.exe -c "import os,urllib.parse; print('redis://:'+urllib.parse.quote(os.environ['REDIS_PASSWORD'],safe='')+'@127.0.0.1:6380/0')"
$env:TEST_S3_ENDPOINT = 'http://127.0.0.1:9000'
$env:TEST_S3_ACCESS_KEY = [Environment]::GetEnvironmentVariable('MINIO_ROOT_USER', 'User')
$env:TEST_S3_SECRET_KEY = [Environment]::GetEnvironmentVariable('MINIO_ROOT_PASSWORD', 'User')
$env:TEST_S3_BUCKET = 'career-assistant-storage-test'
$env:TEST_TENCENT_DOCS_TOKEN = [Environment]::GetEnvironmentVariable('TEST_TENCENT_DOCS_TOKEN', 'User')
$env:ALLOW_DESTRUCTIVE_MYSQL_TESTS = '1'
```

`ALLOW_DESTRUCTIVE_MYSQL_TESTS=1` 是破坏性 MySQL 门禁的显式开关。共享 guard 会先检查
该开关，再读取 `TEST_MYSQL_URL`，并拒绝空 URL、非 MySQL 后端和数据库名不以 `_test`
结尾的连接。缺少开关或 URL 时测试按精确变量名 skip；配置了不安全值时直接失败。只可
使用 `career_assistant_test` 这类隔离 schema，严禁把业务库 URL 复用为测试 URL。
`TEST_TENCENT_DOCS_TOKEN` 仅用于测试环境中的真实腾讯只读门禁。

运行全部门禁：

```powershell
.\.venv\Scripts\python.exe -m ruff check backend src tests scripts
.\.venv\Scripts\python.exe -m pytest -v
npm --prefix frontend run build
npm --prefix frontend run typecheck
rg "app_users.json|USER_STORE_PATH|replace-with-your-own-secret|password_hash.*sha256|postgres" backend src frontend docker-compose.yml README.md
```

最后一条搜索应无匹配；运行手册中唯一允许出现旧文件名的位置是上方“不迁移”说明，可单独核对：

```powershell
rg -n "app_users.json" docs/runbooks/platform-foundation.md
```

生产负向验证必须以非零状态退出，证明默认认证密钥或 SQLite checkpoint 不能启动生产配置：

```powershell
$env:APP_ENV='production'
$env:APP_AUTH_SECRET='replace-with-your-own-secret'
$env:CHECKPOINT_BACKEND='sqlite'
.\.venv\Scripts\python.exe -c "from backend.app.config import Settings; Settings()"
```
