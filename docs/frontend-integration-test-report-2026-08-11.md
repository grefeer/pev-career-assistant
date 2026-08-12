# 前端与前后端联调测试报告

日期：2026-08-11  
状态：认证态 Profile/Assistant、简历闭环与 PEV/SSE 联调通过  
执行账号：gaoshuoqian  
业务代码修改：无

## 0. 2026-08-12 启动复核

按项目交接文档的本机覆盖端口重新启动完整 Compose 栈：前端 `15173`、后端 `18000`、MySQL `3307`、Redis `6380`、MinIO `19000/19001`。镜像构建、数据库迁移均成功；后端、MySQL、Redis、MinIO 为 healthy，前端首页、后端 live/ready、MinIO live 均返回 HTTP 200。

在这套当前栈上重新执行登录冒烟，`POST /api/auth/login` 返回 401；通过只读 SQL 核对 `career_assistant.users` 当前为 0 行。因此后续认证态联调仍需接入包含该账号的正确数据库或由项目负责人提供测试账号数据，未自行创建或修改账户。

## 0.1 2026-08-12 账号与界面复测

在用户明确授权后，按项目注册接口创建 `gaoshuoqian`（昵称“高硕谦”），并上传指定 PDF；没有把密码写入仓库、日志或报告。上传资产返回 201、状态 `ready`；解析导入返回 201、状态 `awaiting_confirmation`，资产 1 个、解析证据 6 条。通过下载接口回读 PDF，SHA-256 与本地文件一致。

使用 Playwright 进行登录转场回归（干净浏览器上下文）：登录后立即、200 ms、2 s 和刷新后均为 `/assistant` 的 PEV Assistant 新界面；可见标题、导航、目标表单和旧版 LangGraph/SQLite 标记一致，`same_visible_state_after_login_and_reload=true`。控制台错误 0，网络失败 0。两张截图已保存到 `temp/transition_after_login.png` 和 `temp/transition_after_reload.png`，视觉内容一致。

用户指定的 `browser-use@claude-plugins-official` 在本地工具列表中不可调用，未伪称使用成功；本轮以项目要求的原生 Playwright 降级执行，结果单独记录。

当前 Nginx 对 `/index.html` 返回 `Cache-Control: no-cache`；这与 hashed assets 的发布策略相符，当前构建中未发现需要再次修改的缓存代码。该现象在干净浏览器上下文中未复现，因此本轮没有修改前后端业务代码。

## 0.2 PEV 与 SSE 联调复核

使用同一测试账号发起最小只读任务（不抓取职位、不修改简历、不投递）：

- `POST /api/agent-runs`：201，初始 `queued`。
- 后台执行：进入 `waiting_user`，事件为 `run_started`、`planner_needs_user`。
- 状态、事件、计划、工件接口：均返回 200；初次事件 2 条，计划/工件为空符合等待用户状态。
- SSE 回放：200，`text/event-stream`，包含 `data:`。
- `POST /api/agent-runs/{id}/resume`：200；补充澄清后最终 `succeeded`、复杂度 `L1`。
- 恢复后的事件共 6 条：`run_started`、`planner_needs_user`、`run_resumed`、`plan_created`、`step_succeeded`、`run_succeeded`。
- 刷新 Assistant 页面后，Run 历史按钮出现，Run、事件、计划、工件和 SSE 请求全部返回 200。

复测中曾出现一次 Playwright `networkidle` 超时。后端日志显示相关请求均为 200，原因是 Assistant 在存在历史 Run 时按设计保持 SSE 长连接，`networkidle` 不会成立。已将临时 UI 冒烟脚本改为等待 DOM 和 Assistant 目标表单；修正后回归通过，未修改生产前端或后端。

## 1. 本轮结论

当前代码的自动化基线、登录后的 Profile/Assistant 只读流程、简历资产闭环以及最小 PEV Run/SSE/恢复流程通过。初始空库导致的 401 阻塞已通过用户授权注册测试账号解除。

这不是前端跳转失败。浏览器显示了后端返回的“账号或密码不正确。”，并按设计阻止进入业务页。

## 2. 环境状态

本轮启动了本地 Compose 环境，没有删除 volume，也没有执行数据库重置。

实际端口：

| 服务 | 地址 | 结果 |
|---|---|---|
| 前端 | http://127.0.0.1:5173 | HTTP 200 |
| 后端 live | http://127.0.0.1:8000/api/health/live | 通过 |
| 后端 ready | http://127.0.0.1:8000/api/health/ready | 通过 |
| MySQL | Compose 内部服务 | up |
| Redis | Compose 内部服务 | up |
| MinIO | Compose 内部服务 | up |

ready 响应摘要：

    status=ready
    mysql=up
    redis=up
    object_store=up

环境审计还发现根目录 `.env` 中的 DATABASE_URL 指向 localhost:3307，REDIS_URL 指向 localhost:6380；这两个主机端口本轮均未监听。直接执行 docker compose 时，Compose 使用了容器内 mysql/redis 和默认宿主端口映射，因此连接的是当前空的 platform-foundation_mysql-data 数据卷，而不是一个可确认包含用户数据的旧实例。

交接文档中记录的 18000/15173 端口在本次实际环境中没有监听；当前 Compose 使用默认 8000/5173 映射。

## 3. 自动化测试结果

### 前端

命令：

    npm.cmd --prefix frontend run test
    npm.cmd --prefix frontend run typecheck
    npm.cmd --prefix frontend run build

结果：

- Vitest：10 个测试文件通过，109 个测试通过。
- TypeScript 类型检查：通过。
- Vite production build：通过。
- 生产构建生成 LoginPage、ProfileWorkspace、AgentWorkspace 资源。

### 后端

命令：

    .\.venv\Scripts\python.exe -m pytest tests/unit/test_auth_routes_and_dependencies.py tests/unit/test_profiles_routes.py tests/unit/test_agent_runtime_routes.py tests/unit/test_agent_runtime_service.py -q

结果：

- 120 passed。
- 1 个 Starlette/httpx 弃用警告。
- 无失败用例。

### 提供的 PDF 输入

本轮只在内存中读取了用户提供的 PDF，没有上传到当前账户：

- 文件存在：通过。
- 文件大小：486,846 bytes。
- parser `needs_manual_entry`：false。
- 可提取 evidence candidate：6 条。
- 文本长度：3,747 字符。

这证明 PDF 本身可被当前解析器读取，但不能证明账户中已有的简历资产与该 PDF 相同；账户认证阻塞使资产对比尚未执行。

## 4. 浏览器 smoke 结果

执行脚本：

    temp/ui_smoke_playwright.py

测试方式：

- Python Playwright。
- Chromium headless。
- 真实本地前端地址。
- 未保存密码、token、简历原文或网络响应正文。

结果：

| 检查项 | 结果 | 说明 |
|---|---|---|
| 登录页加载 | PASS | /login 正常渲染，存在登录按钮，2 个输入框 |
| 给定账号登录 | BLOCKED | POST /api/auth/login 返回 401 |
| 登录错误提示 | PASS | 页面显示“账号或密码不正确。” |
| 未登录访问 /profile | PASS | 重定向到 /login |
| 未登录访问 /assistant | PASS | 重定向到 /login |
| Profile 只读基线 | BLOCKED | 需要成功认证 |
| Assistant 只读基线 | BLOCKED | 需要成功认证 |
| Console | 有一个预期 401 资源错误 | 来源为登录失败响应 |

认证 API 摘要：

    POST /api/auth/login
    HTTP 401
    detail: 账号或密码不正确。

只检查了当前用户表的账号和角色摘要，结果为空；没有打印密码、哈希或其他用户私有字段。

## 5. 未执行的测试

以下测试因认证数据缺失未执行，不应被误报为通过：

- 当前账户已有简历资产数量和状态基线。
- Profile 证据校对、correct/ignore/confirm。
- Profile version 创建和激活。
- 简历上传、reconcile、解析。
- 现有简历与提供 PDF 的差异测试。
- Assistant 创建 PEV Run。
- queued → running → succeeded/waiting_user/failed。
- SSE 事件流、断线重连和事件游标。
- Planner 计划、Verifier 结果和 Artifact 展示。
- resume/recover。
- MySQL 资产、Profile version、Run、Event、Artifact 一致性核对。

## 6. 数据安全结果

- 未上传提供的 PDF。
- 未删除已有简历。
- 未创建新的用户或 Profile 版本。
- 未修改用户业务数据。
- 未把密码写入仓库、脚本、日志或报告。
- 未把 token、完整简历、私有 prompt 或完整响应正文写入证据。
- 未修改前端或后端生产代码。

## 7. 代码修复判断

本轮没有发现需要修改前端或后端代码的可复现缺陷：

- 登录失败是后端真实 401，前端错误显示正确。
- 未认证路由守卫行为正确。
- 自动化前端和后端定向回归均通过。
- 当前主要问题是测试环境用户数据与用户提供的账号状态不一致。

不建议通过注册同名新用户、上传 PDF 或删除数据来掩盖这个环境差异，因为这会改变用户原本声明的“已有账户和已有简历”前置条件。

## 8. 继续执行的前置条件

需要恢复包含以下数据的正确测试数据库或提供正确的本地连接配置：

1. 用户 gaoshuoqian。
2. 该用户已有简历资产。
3. 与简历对应的 resume import、evidence 或确认版本数据。
4. 与当前 Compose/后端实际使用的数据库一致。

恢复后重新执行：

    $env:TEST_ACCOUNT='gaoshuoqian'
    $env:TEST_PASSWORD='<interactive-only>'
    .\.venv\Scripts\python.exe temp/ui_smoke_playwright.py

然后继续计划中的 UI-P01–UI-P25、UI-R01–UI-R18 和 INT-P/INT-R 联调矩阵。
