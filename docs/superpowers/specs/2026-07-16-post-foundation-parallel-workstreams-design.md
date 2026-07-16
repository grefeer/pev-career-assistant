# 平台基础完成后的并行工作流设计

## 0. 文档信息

- 日期：2026-07-16
- 状态：设计章节已获确认，待用户复核成文
- 适用基线：已完成平台基础、腾讯职位同步、职位补全审核和学生 verified 职位中心
- 上游设计：`2026-07-14-campus-recruitment-career-assistant-design.md`
- 当前交接：`docs/platform-foundation-handover-summary.md`

## 1. 背景与命名澄清

项目级 Wave 0 已经完成。当前代码已经具备 MySQL 权威数据、Redis 8 checkpoint、加密对象存储适配器、认证与权限、设备配对、task lease、ApplicationTask 安全状态机、腾讯职位同步、管理员职位补全审核和学生 verified 职位中心。

交接摘要中“Wave 0 共享契约门禁”的表述不再作为新的功能波次使用。下一阶段只保留一个轻量的共享契约检查点，并把它写入每份实施计划的 Task 0。该检查点用于防止并行工作流自行发明冲突的 ID、状态、DTO、事件、隐私字段、migration 和 Executor 协议，不单独形成实施计划。

当前代码仍存在以下明确缺口：

- `Profile`、`ResumeAsset`、`ConfirmedProfileVersion` 和相关证据实体尚不存在。
- 学生不能提交私有 JD 或职位链接，也没有统一来源关系和可解释去重候选。
- Windows Executor 尚未实现，现有代码只有设备、lease 和任务状态机基础。
- 学生不能对 verified 职位提交可追踪反馈。
- FastAPI 分析入口仍从 `data/jobs.json` 读取演示职位，上传简历只在请求内解析。
- `MatchReport`、批准简历版本和 `ApplicationSnapshot` 尚不存在。

## 2. 目标与非目标

### 2.1 目标

下一阶段使用依赖门禁式并行，形成四条可同时推进的 Wave 1 工作流，并在其完成后启动一条 Wave 2 集成工作流：

1. 档案与简历生命周期。
2. 手动 JD 导入、来源合并与统一去重。
3. Windows Executor 骨架与模拟安全门。
4. 学生职位反馈闭环。
5. 证据化匹配、定制简历与投递快照集成。

并行开发只在明确共享点串行：Alembic migration、`backend/app/db/models.py`、API 总路由和 `frontend/src/App.vue` 等聚合入口按预定顺序合并，其余领域模块、服务、功能路由、前端 feature 和测试保持独立。

### 2.2 非目标

本阶段不实现：

- 任意网页通用抓取、公众号图片 OCR 或二维码解析。
- 大疆 Moka 或其他真实招聘网站适配。
- 自动处理登录、短信、验证码或人机验证。
- GUI Agent 点击最终提交按钮。
- 云端保存身份证号、家庭成员、紧急联系人等本地敏感字段明文。
- 批量投递、跨用户公开评论、职位评分或社交功能。
- 使用模型自动确认档案事实、自动合并职位或自动发布 verified 职位。

## 3. 总体架构与执行顺序

### 3.1 Wave 1 并行工作流

Wave 1 同时启动 A、B、C、D 四份计划：

| 工作流 | 主要产出 | 可独立开发部分 | 串行集成点 |
| --- | --- | --- | --- |
| A 档案与简历 | 档案、资产、证据、确认版本 | 解析器、服务、档案 API、档案 UI、对象存储测试 | migration `0005`、共享入口 |
| B 手动 JD | 私有提交、重复候选、多来源关系、管理员提升 | 规范化、候选算法、领域服务、功能 API/UI | migration `0006` 等待 `0005` |
| C Executor 骨架 | Windows 进程、模拟页面、安全门、本地检查点 | 本地程序、模拟站点、协议 fixture、状态机联调 | API 总路由；原则上无 migration |
| D 职位反馈 | 学生反馈、管理员处置、不可变事件 | 领域服务、功能 API/UI、聚合统计 | migration `0007` 等待 `0006` |

B 和 D 可以先完成除 migration 集成和共享入口接线外的开发。Alembic 保持单 head，不使用多分支 migration 和 merge revision。

### 3.2 Wave 2 集成工作流

工作流 E 在以下条件满足后启动：

- MySQL 中至少存在一个来源链完整的 verified `JobPosting`。
- 至少存在一个用户确认的 `ConfirmedProfileVersion`。
- Wave 1 migration 已到 `20260717_0007`。

E 将 LangGraph 的 Web 分析路径从演示职位切换为 MySQL 权威职位快照，实现证据化 `MatchReport`、岗位定制 `ResumeDraft`、用户批准的 `ApprovedResumeVersion` 和不可变 `ApplicationSnapshot`。E 不依赖真实招聘网站；Executor 可以先消费模拟任务和非敏感快照契约。

## 4. 工作流职责边界

### 4.1 A：档案与简历生命周期

工作流 A 支持 PDF、DOCX 和文本导入。原始文件在写入 MinIO/S3 前由 Backend 使用现有 AES-256-GCM 对象存储适配器加密；MySQL 只保存对象引用、文件元数据、解析批次、字段证据和确认版本。

解析器产生字段候选、原文证据、置信度和确认状态。用户可以逐项确认、修正或忽略；重复上传生成差异，不覆盖历史版本。只有用户确认后才能创建不可变 `ConfirmedProfileVersion`。纯图片或无法可靠解析的文件进入 `needs_manual_entry`，保留原资产并引导用户手工补充，不进入 OCR。

本工作流定义本地敏感字段引用协议，但不保存或传输本地敏感字段明文。岗位定制简历、批准附件和投递快照属于工作流 E。

### 4.2 B：手动 JD 导入、来源合并与统一去重

学生可以粘贴 JD 文本或职位链接。提交默认只对本人可见，保留提交者、原始证据、处理状态和版本。系统生成可解释的重复候选，但不得自动合并。

管理员可以把提交关联到已有职位，或创建新的 `pending_completion` 职位。任何新公共职位仍须经过现有补全和核验流程，不能由手动提交直接进入 `verified`。同一职位可以通过 `JobSourceLink` 关联腾讯记录和用户提交等多个来源，公开 DTO 不暴露提交者身份。

本工作流不抓取任意用户 URL。链接只做安全格式校验；后续自动补全需要独立设计。

### 4.3 C：Windows Executor 骨架与模拟安全门

工作流 C 建立独立 Windows 本地进程，复用现有 Device、ApplicationTask、ApplicationEvent、设备凭据和 task lease。Executor 通过出站连接获取已分配任务，使用 `task:progress` 报告进度，使用 `task:result` 报告用户提交后的观察结果。

本工作流使用项目内模拟招聘页面验证导航、字段填写、回读、断线恢复、本地检查点和人工接管。单页底部动作、末页动作、组合提交动作和无法分类的按钮必须停止并进入 `READY_FOR_REVIEW`。不得签发或接受 `task:submit`。

Wave 1 中 Executor 使用版本化协议 fixture 和模拟字段数据，不要求 `ApplicationSnapshot` 已经实现。工作流 E 完成后，再把 fixture 替换为真实的非敏感快照读取。

### 4.4 D：学生职位反馈闭环

学生可以对 verified 职位报告以下稳定类别：

- `closed`
- `application_channel_unavailable`
- `content_changed`
- `incorrect_information`

同一用户对同一职位和类别的反馈使用幂等更新或撤回。管理员可以受理、解决或驳回，并查看聚合数量和脱敏明细。

反馈不能直接修改 `JobPosting.status`。管理员若确认职位失效，必须另行调用现有 JobReviewService，使状态变化继续使用 `review_version`、行锁和同事务 `JobVerification`。

### 4.5 E：证据化匹配、定制简历与投递快照

工作流 E 的输入只能是 verified `JobPosting` 和 `ConfirmedProfileVersion`。匹配结论必须引用确认版本字段路径或对应证据；缺失信息标记为未知，不能自动判定为不满足。

定制简历只能基于已确认事实调整排序、措辞、摘要和内容取舍。每处修改必须引用来源事实并以差异形式展示。用户批准后生成不可变 `ApprovedResumeVersion`、加密附件和 `ApplicationSnapshot`。

既有快照不能因职位、档案或模板变化而被静默修改。创建新任务必须选择当前仍可用的批准版本；历史快照继续用于审计和恢复。

## 5. 共享数据契约

### 5.1 通用约定

- 所有新增业务实体使用 36 字符 UUID。
- 所有时间保存为 UTC，并在 API 边界规范化。
- 可变聚合使用整数版本和 `expected_version` 乐观并发控制。
- 证据、确认版本、快照和审计事件只追加，不原地覆盖。
- 所有用户数据查询从认证主体确定 `user_id`，跨用户资源统一返回 404。
- 公开 DTO 使用显式字段白名单。
- 日志只记录实体 ID、稳定错误码和脱敏计数。

### 5.2 A 的实体

- `Profile`：每个用户一个主档案聚合。
- `ResumeAsset`：对象引用、原始文件名、内容类型、明文大小、加密版本和状态。
- `ResumeImport`：一次解析批次、解析器版本、状态和稳定错误码。
- `ProfileFieldEvidence`：字段路径、候选 JSON 值、证据摘录、来源资产、置信度和确认状态。
- `ConfirmedProfileVersion`：版本号、不可变确认事实 JSON、证据引用和创建时间。
- 本地敏感字段引用：只包含字段类别、不可逆引用和更新时间。

前端资产 DTO 不返回对象 key；下载由鉴权 API 读取并解密。

### 5.3 B 的实体

- `UserJobSubmission`：用户、输入类型、原始链接或 JD、状态、版本和提升结果。
- `JobDuplicateCandidate`：提交、候选职位、匹配理由、分数组成和算法版本。
- `JobSourceLink`：职位、来源类型、来源记录引用、规范链接和创建时间。

公开职位仍以 `JobPosting` 为权威规范记录。用户提交只作为私有输入和来源证据，不成为第二套公开职位真相。

### 5.4 D 的实体

- `JobFeedback`：用户、职位、类别、当前状态、可选说明和版本。
- `JobFeedbackEvent`：动作、actor、from/to 状态、脱敏快照、客户端幂等键和时间。

`user_id + job_id + category` 使用唯一约束；`actor_user_id + idempotency_key` 使用事件唯一约束，防止学生或管理员的网络重试产生重复事件。

### 5.5 E 的实体

- `MatchReport`：分析会话、职位快照、确认档案版本、结构化匹配结果、证据引用、模型版本和状态。
- `ResumeDraft`：基线确认版本、目标职位、结构化差异和状态。
- `ApprovedResumeVersion`：批准事实、批准差异、附件引用和批准时间。
- `ApplicationSnapshot`：职位规范字段、确认档案事实、批准简历版本、非敏感动态答案和本地字段需求引用的不可变快照。

## 6. API 契约

### 6.1 档案与简历

- `/api/resume-assets`：上传、列表、读取元数据和受控下载。
- `/api/resume-imports`：从 ready 资产创建可重试解析批次。
- `/api/profiles`：读取主档案、提交证据确认和创建确认版本。
- `/api/profile-versions`：读取用户自己的不可变版本。

### 6.2 手动 JD

- `/api/job-submissions`：学生创建、列表、读取、修改和提交审核。
- `/api/job-submissions/{id}/duplicate-candidates`：读取可解释候选。
- `/api/admin/job-submissions`：管理员队列、关联已有职位、创建待补全职位或拒绝。

### 6.3 Executor

- `/api/executor/tasks`：设备读取分配给自己的任务元数据。
- `/api/executor/tasks/{task_id}/progress`：校验 `task:progress` lease 和 task version。
- `/api/executor/tasks/{task_id}/result`：校验 `task:result` lease，只接受用户提交后的结果观察。

请求必须同时绑定 device token、task ID、lease scope 和已分配的 `device_id`。

### 6.4 职位反馈

- `/api/jobs/{job_id}/feedback`：学生创建、更新或撤回自己的反馈，请求必须携带 `Idempotency-Key`。
- `/api/admin/job-feedback`：管理员队列和聚合。
- `/api/admin/job-feedback/{feedback_id}/decision`：受理、解决或驳回，请求必须携带 `Idempotency-Key`，且不直接修改职位。

### 6.5 匹配与快照

- `/api/matches`：使用分析会话 ID、job ID 和 confirmed profile version ID 创建或继续报告。
- `/api/resume-drafts`：基于报告创建和读取定制草稿。
- `/api/resume-drafts/{id}/approve`：批准草稿并生成批准版本与附件。
- `/api/application-snapshots`：使用 verified 职位和批准简历版本创建快照。

`POST /api/matches` 是 Wave 2 唯一的 Web 匹配入口。计划 E 在前端迁移和契约测试同步完成后删除现有 `POST /api/analysis/run`，不保留可读取示例职位或示例简历的兼容 Web 路由。现有会话列表、激活、状态和历史 API 继续保留，并由 `MatchReport` 关联分析会话。CLI 示例可以继续使用本地数据，但必须与生产 Web 路径隔离。

verified 但 `gui_eligible=false` 的职位可以生成匹配报告、批准简历和人工投递用快照；只有快照中的职位仍为 `gui_eligible=true` 时，服务端才能创建或派发 Executor `ApplicationTask`。

## 7. 错误处理与恢复

### 7.1 稳定错误边界

所有新 API 错误包含稳定 `code`，并使用以下 HTTP 语义：

- 404：资源不存在或不属于当前主体。
- 409：版本陈旧、状态冲突、重复处理中或 lease 与任务状态冲突。
- 422：字段或领域规则不合法。
- 503：MySQL、Redis、对象存储或其他所需依赖不可用。

响应和日志不得包含对象 key、完整简历、原始 JD、用户身份关联、token、Cookie、验证码、模型原始提示或外部错误正文。

### 7.2 对象存储与解析恢复

上传和解析拆成两个步骤。`ResumeAsset` 先进入 `pending_upload`，加密对象写入成功后进入 `ready`；对象写入失败时记录稳定错误码并允许重试。数据库确认失败但对象已存在时，通过对象 metadata/head 对账恢复。

解析失败保留原资产和旧确认版本。重新解析创建新 `ResumeImport`，不得覆盖旧证据批次。

### 7.3 手动 JD 恢复

链接只接受 `http/https`，并执行长度、主机和格式校验；拒绝 URL userinfo、localhost、环回、链路本地、私网和保留地址。JD 文本有明确大小上限。重复识别失败不影响用户查看或修改私有提交。

管理员提升使用提交行锁和版本校验。关联或创建职位、写入来源关系和记录审计必须在同一 MySQL 事务中完成。

### 7.4 Executor 恢复

- lease 失效或 401：停止当前动作并重新取得许可。
- 409：读取 MySQL 当前任务状态和版本后恢复。
- 网络超时：可重试观察、读取和字段回读，不重试可能产生副作用的按钮。
- 本地检查点只保存页面指纹、步骤和非敏感字段状态。
- 回读不一致、页面拓扑变化或动作含义不明确时进入人工审查。

### 7.5 匹配与快照恢复

模型输出必须通过结构化 schema 和证据引用校验。无法解析、引用不存在或包含未确认事实时，报告进入失败状态，不生成草稿。

批准版本和快照只能由同一组不可变输入创建。附件生成或对象写入失败时不得产生可用快照。Redis checkpoint 丢失不能改变 MySQL 中已保存的报告、批准版本或快照。

## 8. 隐私与安全不变量

- 普通档案和简历对象只通过 Backend 加密存储。
- 身份证号、家庭成员、紧急联系人等本地敏感明文不得进入云端数据库、对象存储、日志或模型输入。
- 用户提交职位默认私有，公开职位 DTO 不返回提交者身份。
- JobFeedback 不公开用户身份或自由文本。
- Executor 不回传密码、Cookie、验证码、完整表单值或完整简历。
- 不存在 `task:submit` scope，任何执行器端点都不得模拟该权限。
- 模型不能直接改变档案确认状态、职位核验状态或 ApplicationTask 权威状态。

## 9. Migration 与共享文件策略

迁移顺序固定为：

1. `20260717_0005`：档案与简历生命周期。
2. `20260717_0006`：手动 JD 与来源关系。
3. `20260717_0007`：职位反馈。
4. 工作流 C：不新增 migration。
5. `20260718_0008`：匹配、批准版本和投递快照。

每份计划必须把 migration 集成任务与领域开发任务分开。B 和 D 可以先完成领域代码、fixture 和前端组件，但只有前一 revision 合并后才创建或最终调整自己的 migration。

以下文件属于共享集成点，不允许多个并行工作流同时无协调修改：

- `backend/app/db/models.py`
- `backend/app/db/__init__.py`
- `backend/app/api/router.py`
- `frontend/src/App.vue`
- `frontend/src/api.ts`
- `alembic/env.py`
- `docs/runbooks/platform-foundation.md`

功能代码应优先放入独立的 repository、service、route、schema 和 frontend feature 文件。共享入口只做导入、挂载和导航，不内联领域逻辑。

## 10. 测试与验收

### 10.1 A 的门禁

- PDF、DOCX 和文本产生待确认证据。
- 图片型或损坏文件保留资产并返回可恢复状态。
- 重复上传不覆盖已确认版本。
- 并发确认只有一个版本成功。
- MinIO 中只有 AES-GCM 密文。
- API、日志和数据库不暴露对象 key 或完整简历。
- 跨用户资产、证据和版本读取返回 404。

### 10.2 B 的门禁

- 用户提交默认仅本人可见。
- 重复候选包含稳定、可解释理由且不自动合并。
- 管理员提升只会关联已有职位或创建 `pending_completion`。
- 提升后仍经过现有补全和核验流程。
- 腾讯重同步不删除手动来源关系，也不覆盖人工规范字段。
- 学生和公开 DTO 不包含提交者身份。

### 10.3 C 的门禁

模拟页面覆盖单页、多页非末页、多页末页、歧义按钮、缺失字段、低置信度字段、登录等待、人工接管、断线、lease 过期、进程重启和陈旧版本。

必须满足：

- 最终提交按钮自动点击次数为 0。
- 歧义按钮点击次数为 0。
- 恢复后字段重复填写次数为 0。
- 恢复后中间副作用重复执行次数为 0。
- 回读不一致时停止推进并进入人工审查。

### 10.4 D 的门禁

- 同一幂等键不产生重复事件。
- 普通用户只能操作自己的反馈。
- 反馈不能直接修改 JobPosting 状态。
- 管理员反馈处置与 JobVerification 保持分离。
- 限流、文本上限、日志脱敏和聚合统计正确。

### 10.5 E 的门禁

- FastAPI 分析路径不调用 `load_jobs()`，不读取 `data/jobs.json`。
- 非 verified 职位和未确认档案版本不能进入匹配。
- 所有匹配结论、缺口和简历修改都能解析到真实证据。
- 模型虚构、无效引用和未确认事实被拒绝。
- 修改档案、职位或草稿不会改变既有 ApplicationSnapshot。
- Redis checkpoint 丢失不改变 MySQL 权威产物。
- Executor 获取的快照不包含本地敏感字段明文。

### 10.6 全局发布门禁

- 完整 Python 回归和 Ruff。
- 前端 Vitest、`vue-tsc` 和 production build。
- MySQL `0004 → 0005 → 0006 → 0007 → 0008` 升级和降级验证。
- Redis lease、MinIO 加密对象和 Nginx 代理链 opt-in 测试。
- Docker Compose 重建、migration、live/ready 和核心页面冒烟测试。
- 使用真实腾讯来源和至少一个人工核验职位完成最终验收。

缺少有效腾讯 token 时，只能记录为外部验证未完成，不能宣称完整发布门禁通过。当前 `npm audit` 的 high severity 依赖项属于发布前必须关闭或形成书面风险接受的既有缺口，不计入任何功能完成声明。

## 11. 完成定义

Wave 1 在以下条件同时满足时完成：

1. A、B、C、D 分别通过自身测试与安全门禁。
2. Alembic 单 head 到达 `20260717_0007`。
3. Compose 环境可以上传并确认至少一个档案版本。
4. 手动职位始终保持私有，直到管理员明确提升。
5. 模拟 Executor 在全部安全样本中最终提交自动点击为零。
6. 学生反馈无法绕过现有 JobReviewService 改变职位状态。

工作流 E 在以下纵向闭环通过时完成：

1. 一个来源明确的 verified 职位。
2. 一个 `ConfirmedProfileVersion`。
3. 一个所有结论均可追溯证据的 `MatchReport`。
4. 一个用户批准的 `ApprovedResumeVersion`。
5. 一个不可变且不包含本地敏感明文的 `ApplicationSnapshot`。
6. Web 分析路径完全脱离演示职位文件。

## 12. 实施计划输出

本设计批准后生成五份实施计划：

1. `2026-07-16-talent-profile-resume-lifecycle.md`
2. `2026-07-16-manual-job-import-deduplication.md`
3. `2026-07-16-windows-executor-simulation-safety.md`
4. `2026-07-16-job-feedback-loop.md`
5. `2026-07-16-evidence-matching-resume-snapshot-integration.md`

前四份属于 Wave 1 并行计划；第五份属于 Wave 2 集成计划。每份计划的 Task 0 都必须引用本设计的共享契约检查点，并明确自己的独占文件、共享入口、migration 前置条件和验收命令。
