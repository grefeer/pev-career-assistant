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
Invoke-RestMethod 'http://127.0.0.1:8000/api/health/live'
```

就绪检查逐项报告 MySQL、Redis 和对象存储，但不会返回连接 URL 或凭据：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/health/ready' | ConvertTo-Json -Depth 4
docker compose ps
```

## 测试和发布前门禁

先在当前进程构造真实 integration URL。MySQL 使用 `root` 与 User-scope `DB_PASSWORD`；Redis 8 使用 User-scope `REDIS_PASSWORD` 和 DB 0。以下赋值不会回显 URL：

```powershell
$env:TEST_MYSQL_URL = & .\.venv\Scripts\python.exe -c "import os,urllib.parse; print('mysql+pymysql://root:'+urllib.parse.quote(os.environ['DB_PASSWORD'],safe='')+'@127.0.0.1:3307/career_assistant_test?charset=utf8mb4')"
$env:TEST_REDIS_URL = & .\.venv\Scripts\python.exe -c "import os,urllib.parse; print('redis://:'+urllib.parse.quote(os.environ['REDIS_PASSWORD'],safe='')+'@127.0.0.1:6380/0')"
$env:TEST_S3_ENDPOINT = 'http://127.0.0.1:9000'
$env:TEST_S3_ACCESS_KEY = [Environment]::GetEnvironmentVariable('MINIO_ROOT_USER', 'User')
$env:TEST_S3_SECRET_KEY = [Environment]::GetEnvironmentVariable('MINIO_ROOT_PASSWORD', 'User')
$env:TEST_S3_BUCKET = 'career-assistant-storage-test'
```

运行全部门禁：

```powershell
.\.venv\Scripts\python.exe -m ruff check backend src tests scripts
.\.venv\Scripts\python.exe -m pytest -v
npm --prefix frontend run build
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
