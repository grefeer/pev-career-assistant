# 真实职位同步首个纵向闭环设计

## 1. 文档信息

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-07-15 |
| 状态 | 已确认，等待书面规格复核 |
| 所属工作包 | 工作包 2：真实职位同步与核验 |
| 实施范围 | 腾讯智能表 → 原始记录留存 → 归一化职位 → 查询 API |
| 前置版本 | 平台基础与权威数据 `706434fccc90a4d3a07538311cb18a42a224994a` |

## 2. 背景与目标

平台基础阶段已经建立 MySQL 权威业务数据、Redis 临时状态、认证授权、审计、迁移和完整质量门。职位数据目前仍来自 `data/jobs.json` 演示文件，尚未连接真实招聘来源。

本阶段交付工作包 2 的第一个纵向闭环：后端从两个指定腾讯智能表子表只读同步记录，在 MySQL 中保存不可变原始快照，对字段充分的记录生成待补全职位，并向已认证用户提供分页查询 API。该闭环用于尽早验证真实数据结构、分页、幂等、权限和数据权威边界。

目标来源：

| 来源键 | 文档 ID | 子表 ID | 子表名称 | 当前数据特征 |
| --- | --- | --- | --- | --- |
| `tencent-27-referrals` | `DZkdPVGtGb1ZvaG5R` | `t00i2h` | 27届内推信息【重要】 | 公司和招聘推文线索为主，通常缺少具体岗位 |
| `tencent-intern-referrals` | `DY3pHYkNvb0ZRSHdi` | `BB08J2` | 实习内推汇总 | 包含公司、岗位、地点、截止日期和直接投递链接 |

同步只读取腾讯文档，不写入或修改源表。

## 3. 范围

### 3.1 本阶段包含

- 后端内置腾讯智能表 MCP HTTP 适配器。
- 两个预置来源及各自独立、带版本的字段映射。
- 来源字段定义读取和每页最多 100 条的完整分页读取。
- MySQL 中的来源、同步运行、不可变原始快照和归一化职位模型。
- 内容摘要去重、分页提交、部分成功和安全重试。
- 管理员手动同步单个来源的 API。
- 已认证用户查询待补全职位的列表和详情 API。
- 安全日志、脱敏审计、运行手册和自动化测试。

### 3.2 本阶段不包含

- 定时同步、后台任务队列和管理员前端页面。
- 管理员在线创建或修改来源及字段映射。
- 招聘官网访问、链接追踪、JD 抓取或职位补全。
- 跨来源去重、职位合并、人工审核和职位下线。
- 用户手动添加 JD 或职位链接。
- 将待补全职位用于 GUI Agent 或投递任务。
- 修改现有分析流程、GUI Agent 状态机或前端页面。

## 4. 核心不变量

1. MySQL 是同步运行、原始快照和归一化职位的唯一权威来源。
2. 腾讯文档是只读外部来源；同步不得对其执行任何写操作。
3. 相同来源记录的相同内容重复同步不得创建重复快照。
4. 外部记录内容变化必须创建新的不可变快照，不能覆盖历史原文。
5. 只有同时具备公司名称、非空岗位名称和有效 HTTP(S) 投递链接的记录才能生成职位。
6. 缺少岗位信息的记录只保存为原始线索，不得编造岗位名称。
7. 新职位只能进入 `PENDING_COMPLETION`，本阶段不得标记为已核验。
8. 腾讯表临时缺行或部分读取失败不得删除、失效或覆盖既有职位。
9. 腾讯令牌、完整上游响应和原始记录载荷不得进入 API 响应、日志或审计事件。
10. 普通用户不能触发同步，匿名用户不能查询职位。

## 5. 架构与组件

### 5.1 `TencentSmartsheetGateway`

Gateway 是唯一了解腾讯 MCP 协议的组件，职责限定为：

- 使用代码中固定的官方 MCP HTTPS 地址建立客户端。
- 从 `TENCENT_DOCS_TOKEN` 读取授权令牌。
- 调用 `smartsheet.list_fields` 获取字段定义。
- 调用 `smartsheet.list_records` 按 offset/limit 分页读取记录。
- 将 MCP/JSON-RPC 响应转换为内部 DTO。
- 将超时、限流、鉴权和协议错误转换为稳定异常类型。

Gateway 不负责数据库事务、字段映射或业务状态。生产实现使用进程内 HTTP/MCP 客户端，禁止通过 `mcporter` 子进程执行同步。测试通过同一协议注入假 Gateway。

MCP 地址不接受请求参数或数据库覆盖，防止把同步端点变成任意 URL 请求器。令牌延迟读取；未配置令牌只影响同步，不影响应用启动、登录、健康检查或已有职位查询。

### 5.2 `SourceMapper`

每个目标子表有独立 Mapper。Mapper 接收字段定义和一条规范化腾讯记录，输出以下两种结果之一：

- `NormalizedJobCandidate`：包含公司、岗位、地点、招聘类型、行业、投递链接、内推码、截止日期原文和来源更新时间。
- `SkippedRecord`：包含稳定原因码，例如 `missing_company`、`missing_title`、`missing_apply_url` 或 `invalid_apply_url`。

Mapper 必须保留显式版本号。职位保存所用 Mapper 版本，以便后续映射变更时重新处理已有原始快照。第一张来源允许其绝大多数记录因 `missing_title` 被跳过；跳过属于预期数据结果，不是同步失败。

### 5.3 `JobSyncService`

服务负责：

- 查找预置来源并取得来源级同步租约。
- 创建和完成 `JobSyncRun`。
- 校验字段结构，驱动分页读取。
- 规范化原始 JSON、计算内容摘要并调用仓储。
- 调用来源 Mapper 并幂等写入职位。
- 按页提交事务、刷新租约并累计运行计数。
- 记录不含敏感内容的审计事件。

服务不依赖 FastAPI 请求对象。Gateway、时钟和仓储边界均可注入，以便确定性测试。

### 5.4 仓储与 API

`backend/app/repositories/jobs.py` 封装 JobSource、JobSyncRun、RawJobRecord 和 JobPosting 的 SQLAlchemy 操作。`backend/app/api/routes/jobs.py` 只处理认证、输入验证、服务调用、异常到 HTTP 状态的映射和 Pydantic 响应序列化。

## 6. 数据模型

### 6.1 枚举

`JobSourceProvider`：

- `TENCENT_SMARTSHEET`

`JobSyncRunStatus`：

- `RUNNING`
- `SUCCEEDED`
- `PARTIAL`
- `FAILED`

`JobPostingStatus`：

- `PENDING_COMPLETION`

后续阶段可以增加待人工审核、已核验、已失效和已拒绝状态，但本迁移不提前实现这些行为。

### 6.2 `JobSource`

| 字段 | 约束与含义 |
| --- | --- |
| `id` | UUID 主键 |
| `source_key` | 稳定唯一键，不使用显示名称作为身份 |
| `provider` | `TENCENT_SMARTSHEET` |
| `name` | 来源显示名称 |
| `file_id` | 腾讯智能表文档 ID |
| `sheet_id` | 腾讯子表 ID |
| `mapper_version` | 当前映射版本 |
| `enabled` | 是否允许同步 |
| `last_successful_sync_at` | 最近完整成功时间，可空 |
| `active_sync_run_id` | 当前租约对应运行 ID，可空 |
| `sync_lease_expires_at` | 租约过期时间，可空 |
| `created_at/updated_at` | 平台时间 |

`source_key` 唯一；`provider + file_id + sheet_id` 也必须唯一。管理员同步入口在查找来源前调用 `ensure_builtin_job_sources()`，幂等写入这两个预置来源；应用启动和数据库迁移都不写入来源行。初始化函数只写固定来源元数据，不写访问令牌或动态业务数据。

### 6.3 `JobSyncRun`

| 字段 | 约束与含义 |
| --- | --- |
| `id` | UUID 主键 |
| `source_id` | JobSource 外键，限制删除 |
| `status` | 同步运行状态 |
| `pages_read` | 已成功处理页数 |
| `records_read` | 已读取记录数 |
| `raw_snapshots_created` | 新建原始快照数 |
| `postings_created` | 新建职位数 |
| `postings_updated` | 更新职位数 |
| `records_skipped_incomplete` | 因归一化门槛跳过数 |
| `error_code` | 稳定脱敏错误码，可空 |
| `started_at/finished_at` | 平台时间，结束时间可空 |

运行记录不保存上游错误正文、完整 URL、令牌或原始载荷。

### 6.4 `RawJobRecord`

| 字段 | 约束与含义 |
| --- | --- |
| `id` | UUID 主键 |
| `source_id` | JobSource 外键，限制删除 |
| `external_record_id` | 腾讯 record_id |
| `payload_hash` | 规范化 JSON 的 SHA-256 十六进制摘要 |
| `raw_fields` | 完整字段值 JSON |
| `source_updated_at` | 来源更新时间，可空且不作为游标 |
| `observed_at` | 平台首次观察时间 |

`source_id + external_record_id + payload_hash` 唯一。记录插入后不更新；来源内容变化创建新的行。JSON 规范化使用 UTF-8、排序键、稳定分隔符，并保留字段值类型，避免同一语义因序列化顺序变化产生新快照。

### 6.5 `JobPosting`

| 字段 | 约束与含义 |
| --- | --- |
| `id` | UUID 主键 |
| `source_id` | JobSource 外键，限制删除 |
| `external_record_id` | 对应腾讯 record_id |
| `raw_record_id` | 当前归一化依据的 RawJobRecord |
| `status` | 固定为 `PENDING_COMPLETION` |
| `company_name` | 非空公司名称 |
| `title` | 非空来源岗位原文，本阶段不自动拆分多岗位文本 |
| `locations` | 规范化字符串数组 |
| `recruitment_types` | 规范化字符串数组 |
| `industries` | 规范化字符串数组 |
| `apply_url` | 校验后的 HTTP(S) URL |
| `referral_code` | 内推码，可空 |
| `deadline_text` | 截止日期来源原文，可空 |
| `source_updated_at` | 来源更新时间，可空 |
| `mapper_version` | 生成该职位的映射版本 |
| `created_at/updated_at` | 平台时间 |

`source_id + external_record_id` 唯一。同一来源记录内容变化且仍满足门槛时更新当前职位并指向新原始快照；历史原始快照继续保留。本阶段不把一个包含多个岗位名称的单元格自动拆成多个职位，避免错误切分。

## 7. 腾讯字段映射

### 7.1 `tencent-27-referrals`

| 统一字段 | 腾讯字段 |
| --- | --- |
| 公司 | `企业名称` |
| 岗位 | 无稳定字段，因此不生成职位 |
| 地点 | `工作地点` |
| 招聘类型 | `招聘类型` |
| 行业 | `行业类型` |
| 投递链接 | `内推链接` |
| 内推码 | `内推码(区分大小写)` |
| 来源更新时间 | `更新时间` |

该来源当前主要生成原始线索。即使存在公司和链接，也不能用公司名、分组或整段文案伪造岗位名。

### 7.2 `tencent-intern-referrals`

| 统一字段 | 腾讯字段 |
| --- | --- |
| 公司 | `公司名称` |
| 岗位 | `招聘岗位` |
| 地点 | `工作地点` |
| 招聘类型 | `招聘类型` |
| 行业 | `多选` |
| 投递链接 | `投递链接` |
| 内推码 | `内推码` |
| 截止日期 | `截止日期` |
| 来源更新时间 | `更新时间` |

标题行、说明行和缺少投递链接的行保存为原始快照但不生成职位。图片字段继续保存在原始 JSON 中，本阶段不下载、不 OCR、不写入对象存储。

## 8. 同步数据流

1. 管理员调用单来源同步 API。
2. 服务在短事务中锁定 JobSource，确认 enabled，检查租约，并创建 `RUNNING` 的 JobSyncRun。
3. 服务提交事务后调用 Gateway 获取字段定义。
4. 字段定义必须满足当前来源 Mapper 的结构契约。关键字段被删除或类型变更时，以 `source_schema_changed` 终止，不进入分页写入。
5. Gateway 从 offset 0、limit 100 开始分页读取。
6. 每条记录转换为稳定内部 JSON并计算 payload hash。
7. 每页在独立数据库事务中：
   - 插入尚不存在的 RawJobRecord。
   - 对本页所有记录执行 Mapper。
   - 对满足门槛的候选按来源和 external_record_id 创建或更新 JobPosting。
   - 累计运行计数并刷新同步租约。
8. 服务提交本页事务后读取下一页。
9. 全部页面成功后，在短事务中将运行标记为 `SUCCEEDED`、清理租约并更新 `last_successful_sync_at`。
10. 失败时根据已提交页数标记 `FAILED` 或 `PARTIAL`，保存稳定错误码并清理租约。

同步租约不在外部网络调用期间持有数据库行锁。租约初始有效期为 10 分钟，并在每页成功提交时刷新为“当前时间后 10 分钟”；单次 Gateway 请求超时为 15 秒。进程崩溃后可以由后续请求接管。租约只负责减少重复工作，数据正确性仍由唯一约束和幂等 upsert 保证。

分页必须验证：

- `next` 严格大于当前 offset。
- `records` 数量不超过请求 limit。
- 已读取数量不超过上游声明 total。
- `has_more=false` 时停止。
- 单次运行最多读取 1,000 页或 100,000 条记录；超过任一上限时返回 `tencent_protocol_error`。

重新同步总是从第一页开始。同步不使用来源更新时间作为增量游标，因此可以恢复任何部分失败；已有相同快照会被唯一约束跳过。

## 9. API 设计

### 9.1 手动同步

`POST /api/admin/job-sources/{source_key}/sync`

权限：管理员 JWT。

成功响应：

```json
{
  "run_id": "uuid",
  "source_key": "tencent-intern-referrals",
  "status": "succeeded",
  "pages_read": 2,
  "records_read": 120,
  "raw_snapshots_created": 120,
  "postings_created": 118,
  "postings_updated": 0,
  "records_skipped_incomplete": 2,
  "started_at": "2026-07-15T10:00:00Z",
  "finished_at": "2026-07-15T10:00:03Z"
}
```

计数仅为示例，不能在测试中固定为当前真实来源数量。

状态码：

| 状态 | 条件 |
| --- | --- |
| 200 | 同步完成，包括结果为 SUCCEEDED |
| 401 | 未认证 |
| 403 | 非管理员 |
| 404 | 来源键不存在 |
| 409 | 来源已有未过期同步租约 |
| 502 | 上游协议错误或来源字段结构改变 |
| 503 | 令牌缺失、鉴权失败、限流耗尽或腾讯服务不可用 |
| 504 | 重试后仍超时 |

当运行结果为 PARTIAL 或 FAILED 时，API 返回与上游异常类别对应的 5xx，同时响应 detail 只包含稳定错误码和 run_id，便于查询数据库运行记录且不泄露上游正文。

### 9.2 职位列表

`GET /api/jobs?limit=20&offset=0&source_key=...&company=...&recruitment_type=...`

权限：已认证用户。

- `limit` 默认 20，范围 1–100。
- `offset` 默认 0，必须非负。
- `source_key` 精确匹配。
- `company` 对标准化公司名称执行包含匹配，并转义 SQL 通配符。
- `recruitment_type` 匹配数组中的完整标签，不能用子串误匹配。
- 默认只返回 `PENDING_COMPLETION`，按 `updated_at DESC, id DESC` 稳定排序。

列表项包含职位 ID、公司、岗位、地点、招聘类型、行业、投递链接、截止日期原文、状态、来源名称和平台更新时间。响应必须明确暴露 `pending_completion` 状态。

### 9.3 职位详情

`GET /api/jobs/{job_id}`

权限：已认证用户。

详情增加内推码、来源更新时间和 Mapper 版本。不存在时返回 404。响应不包含 raw_fields、payload_hash、腾讯 record_id、令牌或 MCP trace。

## 10. URL 与数据校验

投递链接满足以下条件才视为有效：

- scheme 为 `http` 或 `https`。
- 存在合法主机名。
- 不包含用户名或密码。
- 不包含控制字符。
- 总长度不超过 4,096 个 Unicode 码点。

本阶段不主动访问投递链接，也不判断职位真实性。查询接口返回来源原文中的有效链接，后续补全阶段再执行重定向追踪、公司归属验证和最终入口核验。

空白字符串统一转为缺失值。文本列表去除空项并保持首次出现顺序。来源时间按毫秒 Unix 时间戳解析为 UTC；无效时间保留在原始 JSON 中，但归一化字段置空。

## 11. 错误处理与重试

稳定错误码至少包括：

- `tencent_token_missing`
- `tencent_auth_failed`
- `tencent_rate_limited`
- `tencent_timeout`
- `tencent_unavailable`
- `tencent_protocol_error`
- `source_schema_changed`
- `sync_conflict`
- `database_write_failed`

429、临时 5xx、连接失败和超时最多重试三次，使用有上限的指数退避。令牌缺失、鉴权失败、字段结构改变、非法分页和记录业务缺失不重试。

单条记录无法归一化时保存原始快照、增加跳过计数并继续。整页响应无法解析、分页不前进或来源关键字段定义改变时终止同步。第一页事务提交前失败标记 FAILED；已经提交至少一页后失败标记 PARTIAL。

任何异常路径都必须尽力完成运行状态并清理租约。若进程被强制终止，租约到期后允许下一次同步重试；旧 RUNNING 记录在接管时标记为 FAILED，错误码为 `sync_lease_expired`。

## 12. 安全与隐私

- `TENCENT_DOCS_TOKEN` 只从环境变量读取，不写入 `.env`、数据库、命令行参数、日志或测试 fixture。
- Gateway 日志只记录稳定操作名、来源键、页偏移、HTTP 状态类别和关联 ID。
- API 和审计事件不得包含上游原始响应、MCP trace、完整原始载荷或令牌。
- 同步审计只记录 actor_user_id、run_id、source_key、结果、计数和错误码。
- 原始职位字段属于平台业务数据并保存在 MySQL；其中的图片 URL 本阶段仅作为原始 JSON 数据保存，不主动下载。
- 同步端点使用 `require_admin`；职位查询使用 `get_current_user`。
- file_id、sheet_id、MCP 地址和映射版本不接受请求体覆盖。
- 腾讯接口 readiness 不加入现有 `/api/health/ready`，避免外部内容源波动使核心平台整体不就绪。

## 13. 测试策略

### 13.1 单元测试

- MCP 字段和记录 DTO 解码。
- text、URL、select、dateTime、image 和空值的稳定 JSON 转换。
- 两个来源 Mapper 的正常、缺失和非法 URL 分支。
- JSON 规范化和 payload hash 稳定性。
- 分页 next/has_more/total 防御性校验。
- 重试分类和稳定错误码。
- 租约取得、刷新、冲突、过期接管和清理。

### 13.2 服务测试

- 多页完整成功。
- 第一页前失败得到 FAILED。
- 后续页面失败得到 PARTIAL 且保留已提交页。
- 相同数据重跑不新增 RawJobRecord。
- 单条内容变化只创建一个新快照并更新对应 JobPosting。
- 缺岗位的第一来源记录只保存原始快照。
- 第二来源符合门槛的记录生成 PENDING_COMPLETION 职位。
- 腾讯表缺行不删除或失效既有职位。

### 13.3 API 契约与安全测试

- 管理员可同步，学生得到 403，匿名得到 401。
- 未知来源 404，并发租约 409。
- 查询认证、分页、稳定排序和筛选。
- 详情 404 和响应字段白名单。
- 日志、审计和 HTTP 错误不包含测试令牌、原始载荷或完整上游响应。
- 缺少 `TENCENT_DOCS_TOKEN` 不影响健康检查和职位查询。

### 13.4 集成测试

- Alembic 从当前 head 升级到新版本并 downgrade/upgrade 往返。
- 真实 MySQL 上验证 JSON、唯一约束、并发租约和招聘类型完整标签筛选。
- 设置专用 `TEST_TENCENT_DOCS_TOKEN` 时，对两个真实目标子表执行只读全分页同步。
- 真实来源测试断言本次读取数与上游本次声明 total 一致，不固定断言历史观察到的 731 和 120。
- 真实来源测试不得调用任何腾讯写工具。

提交的测试 fixture 只保留最小、脱敏的字段结构和代表性记录，不保存令牌或完整真实数据集。

## 14. 运行手册更新

`docs/runbooks/platform-foundation.md` 增加：

- `TENCENT_DOCS_TOKEN` 和测试专用 `TEST_TENCENT_DOCS_TOKEN` 的用途与安全设置方式。
- 两个预置来源键和管理员手动同步示例。
- 同步结果、PARTIAL、FAILED、409 和租约过期的处理步骤。
- 腾讯同步只读、不属于 readiness 的边界。
- 禁止把令牌写入仓库、命令行、日志或带参数的 URL。

## 15. 完成定义

只有同时满足以下条件，本纵向闭环才算完成：

1. 新迁移可从现有 head 升级，并能 downgrade/upgrade 往返。
2. 两个预置来源都能通过管理员 API 完成只读同步。
3. 所有读取记录都有不可变、幂等的原始快照。
4. 缺岗位的记录不会生成虚假职位。
5. 第二来源中满足门槛的记录生成 PENDING_COMPLETION 职位并可查询。
6. 相同来源重复同步不会创建重复快照或职位。
7. 部分失败保留已提交页面，下一次全量重跑可安全恢复。
8. 来源缺行不会删除、失效或覆盖既有职位。
9. 权限、日志脱敏和固定 MCP 地址安全边界通过测试。
10. Ruff、全部 Python 测试、真实 MySQL 集成测试和前端生产构建通过。
11. 运行手册覆盖令牌设置、同步、故障处理和只读边界。

## 16. 后续工作

完成本闭环后，工作包 2 的下一规格再覆盖：招聘链接分类、官网/JD 补全、具体职位拆分、跨来源去重、图片公众号人工审核、管理员职位后台、职位核验和失效策略。本阶段形成的 RawJobRecord 历史、Mapper 版本和 PENDING_COMPLETION 状态是这些能力的输入。
