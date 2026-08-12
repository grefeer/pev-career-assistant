# 前端测试与前后端联调测试计划

项目：Personal Career Agent  
日期：2026-08-11  
测试对象：Vue 3 前端、FastAPI API、MySQL/Redis/MinIO、PEV Planner–Executor–Verifier 运行时  
执行账号：gaoshuoqian  
状态：计划，尚未执行

## 1. 结论与测试目标

本计划覆盖当前仓库真正存在的三条前端主线：

    登录/认证
      → Profile：已有简历、资产状态、解析、证据校对、确认版本
      → Assistant：创建 PEV Run、SSE 实时进度、计划、工件、waiting_user 恢复

测试目标不是只确认“页面能打开”，而是确认每条用户操作都能正确穿过：

    浏览器 UI → Vue API client → FastAPI route → service → repository/storage/runtime
             ← HTTP DTO / SSE event / MySQL authoritative state ←

验收重点：

1. 登录状态、路由守卫和 JWT 失效行为正确。
2. 已有简历不会被默认覆盖或删除。
3. 简历资产、解析证据、校对决定、确认版本的状态变化与后端一致。
4. Assistant 创建的 Run 能经过 queued → running → succeeded，或按设计安全进入 waiting_user / failed。
5. SSE 事件顺序、断线重连和切换任务时的旧事件隔离正确。
6. 计划和工件只显示后端返回的 owner-safe 数据，不泄露私有上下文、token、完整简历或模型内部推理。
7. 后端不可用、模型未配置、Redis/SSE 中断、并发版本冲突等异常能被前端转成可理解的状态。

## 2. 当前项目范围

### 2.1 前端页面与路由

| 页面 | 路由 | 主要能力 | 组件 |
|---|---|---|---|
| 登录/注册 | /login | 登录、注册、错误提示、成功跳转 | frontend/src/components/LoginPage.vue |
| Assistant | /assistant | 自然语言目标、技能选择、候选 URL、Run 历史、SSE、计划、工件、恢复 | frontend/src/features/agent-workspace/AgentWorkspace.vue |
| Profile | /profile | 简历资产、上传、同步、解析、证据决定、确认版本、激活版本、删除 | frontend/src/features/profile/ProfileWorkspace.vue |

未知路由按当前 Router 设计重定向到 /assistant。未发现当前前端提供管理员职位审核页面，因此本轮不把已退休或未挂载的后台页面列为前端验收范围。

### 2.2 后端接口范围

#### 认证

| 方法 | 路径 | 预期用途 |
|---|---|---|
| POST | /api/auth/register | 注册 |
| POST | /api/auth/login | 登录 |
| GET | /api/auth/me | 启动时恢复用户 |

#### 简历与 Profile

| 方法 | 路径 | 预期用途 |
|---|---|---|
| POST/GET | /api/resume-assets | 上传/列出简历资产 |
| GET | /api/resume-assets/{asset_id} | 查看资产 |
| GET | /api/resume-assets/{asset_id}/download | 下载资产 |
| POST | /api/resume-assets/{asset_id}/reconcile | 同步资产状态 |
| DELETE | /api/resume-assets/{asset_id} | 删除资产及关联解析证据 |
| POST/GET | /api/resume-imports | 创建/查看简历解析 |
| GET | /api/profiles | 查看当前用户 Profile |
| PATCH | /api/profiles/evidence | 保存证据校对决定 |
| PATCH | /api/profiles/local-sensitive-references | 更新受保护引用 |
| POST/GET | /api/profile-versions | 创建/列出确认版本 |
| GET | /api/profile-versions/{version_id} | 查看确认版本 |
| POST | /api/profile-versions/{version_id}/activate | 激活版本 |

#### PEV Agent Runtime

| 方法 | 路径 | 预期用途 |
|---|---|---|
| GET/POST | /api/agent-runs | 查询/创建用户 Run |
| POST | /api/agent-runs/{run_id}/resume | 回复 waiting_user |
| POST | /api/agent-runs/{run_id}/recover | 从持久化检查点恢复 running Run |
| GET | /api/agent-runs/{run_id} | 查询 Run |
| GET | /api/agent-runs/{run_id}/events | 查询事件 |
| GET | /api/agent-runs/{run_id}/events/stream | SSE 事件流 |
| GET | /api/agent-runs/{run_id}/plans | 查询 Planner 计划 |
| GET | /api/agent-runs/{run_id}/artifacts | 查询用户可见工件 |

## 3. 测试数据与安全边界

### 3.1 账号

- 使用用户提供的已有账号 gaoshuoqian。
- 密码只在实际执行时通过计算机界面或内存变量输入。
- 不把密码写入仓库、PowerShell 命令行、测试报告、截图、日志或聊天记录。
- 不在浏览器 DevTools、HAR、网络录制或截图中保存登录响应 token。

### 3.2 简历基线

账户里已经有简历，因此第一次进入 /profile 时必须先建立只读基线：

- 资产数量、原始文件名、类型、大小、状态、error_code。
- 当前 Profile version、证据条数及状态分布：pending / confirmed / corrected / ignored。
- latest_version 和 active_version_id。
- 当前页面是否存在可解析资产、可保存决定、可创建版本的按钮。

用户提供的可选上传输入为：

    C:\Users\Grefer\Desktop\高硕谦-东北大学-控制科学与工程-硕士-男-简历 -.pdf

处理规则：

1. 默认不上传，因为上传会新增资产并可能产生新的解析证据。
2. 如需测试上传/解析，先记录旧资产 ID 和状态，再以新增资产路径执行。
3. 默认不删除原有资产，也不删除新资产；删除用例列为需确认的破坏性用例。
4. PDF 只从本地读取或通过 UI 文件选择器提交，不复制到仓库、不打印原文、不把全文写入测试报告。
5. 测试报告只记录文件名、文件类型、大小、状态、错误码、资产 ID 的脱敏后缀或哈希。

### 3.3 PEV 测试数据

- 首轮使用不需要真实外部职位页面的最小目标，确认 UI/API 链路。
- 真实证据链测试使用仓库现有 live test 中的官方公开 JD URL 列表，优先选择执行前仍返回公开页面的 1–2 个 URL。
- 页面返回登录、验证码或反爬时立即记录 login_required / captcha / anti_bot 等结果，不尝试绕过。
- 不使用退休的 Supervisor/Web Navigation/独立 LangGraph job-discovery 作为当前默认运行时验收对象。

## 4. 环境准备与前置检查

### 4.1 推荐运行模式：Docker Compose

项目交接文档记录的当前开发端口为：

> 执行时必须以实际 Compose 映射为准。本轮实际检测到前端 5173、后端 8000、MinIO 9000/9001、Redis 6379；交接文档中的 15173/18000 未监听。

| 服务 | 地址 |
|---|---|
| 前端 | http://127.0.0.1:15173 |
| 后端 | http://127.0.0.1:18000 |
| MySQL | 127.0.0.1:3307 |
| Redis | 127.0.0.1:6380 |
| MinIO API | http://127.0.0.1:19000 |
| MinIO Console | http://127.0.0.1:19001 |

执行前检查：

    docker compose -p platform-foundation ps
    Invoke-RestMethod http://127.0.0.1:18000/api/health/live
    Invoke-RestMethod http://127.0.0.1:18000/api/health/ready
    Invoke-WebRequest http://127.0.0.1:15173 -UseBasicParsing

预期：

- live 返回 HTTP 200。
- ready 返回 HTTP 200，MySQL、Redis、object store 为 up。
- 前端首页返回 HTTP 200，SPA 资源能加载。
- docker compose -p platform-foundation ps 中 migrate 已成功完成，backend/frontend 为 healthy 或运行中。

不要执行以下操作作为常规准备步骤：

- docker compose down -v。
- 删除 MySQL/MinIO/Redis volume。
- Alembic downgrade/upgrade roundtrip。
- 清空用户表、资产表或 profile 数据。

这些操作会改变已有数据，只有单独的隔离测试库和明确批准时才允许执行。

### 4.2 前端自动化基线

在确认 Node 依赖有效后执行：

    npm.cmd --prefix frontend run test
    npm.cmd --prefix frontend run typecheck
    npm.cmd --prefix frontend run build

项目 Vite 配置把前端覆盖率门槛设为语句、分支、函数、行均 100%；若运行覆盖率门禁，应再执行：

    npm.cmd --prefix frontend run test:coverage

### 4.3 后端定向基线

    .\.venv\Scripts\python.exe -m pytest tests/unit/test_auth*.py tests/unit/test_profiles*.py tests/unit/test_profile*.py -q
    .\.venv\Scripts\python.exe -m pytest tests/unit/test_agent_runtime*.py tests/unit/test_*pev_skill.py tests/unit/test_job_matching_skill.py -q

有真实依赖时，再按门禁执行：

    .\.venv\Scripts\python.exe -m pytest tests/integration/test_profile_lifecycle_mysql.py tests/integration/test_object_store.py -q

真实模型测试必须显式设置 RUN_LIVE_PEV_E2E=1 或对应 smoke gate，并通过环境变量传入 LIVE_RESUME_PDF；不要把密码或 PDF 内容写进参数。

## 5. 测试策略与优先级

| 层级 | 内容 | 优先级 | 工具 |
|---|---|---:|---|
| L0 | TypeScript、Vue 单元、API client、构建 | P0 | Vitest、vue-tsc、Vite |
| L1 | 后端 API 契约、状态码、所有权、数据层 | P0 | pytest、FastAPI client、MySQL/MinIO/Redis 检查 |
| L2 | 浏览器 UI smoke 和完整用户旅程 | P0 | computer-use，必要时浏览器 Network/Console |
| L3 | 真实 PEV、公开 JD、简历解析和 SSE 恢复 | P1 | computer-use + API/日志 + gated live pytest |
| L4 | 破坏性删除、故障注入、并发冲突、性能 | P2 | 隔离数据、脚本、受控故障注入 |

P0 未通过时不进入 P1/P2。P2 不作为“已有账号正常功能通过”的必要条件。

## 6. 前端自动化与 UI 测试用例

### 6.1 L0：自动化回归

| ID | 场景 | 操作 | 通过标准 |
|---|---|---|---|
| FE-001 | API JSON 请求 | 执行 shared API client 测试 | 自动设置 Content-Type: application/json，带 Bearer 时设置 Authorization |
| FE-002 | Multipart 上传 | 执行上传 API client 测试 | 不手动设置 JSON Content-Type，浏览器生成 multipart boundary |
| FE-003 | 错误归一化 | 覆盖 4xx/5xx、字符串 detail、结构化 detail、无 JSON | ApiError 消息可读，不显示内部 traceback |
| FE-004 | 登录组件 | 运行 LoginPage 测试 | 登录/注册控件切换、成功提示、失败提示、成功跳转 / |
| FE-005 | 路由守卫 | 运行 guards/auth 测试 | 未登录访问受保护页跳 /login；token 无效时清理本地状态 |
| FE-006 | Profile 组件 | 运行 ProfileWorkspace 测试 | 资产、证据状态、决定按钮、确认版本和 409 分支可渲染 |
| FE-007 | Assistant 组件 | 运行 AgentWorkspace 测试 | Run 历史、计划、事件、工件、waiting_user、recover、SSE 错误可渲染 |
| FE-008 | 类型与生产构建 | 运行 typecheck/build | 无 TypeScript 错误，Vite 产物成功生成 |

### 6.2 L2-UI-A：登录与会话

| ID | 场景 | 操作 | 通过标准 |
|---|---|---|---|
| UI-A01 | 首次打开 | 清理浏览器站点数据后打开 /login | 登录页完整显示，不访问需要认证的业务接口 |
| UI-A02 | 登录成功 | 输入账号和密码，点击“进入工作台” | 显示“登录成功”，跳转 /assistant，左侧出现 Assistant/Profile/Logout |
| UI-A03 | 页面刷新 | 登录成功后刷新浏览器 | /api/auth/me 成功，仍保持登录，不回到登录页 |
| UI-A04 | 错误密码 | 输入错误密码 | 页面显示安全错误，不显示 token、SQL、堆栈或密码原文 |
| UI-A05 | 空账号/空密码 | 不填或只填空白字符提交 | 按后端校验返回可读错误；不创建 Run，不进入业务页 |
| UI-A06 | 注册入口 | 切换“注册” | 显示昵称字段；切换回登录时昵称字段隐藏，登录输入不被错误复用 |
| UI-A07 | 重复账号 | 注册已有账号 | 显示“账号已存在”或等价安全消息，不覆盖已有账号 |
| UI-A08 | 退出登录 | 点击 Logout | 清理 job_assistant_token，跳 /login；浏览器后退不能恢复业务数据 |
| UI-A09 | token 失效 | 清理/替换 token 后刷新受保护页面 | /api/auth/me 失败，自动清理本地 token，回登录页 |
| UI-A10 | 路由边界 | 访问 /profile、/assistant、未知路径 | 已登录能进入对应页面；未知路径按 Router 规则进入 Assistant |

### 6.3 L2-UI-P：Profile 与简历生命周期

#### 基线读取，不改变数据

| ID | 场景 | 操作 | 通过标准 |
|---|---|---|---|
| UI-P01 | 加载 Profile | 登录后进入 /profile | Profile、资产、版本三个请求均成功；加载结束后不显示错误 |
| UI-P02 | 已有资产 | 查看“简历资产” | 显示当前已有资产的文件名、类型、大小、状态、错误码；无资产误报时除外 |
| UI-P03 | 已有证据 | 查看“证据校对” | 证据按当前选中的 import 显示，状态标签与后端一致 |
| UI-P04 | 按钮状态 | 不做任何决定 | “保存校对”“创建确认版本”按当前状态正确禁用 |
| UI-P05 | 版本状态 | 查看“已确认版本” | 版本号、创建时间、当前版本状态可读；激活按钮可用性正确 |

#### 新增上传/解析，默认不删除旧资产

以下用例必须先记录旧资产 ID，且在执行前确认允许给账号新增数据。

| ID | 场景 | 操作 | 通过标准 |
|---|---|---|---|
| UI-P06 | 上传 PDF | 选择用户提供的 PDF | 请求为 multipart；页面显示上传中，成功后显示“上传成功”；旧资产仍存在 |
| UI-P07 | 上传后同步 | 观察上传后的 reconcile | 资产状态按后端返回变化；前端不伪造 ready 状态 |
| UI-P08 | 解析 ready 资产 | 点击“解析” | 解析成功显示“解析完成”，重新加载证据，选中新的 import |
| UI-P09 | 非 ready 资产 | 对 failed/pending 资产观察“解析” | 按组件规则禁用或拒绝操作，不发错误的解析请求 |
| UI-P10 | unsupported file | 选择不支持扩展名 | 前端 accept 限制和后端校验共同生效；显示安全失败；旧资产不变 |
| UI-P11 | oversize file | 使用超过 10 MiB 的隔离临时文件 | 后端返回大小限制错误；不新增可用资产，不污染 Profile |
| UI-P12 | 上传请求失败 | 断开后端或模拟 5xx | 显示“上传失败”或后端安全消息；loading 解除；文件选择框可重新选择 |

#### 证据决定与确认版本

| ID | 场景 | 操作 | 通过标准 |
|---|---|---|---|
| UI-P13 | confirm | 选择一个 pending 证据的“确认” | 行状态变为本地待保存；保存按钮启用；离开页面出现 dirty 提醒（若外层接入） |
| UI-P14 | ignore | 选择“忽略”并保存 | 请求带正确 evidence ID/action；后端状态为 ignored |
| UI-P15 | correct | 选择“更正”，输入普通字符串并保存 | corrected_value 按字符串提交，后端状态为 corrected |
| UI-P16 | correct JSON | 输入合法 JSON（如数组/对象）并保存 | 页面按 JSON 解析后提交结构化值 |
| UI-P17 | correct 空值 | 选择更正但不填值 | 保存按钮保持禁用，不能提交空 corrected_value |
| UI-P18 | 撤销本地决定 | 再次点击同一决定 | 本地决定移除，保存按钮恢复禁用 |
| UI-P19 | 全部处理后创建版本 | 所有证据非 pending 后点击创建确认版本 | 返回新版本号；Profile aggregate version 更新；页面刷新后版本仍存在 |
| UI-P20 | 部分 pending | 只处理部分证据 | “创建确认版本”保持禁用 |
| UI-P21 | 409 并发冲突 | 两个标签页分别基于旧 version 保存 | 页面显示“档案已被其他操作更新”，清空本地决定并重新加载 |
| UI-P22 | 激活版本 | 点击某个确认版本“设为当前版本” | active_version_id 更新，显示成功；刷新后仍一致 |
| UI-P23 | 删除确认 | 点击删除但在 confirm 对话框取消 | 不发 DELETE；资产和证据不变 |
| UI-P24 | 删除受控执行 | 明确批准后删除新上传的隔离资产 | 仅目标资产及其关联证据删除；旧资产和其他用户数据不变 |
| UI-P25 | 下载资产 | 选择已批准的资产下载 | 返回文件流，文件名正确；不在页面显示 token 或完整内容日志 |

### 6.4 L2-UI-A：Assistant 与 PEV Run

| ID | 场景 | 操作 | 通过标准 |
|---|---|---|---|
| UI-R01 | 初始加载 | 进入 /assistant | 加载最近 Run；无历史时显示空状态，不报错 |
| UI-R02 | 目标必填 | 不填目标提交 | 按钮禁用或前端阻止提交；不发送 POST |
| UI-R03 | 技能选择 | 逐个取消/恢复四个 skill | 页面正确显示选中状态；无 skill 时不能提交 |
| UI-R04 | 候选 URL | 输入多行 URL，含空行和首尾空格 | 提交前 trim/filter；后端 context 只包含有效 URL |
| UI-R05 | 创建 Run | 输入最小合法目标并提交 | 返回 201 和 run ID；页面显示 queued/running；开始加载详情和事件 |
| UI-R06 | 默认四技能目标 | 使用岗位+匹配+简历+面试目标 | Planner 计划中的每个 step 只绑定一个 skill；不因混合目标导致工具不可见 |
| UI-R07 | SSE 实时事件 | Run 运行时观察 Agent 活动 | 事件按 sequence 升序显示；不重复；能显示 Planner/Executor/Verifier 活动 |
| UI-R08 | 完成 Run | 等待 succeeded | 显示成功状态、最终摘要、计划和对应 artifacts；来源 URL/hash 可见但无私有上下文 |
| UI-R09 | 工件展示 | 查看岗位证据、结构化 JD、匹配、简历建议、准备计划 | 标题、来源、摘要、审阅项可读；未知或字段缺失时显示安全 fallback |
| UI-R10 | waiting_user | 使用会触发补充信息或预算暂停的运行 | 显示 waiting_user、问题/摘要、回复框和“继续任务” |
| UI-R11 | resume | 输入用户补充后继续 | POST /resume 带 trim 后 user_response；状态重新更新；空回复不发请求 |
| UI-R12 | running recover | 选中 running Run 点击“从检查点恢复” | POST /recover 空 JSON；页面显示恢复中；不接受浏览器自带的新上下文 |
| UI-R13 | SSE 断线 | 在事件流期间刷新/模拟网络中断后重连 | 使用最后持久化 sequence 继续，不重复渲染旧事件 |
| UI-R14 | 切换 Run | 在 Run A 事件流未结束时切到 Run B | A 的迟到事件不能修改 B 的事件、计划、工件或状态 |
| UI-R15 | 运行时不可用 | 关闭 harness 或移除模型 key 后创建 Run | 页面显示“智能求职助手暂不可用，请稍后重试”；不显示 traceback |
| UI-R16 | 后端 500 | 模拟非 harness 5xx | 页面显示安全的后端错误消息；当前 Run 选择和历史不丢失 |
| UI-R17 | 长文本 | 输入接近 8,000 字目标 | 能提交合法上限；超过上限返回 422 并显示可读错误，不锁死 loading |
| UI-R18 | 退出中断 | Run 运行时退出登录 | 事件流 abort；token 清理；重新登录后只能看到服务端已持久化的 owner 数据 |

### 6.5 L2-UI-Q：视觉、响应式和可用性

| ID | 场景 | 检查 |
|---|---|---|
| UI-Q01 | 1280×800 | 登录、Profile、Assistant 无横向溢出，主要按钮可见 |
| UI-Q02 | 900px 附近 | Assistant 单栏布局、导航折叠/换行正常 |
| UI-Q03 | 375×812 | 输入框、textarea、上传、确认按钮可操作；状态不被遮挡 |
| UI-Q04 | 键盘操作 | Tab 顺序合理，Enter 提交表单，Esc/取消可用 |
| UI-Q05 | 加载与禁用 | 所有异步操作期间 loading/disabled 清晰，失败后恢复可操作 |
| UI-Q06 | 错误可读性 | 中文提示不包含内部对象、token、完整简历或原始异常堆栈 |
| UI-Q07 | 浏览器 Console | 完整 smoke 期间无未处理 Promise、Vue runtime error 或持续 4xx 噪音 |

## 7. 前后端 API 联调用例

API 联调不能只看页面文字；每个用例至少同时记录 HTTP 状态、响应 DTO、页面结果和必要的持久化状态。

### 7.1 认证契约

| ID | 请求 | 预期 |
|---|---|---|
| INT-A01 | POST /api/auth/login 正确账号密码 | 200；ok=true；返回 token/profile；不返回密码 |
| INT-A02 | 错误密码 | 401 或项目定义的认证错误；不泄露账号是否存在的过多信息 |
| INT-A03 | 空/过短字段 | 422；字段错误可读；不创建会话 |
| INT-A04 | GET /api/auth/me 无 token | 401 |
| INT-A05 | GET /api/auth/me 失效 token | 401；前端清理 token 并跳转登录 |
| INT-A06 | 重复注册 | 安全错误；不覆盖已有用户 |
| INT-A07 | auth rate limit | 连续失败超过阈值后返回限流错误；前端不无限重试 |

### 7.2 简历/Profile 契约

| ID | 请求/状态 | 预期 |
|---|---|---|
| INT-P01 | GET /resume-assets | 只返回当前用户资产；DTO 不含密文对象、密钥或原文 |
| INT-P02 | multipart PDF 上传 | 201；返回资产 metadata；对象进入 MinIO，数据库有对应记录 |
| INT-P03 | unsupported type | 4xx；不残留可用资产 |
| INT-P04 | 超过 10 MiB | 4xx；不残留可用资产 |
| INT-P05 | reconcile | 状态转换符合服务层规则；前端刷新后与数据库一致 |
| INT-P06 | start import | 201；asset_id 属于当前用户；import 与资产关联正确 |
| INT-P07 | GET /profiles | 证据、版本和敏感引用均为 owner-safe DTO |
| INT-P08 | evidence patch | 正确 expected_version 成功并递增 version；错误版本返回 409 |
| INT-P09 | evidence extra field | Schema extra=forbid 时返回 422 |
| INT-P10 | profile version | 只允许合法、已关联的 import；创建后快照和 evidence refs 可回读 |
| INT-P11 | activate version | 目标 version 属于当前用户；激活后 Profile active_version_id 一致 |
| INT-P12 | download | 需要认证和 owner 校验；只返回目标资产文件流 |
| INT-P13 | delete | 需要认证和 owner 校验；删除范围只包含目标资产及明确关联证据 |

### 7.3 Agent Runtime 契约

| ID | 请求/状态 | 预期 |
|---|---|---|
| INT-R01 | POST /agent-runs 最小目标 | 201；状态 queued；返回 run ID，不返回私有 context |
| INT-R02 | 空目标/超过 8,000 字 | 422；不创建 Run |
| INT-R03 | 非法 skill | 由服务/领域校验拒绝；不把越权 skill 交给 Executor |
| INT-R04 | harness disabled | 503 agent_harness_disabled；前端显示安全提示 |
| INT-R05 | model key 缺失 | 503 agent_harness_unavailable；应用不崩溃 |
| INT-R06 | Run detail | 只返回当前用户 Run；状态、摘要、错误码可回读 |
| INT-R07 | events list | sequence 单调；payload 有界；不含 prompt、私有上下文、密钥 |
| INT-R08 | SSE 首次连接 | text/event-stream；事件 ID/sequence 可作为游标 |
| INT-R09 | SSE reconnect | 使用 after_sequence 或 Last-Event-ID 只返回后续事件 |
| INT-R10 | plans | 返回 Planner safe projection；每个 step 的 skill 数量符合单 skill 约束 |
| INT-R11 | artifacts | 只返回工具产生的、owner-scoped 工件；source_url/content_hash 可验证 |
| INT-R12 | resume | 只允许当前用户的 waiting_user Run；空 response/错误状态拒绝 |
| INT-R13 | recover | 只允许当前用户的 running Run；body 不能注入客户端上下文 |
| INT-R14 | terminal resume/recover | 对 succeeded/failed/cancelled 等终态返回 409 或项目定义错误 |

### 7.4 所有权与安全隔离

至少准备第二个隔离用户或使用已有测试用户，验证：

| ID | 操作 | 预期 |
|---|---|---|
| INT-S01 | 用户 B 查询用户 A 的 asset ID | 404/403，不返回 metadata |
| INT-S02 | 用户 B 下载用户 A 的 asset | 404/403，不返回文件流 |
| INT-S03 | 用户 B 查询/恢复用户 A 的 run | 404/403，不返回事件、计划、工件 |
| INT-S04 | 用户 B 激活用户 A 的 profile version | 404/403，不改变 A 的 active version |
| INT-S05 | 未授权访问所有业务 API | 401，不返回业务数据 |
| INT-S06 | 观察前端、SSE、日志 | 不出现密码、Bearer token、API key、完整 PDF 原文或模型私有 prompt |

## 8. 数据与依赖一致性验证

### 8.1 MySQL 权威状态

每个会改变状态的用例都按以下顺序核对：

1. 记录操作前的实体 ID、状态和 version。
2. 执行浏览器操作。
3. 读取 API 响应和页面状态。
4. 在隔离连接或项目已有测试 fixture 中核对数据库记录。
5. 刷新页面，确认 UI 不是只依赖本地状态。

重点核对：

- ResumeAsset 状态、asset/import 关联。
- Profile aggregate version、证据 status、确认版本快照。
- AgentRun 状态、Plan revision、Step status、Event sequence、Artifact 类型。
- waiting_user → running 和 running → succeeded/failed 状态转换。

### 8.2 Redis 与 SSE

- Redis 只作为通知/缓存/临时 checkpoint 的辅助，不作为业务最终状态来源。
- Redis 暂时不可用时，验证 MySQL 查询和已持久化 Run 不被伪造或清空。
- SSE 断线后用 MySQL 事件游标恢复，不重复显示旧事件。

### 8.3 MinIO/object store

- 上传成功必须同时能看到 API asset metadata 和 object store 可读状态。
- object store 不可用时，前端显示失败并允许重试；不能报告“上传成功”。
- 不在 MinIO Console 或日志中公开显示密钥、加密对象内容或用户完整简历。

## 9. 异常、恢复与安全测试

### 9.1 依赖故障

| 场景 | 注入方式 | 预期 |
|---|---|---|
| 后端不可达 | 停止 backend 或切换代理目标 | 页面显示请求失败；loading 恢复；不产生假成功 |
| MySQL 不可用 | 仅在隔离环境停止 MySQL | ready 失败；业务操作不写入半成品 |
| Redis 不可用 | 仅在隔离环境停止 Redis | 按项目规则限流/SSE/临时状态失败或降级；MySQL 权威状态不被覆盖 |
| MinIO 不可用 | 仅在隔离环境停止 MinIO | 上传/下载/ready 检查显示明确错误 |
| 模型服务超时 | 使用 gated live 配置或受控超时 | Run 进入可解释的 waiting_user/failed，不出现无限 spinner |
| SSE 断线 | 浏览器刷新或中断网络 | 重连游标有效，事件不丢失或重复到不可接受程度 |

### 9.2 PEV 约束

- Executor 只能调用当前 PlanStep skill 范围内的工具。
- Verifier 失败、重试超限、预算耗尽和 invalid model response 必须映射到设计规定的状态。
- 连续重复成功工具调用不会无限消耗预算；三次无进展后应进入需要用户处理的路径。
- 只允许公开页面证据进入可持久化证据链；模型自报 URL 不应成为可信 evidence。
- 不测试绕过登录、验证码、反爬，也不测试任何自动点击外部求职网站最终提交的行为。

## 10. 推荐执行顺序

### 阶段 0：环境与基线

1. 读取当前 git 状态，避免覆盖用户已有修改。
2. 检查 Compose 服务、live/ready、首页 HTTP 200。
3. 运行前端 test/typecheck/build。
4. 登录 gaoshuoqian，仅读取采集 Profile 和已有简历基线。
5. 记录测试开始时间、代码版本、Compose 服务状态和环境端口。

### 阶段 1：P0 自动化与认证

1. 执行 L0 前端测试。
2. 执行 auth/profile/agent runtime 定向后端测试。
3. 执行 UI-A01–UI-A10。
4. 发现认证、路由守卫或 API client 问题时停止，不进入简历写操作。

### 阶段 2：P0 只读业务验收

1. 执行 UI-P01–UI-P05，确认已有简历和 Profile 基线。
2. 执行 UI-R01–UI-R05，创建最小 Run。
3. 验证 Run 历史、状态、事件列表和基础错误处理。

### 阶段 3：P1 PEV 完整链路

1. 使用一个已验证可访问的公开 JD URL。
2. 执行岗位发现、匹配、简历定制、面试准备目标。
3. 观察 SSE、Planner 计划、Executor/Verifier 事件和最终工件。
4. 对 waiting_user、resume、recover、SSE 重连各至少执行一次。
5. 用 API 和数据库证据核对状态、事件 sequence、artifact type/source/hash。

### 阶段 4：P1 简历新增上传分支

只有在确认允许给已有账号新增资产后：

1. 选择用户提供的 PDF，记录旧资产不变。
2. 验证 upload → reconcile → import → evidence load。
3. 选择少量无敏感字段的证据做 confirm/correct/ignore，验证保存和 version 递增。
4. 需要确认版本时创建一个版本并验证激活。
5. 不删除旧资产；新资产也保留，除非用户明确要求清理。

### 阶段 5：P2 受控故障与清理

1. 在隔离数据或测试库执行故障注入、并发冲突和跨用户隔离。
2. 破坏性删除只针对明确指定的新测试资产，并先执行 confirm 对话框取消分支。
3. 记录所有新增资产、Run ID、Profile version，形成可逆清理清单。

## 11. 证据记录格式

每个用例记录以下字段：

    Case ID:
    执行时间:
    代码版本:
    环境地址:
    前置数据摘要:
    操作步骤:
    HTTP 状态/接口:
    页面实际结果:
    数据库/对象存储/事件核对:
    截图路径（已脱敏）:
    日志路径（已脱敏）:
    结果: PASS / FAIL / BLOCKED / NOT RUN
    失败原因与关联 Run/Asset/Version ID:

允许记录：状态、错误码、sequence、artifact_type、source_url、content_hash、脱敏 ID。  
禁止记录：密码、Bearer token、API key、完整简历文本、原始 prompt、完整私有 context、完整 OCR 文本。

建议目录：

    test-evidence/2026-08-11/
      environment.txt
      frontend/
      api/
      profile/
      agent-runs/
      screenshots/

证据目录不应提交到 Git；若必须长期保存，只保存脱敏后的摘要和哈希。

## 12. 通过标准与停止条件

### P0 通过标准

- 前端 test、typecheck、build 通过。
- 登录、刷新、退出、路由守卫通过。
- /profile 能正确读取已有资产和 Profile，不发生意外写入。
- Assistant 能创建 Run，且至少能正确显示 queued/running/终态或安全不可用状态。
- API 未发现跨用户数据泄露、token/密码泄露、错误的成功提示。
- 无 P0 级 Console error、未处理 Promise 或永久 loading。

### P1 通过标准

- SSE 事件顺序、断线重连、Run 切换隔离通过。
- waiting_user 回复和 running recover 通过。
- 真实或受控的 Profile upload/import/evidence/version 链路通过。
- PEV 工件类型、来源、哈希和页面展示一致。

### 立即停止

- 发现已有简历被删除、覆盖或关联证据丢失。
- 发现用户 A 可读取或修改用户 B 的 asset、Profile、Run、事件或工件。
- 发现密码、token、API key、完整简历或私有 prompt 写入日志/截图/响应。
- 页面或 Agent 尝试绕过登录、验证码、反爬或自动点击最终投递提交。
- 数据库出现无法解释的批量写入、状态回退或版本跳跃。
- 运行环境疑似连接生产库、生产 MinIO 或非测试数据库。

## 13. 交付物

本轮计划执行完成后应产出：

1. P0/P1 用例结果表。
2. 前端 test/typecheck/build 输出摘要。
3. API 联调结果与失败响应样例（已脱敏）。
4. 关键用户旅程截图：登录、Profile 基线、Assistant Run、SSE/终态、工件/恢复。
5. 数据一致性核对摘要：资产、Profile version、Run、Event、Artifact。
6. 缺陷清单：严重级别、复现步骤、接口/页面、证据路径、是否阻塞。
7. 新增资产和 Run 的清理清单；未经批准不删除任何已有数据。

## 14. 最小下一步

实际执行时先做以下不改变业务数据的检查：

    docker compose -p platform-foundation ps
    Invoke-RestMethod http://127.0.0.1:18000/api/health/live
    Invoke-RestMethod http://127.0.0.1:18000/api/health/ready
    npm.cmd --prefix frontend run test
    npm.cmd --prefix frontend run typecheck
    npm.cmd --prefix frontend run build

然后使用 computer-use 打开前端，登录 gaoshuoqian，先完成 UI-P01–UI-P05 的只读基线检查；在确认基线前不上传、解析、创建 Profile 版本或删除简历。
