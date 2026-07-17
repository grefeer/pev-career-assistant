# Wave 2 证据化匹配、定制简历与投递快照 — 实施计划

## 0. 文档信息

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-07-17 |
| 状态 | 已确认，待执行 |
| 上游设计 | `2026-07-16-mvp-parallel-delivery-design.md`、`2026-07-16-post-foundation-parallel-workstreams-design.md` |
| 当前基线 | Wave 1 四条工作流全部完成，migration head = `20260717_0007` |
| 目标 | 完成证据化匹配 → 定制简历 → 投递快照的完整纵向闭环 |

## 1. 前置条件

实施前必须确认：

- [ ] MySQL 中至少存在一个来源链完整的 `verified` JobPosting
- [ ] 至少存在一个用户确认的 `ConfirmedProfileVersion`
- [ ] Wave 1 migration 已到 `20260717_0007` 且 upgrade/downgrade 验证通过
- [ ] 现有 Python 回归、Ruff、Vitest、vue-tsc、production build 全部通过

## 2. 实施阶段总览

```
阶段 0: 契约冻结与就绪门禁          → 不写代码，固定所有接口契约
阶段 1: Migration 0008 与领域基础    → 建表、ORM、外键闭环、校验器
阶段 2: 证据化匹配纵向切片           → LangGraph 切换 + MatchService + /api/matches
阶段 3: ResumeDraft、批准版本与附件   → 差异草稿 + 批准 + 附件生成
阶段 4: ApplicationSnapshot 与 Executor 接入 → 快照 + Task 创建校验 + Executor 真实 DTO
阶段 5: 前端路由与三页闭环           → vue-router + 匹配/草稿/快照页面
阶段 6: 切换旧入口与发布门禁         → 删除 /api/analysis/run + 全局验证
```

每个阶段形成独立、可验证的提交。

---

## 3. 阶段 0：契约冻结与就绪门禁

**目标**：在所有代码编写之前，固定跨模块接口契约。不写业务代码。

### 3.1 数据契约固定

#### 3.1.1 VerifiedJobSnapshot（匹配输入）

```python
# 从 JobPosting + JobVerification 冻结的快照，仅供匹配使用
class VerifiedJobSnapshot:
    job_id: str                          # JobPosting.id
    company_name: str
    title: str
    description_text: str
    locations: list[str]
    recruitment_types: list[str]
    industries: list[str]
    apply_url: str | None
    gui_eligible: bool                   # 快照创建时的值
    verified_at: datetime
    review_version: int
    source_links: list[JobSourceLinkRef] # 来源链引用
```

#### 3.1.2 ConfirmedProfileSnapshot（匹配输入）

```python
# 从 ConfirmedProfileVersion 提取，不包含本地敏感字段引用
class ConfirmedProfileSnapshot:
    profile_version_id: str
    profile_id: str
    version_number: int
    facts: dict[str, Any]                # facts_snapshot JSON
    evidence_refs: dict[str, list[str]]  # 字段路径 → evidence ID 列表
    confirmed_at: datetime
```

#### 3.1.3 MatchReport 结构化输出

```python
class RequirementAssessment:
    requirement: str                     # 职位要求原文
    field_path: str                      # 对应档案字段路径
    verdict: Literal["satisfied", "gap", "unknown"]
    evidence_ids: list[str]              # 引用的 ProfileFieldEvidence.id
    detail: str                          # 评估说明

class MatchReportOutput:
    id: str  # UUID，由 MatchService 在持久化前分配
    analysis_session_id: str
    job_snapshot_id: str
    profile_version_id: str
    score: int                           # 0–100
    scoring_rule_version: str
    strengths: list[RequirementAssessment]
    gaps: list[RequirementAssessment]
    unknowns: list[RequirementAssessment]
    risks: list[str]
    application_priority: Literal["high", "medium", "low", "not_recommended"]
    recommendation: str
    model_version: str
    prompt_version: str
    output_schema_version: str
    created_at: datetime
    status: Literal["completed", "failed"]

# DB 持久化时使用 MatchReportOutput 结构，id 由服务端生成 UUID
```

#### 3.1.4 ResumeDraft 差异操作格式

```python
class ResumeDiffOp:
    op: Literal["reorder", "rephrase", "summarize", "omit", "highlight"]
    section: str                         # 简历段落标识
    before: str | None                   # 原文（omit 时为原始内容）
    after: str | None                    # 修改后（omit 时为 null）
    fact_ref: str                        # 引用的 ConfirmedProfileVersion 事实路径

class ResumeDraftOutput:
    draft_id: str
    match_report_id: str
    profile_version_id: str
    target_job_id: str
    diffs: list[ResumeDiffOp]
    status: Literal["draft", "approved", "rejected"]
```

#### 3.1.5 ApplicationSnapshot 内容白名单

```python
class ApplicationSnapshotContent:
    # 允许包含的字段（白名单）
    job_snapshot: VerifiedJobSnapshot
    profile_facts: dict[str, Any]         # 非敏感字段 only
    approved_resume_version_id: str
    approved_resume_attachment_refs: list[str]  # 对象存储引用（非 key）
    dynamic_answers: dict[str, str]       # 用户填写的动态字段答案
    local_sensitive_requirements: list[str]     # 语义键列表，绝不含明文
    gui_eligible: bool
    job_status_at_snapshot: str
    job_review_version_at_snapshot: int
    created_at: datetime
    created_by: str
    schema_version: str
```

### 3.2 gui_eligible 语义（最终确定）

1. `verified` 职位**均可**创建 ApplicationSnapshot
2. `gui_eligible=false` 的快照只能人工使用
3. ApplicationSnapshot 保存创建时的 `gui_eligible`、`job_status`、`review_version`
4. 在 **ApplicationTask 创建**和**派发**两个边界重复校验：
   - snapshot 属于当前用户
   - approved version 可用
   - 快照中 `gui_eligible == true`
   - 当前职位未失效（status != expired/rejected）
   - device 与 task scope 合法

### 3.3 状态与错误码

| 实体 | 状态 | 说明 |
| --- | --- | --- |
| MatchReport | `completed`, `failed` | failed 不阻止用户查看职位 |
| ResumeDraft | `draft`, `approved`, `rejected` | 只有 draft 可批准 |
| ApprovedResumeVersion | 不可变 | 批准后不可修改 |
| ApplicationSnapshot | 不可变 | 创建后不可修改，旧快照保留审计 |

新增稳定错误码（前缀 `MATCH_`, `DRAFT_`, `SNAPSHOT_`）：

| 错误码 | HTTP | 说明 |
| --- | --- | --- |
| `MATCH_NOT_VERIFIED_JOB` | 422 | 职位非 verified |
| `MATCH_NO_CONFIRMED_PROFILE` | 422 | 无已确认档案版本 |
| `MATCH_MODEL_VALIDATION_FAILED` | 502 | 模型输出校验失败 |
| `MATCH_EVIDENCE_REF_INVALID` | 502 | 证据引用不存在或未确认 |
| `DRAFT_NOT_APPROVABLE` | 409 | 草稿状态不允许批准 |
| `DRAFT_ATTACHMENT_FAILED` | 502 | 附件生成失败 |
| `SNAPSHOT_GUI_NOT_ELIGIBLE` | 422 | 快照职位不支持 GUI 投递（仅创建 Task 时） |
| `SNAPSHOT_JOB_EXPIRED` | 422 | 职位已失效（仅创建 Task 时） |
| `SNAPSHOT_VERSION_STALE` | 409 | 批准版本已过期 |

### 3.4 事务边界

| 失败场景 | 策略 |
| --- | --- |
| 模型输出 schema 校验失败 | MatchReport 状态 = `failed`，不回滚，不生成草稿 |
| 证据引用不存在/未确认 | MatchReport 状态 = `failed`，不生成草稿 |
| 附件（对象存储）写入失败 | ApprovedResumeVersion 不创建，事务回滚 |
| Redis checkpoint 丢失 | 不影响 MySQL 中 MatchReport/ApprovedResumeVersion/Snapshot |
| 模型调用超时/异常 | MatchReport 状态 = `failed`，保留错误信息 |

### 3.5 就绪门禁

- [ ] 契约文档经评审确认（本文件 3.1–3.4 节）
- [ ] fixture 脚本可运行：MySQL 中至少存在 1 个 verified job + 1 个 confirmed profile version
- [ ] 契约测试骨架建立（至少验证 DTO 序列化/反序列化往返）

---

## 4. 阶段 1：Migration 0008 与领域基础

**目标**：新建四张表，补齐外键闭环，实现校验器。不调用模型，不实现完整 API。

### Task 1.1：创建 Alembic migration `20260717_0008`

新建 `alembic/versions/20260717_0008_match_resume_snapshot.py`，包含：

**新建表：**

1. `match_reports`
   - `id` VARCHAR(36) PK
   - `analysis_session_id` VARCHAR(36) NOT NULL → `analysis_sessions.id`
   - `job_id` VARCHAR(36) NOT NULL → `job_postings.id`
   - `profile_version_id` VARCHAR(36) NOT NULL → `confirmed_profile_versions.id`
   - `score` INTEGER NOT NULL CHECK (0–100)
   - `scoring_rule_version` VARCHAR(64) NOT NULL
   - `strengths` JSON NOT NULL
   - `gaps` JSON NOT NULL
   - `unknowns` JSON NOT NULL
   - `risks` JSON NOT NULL
   - `application_priority` VARCHAR(20) NOT NULL
   - `recommendation` TEXT NOT NULL
   - `model_version` VARCHAR(64) NOT NULL
   - `prompt_version` VARCHAR(64) NOT NULL
   - `output_schema_version` VARCHAR(64) NOT NULL
   - `status` VARCHAR(20) NOT NULL DEFAULT 'completed'
   - `created_at` DATETIME NOT NULL
   - 索引：`(analysis_session_id)`, `(job_id)`, `(profile_version_id)`

2. `resume_drafts`
   - `id` VARCHAR(36) PK
   - `match_report_id` VARCHAR(36) NOT NULL UNIQUE → `match_reports.id`
   - `profile_version_id` VARCHAR(36) NOT NULL → `confirmed_profile_versions.id`
   - `target_job_id` VARCHAR(36) NOT NULL → `job_postings.id`
   - `diffs` JSON NOT NULL
   - `status` VARCHAR(20) NOT NULL DEFAULT 'draft'
   - `created_at` DATETIME NOT NULL
   - `approved_at` DATETIME
   - `rejected_at` DATETIME

3. `approved_resume_versions`
   - `id` VARCHAR(36) PK
   - `draft_id` VARCHAR(36) NOT NULL → `resume_drafts.id`
   - `profile_version_id` VARCHAR(36) NOT NULL → `confirmed_profile_versions.id`
   - `target_job_id` VARCHAR(36) NOT NULL → `job_postings.id`
   - `approved_facts` JSON NOT NULL
   - `approved_diffs` JSON NOT NULL
   - `attachment_refs` JSON NOT NULL  # 对象存储引用列表
   - `approved_at` DATETIME NOT NULL
   - `approved_by` VARCHAR(36) NOT NULL → `users.id`
   - 唯一约束：`(draft_id)` — 一个草稿只能批准一次

4. `application_snapshots`
   - `id` VARCHAR(36) PK
   - `user_id` VARCHAR(36) NOT NULL → `users.id`
   - `job_id` VARCHAR(36) NOT NULL → `job_postings.id`
   - `approved_resume_version_id` VARCHAR(36) NOT NULL → `approved_resume_versions.id`
   - `profile_version_id` VARCHAR(36) NOT NULL → `confirmed_profile_versions.id`
   - `job_snapshot` JSON NOT NULL
   - `profile_facts` JSON NOT NULL
   - `dynamic_answers` JSON NOT NULL
   - `local_sensitive_requirements` JSON NOT NULL
   - `attachment_refs` JSON NOT NULL
   - `gui_eligible` BOOLEAN NOT NULL
   - `job_status_at_snapshot` VARCHAR(20) NOT NULL
   - `job_review_version_at_snapshot` INTEGER NOT NULL
   - `created_at` DATETIME NOT NULL
   - `created_by` VARCHAR(36) NOT NULL → `users.id`
   - `schema_version` VARCHAR(16) NOT NULL

**补齐外键约束（ALTER TABLE）：**

5. `application_tasks` 增加 FK：
   - `snapshot_id → application_snapshots.id`
   - `target_job_id → job_postings.id`

**添加索引：**

6. `application_tasks(snapshot_id)`, `application_tasks(target_job_id)`（如不存在）
7. `match_reports(created_at DESC)` — 按时间排序
8. `application_snapshots(user_id, created_at DESC)` — 按用户排序
9. `resume_drafts(target_job_id, status)` — 按职位和状态查询

### Task 1.2：添加 ORM 模型

在 `backend/app/db/models.py` 添加四个模型类：
- `MatchReport`
- `ResumeDraft`
- `ApprovedResumeVersion`
- `ApplicationSnapshot`

同时为 `ApplicationTask` 的 `snapshot_id` 和 `target_job_id` 添加 `ForeignKey` 声明。

### Task 1.3：实现确定性校验器

在 `backend/app/services/` 下新建校验模块，不依赖模型调用：

- `match_validators.py`：校验结构化输出 schema、证据引用有效性、评分范围
- `draft_validators.py`：校验差异操作（op 类型、section 非空、fact_ref 可解析）
- `snapshot_validators.py`：校验快照内容白名单、敏感字段不包含明文、`gui_eligible` 一致性

校验器为纯函数，输入 DTO 输出 `(bool, list[str] error_codes)`。

### Task 1.4：实现 Repository

在 `backend/app/repositories/` 下新建：
- `matches.py`：`create_match_report()`、`get_by_id()`、`list_by_session()`
- `drafts.py`：`create_draft()`、`get_by_id()`、`approve_draft()`、`reject_draft()`
- `snapshots.py`：`create_snapshot()`、`get_by_id()`、`list_by_user()`

所有写操作使用乐观并发控制（`expected_version` 或 `state_version`）。

### Task 1.5：Migration 测试

- [ ] `alembic upgrade 0007→0008` 成功
- [ ] `alembic downgrade 0008→0007` 成功
- [ ] 新建表 CRUD 冒烟测试
- [ ] 外键约束验证（无效引用被拒绝）
- [ ] 唯一约束验证

### 阶段 1 完成定义

- [ ] migration 文件存在且 upgrade/downgrade 通过
- [ ] 四个 ORM 模型可被 import 且基本属性正确
- [ ] 校验器纯函数测试通过
- [ ] Repository 创建/读取测试通过（使用测试数据库）

---

## 5. 阶段 2：证据化匹配纵向切片

**目标**：LangGraph 从 `data/jobs.json` 切换到 MySQL + ConfirmedProfileVersion，MatchService 持久化 MatchReport。

### Task 2.1：实现 VerifiedJobSnapshot 和 ConfirmedProfileSnapshot 加载器

在 `backend/app/services/job_snapshot_service.py`：
- `build_verified_job_snapshot(job_id: str) → VerifiedJobSnapshot`
  - 从 `job_postings` + `job_verifications` + `job_source_links` 加载
  - 参数校验：job 状态必须为 `verified`
- 在 `backend/app/services/profile_snapshot_service.py`：
  - `build_confirmed_profile_snapshot(profile_version_id: str) → ConfirmedProfileSnapshot`
    - 从 `confirmed_profile_versions` 加载
    - 过滤本地敏感字段引用

### Task 2.2：LangGraph 结构化输入输出改造

修改 `src/` 下的 graph 和 agents：

- 输入改为单个 `(VerifiedJobSnapshot, ConfirmedProfileSnapshot)` 而非 `data/jobs.json` 批量加载
- 添加 Pydantic 模型定义（`src/schemas.py`）：
  - `StructuredRequirementAssessment`
  - `StructuredMatchResult`
  - `StructuredMatchReport`
- 改造 `score_match` agent：输出结构化 JSON，包含每个评估项的 `field_path` 和 `evidence_ids`
- 改造 `extract_job_requirements`：输出结构化职位要求列表，每条带 `field_path` 映射
- `unknown` 显式标记，不在 prompt 中等价于 "不满足"
- 模型输出解析失败时抛出明确异常（由 MatchService 处理）

### Task 2.3：移除低分覆盖简历文本行为

在 `src/graph.py` 中：
- 移除 `optimize_resume` 节点中直接修改 `resume_text` 的逻辑
- 改为：低分时生成改进建议，存入 state 但不覆盖输入
- ResumeDraft 由阶段 3 的独立服务基于 MatchReport 创建

### Task 2.4：实现 MatchService

在 `backend/app/services/match_service.py`：

```python
class MatchService:
    async def create_match(
        self,
        user_id: str,
        job_id: str,
        profile_version_id: str,
        analysis_session_id: str | None = None
    ) -> MatchReport:
        # 1. 加载并冻结权威输入
        # 2. 校验 job verified + profile version 属于 user
        # 3. 调用 LangGraph（纯计算，不写 DB）
        # 4. 校验结构化输出 (schema + 证据引用)
        # 5. 同一事务持久化 MatchReport
        # 6. 返回 MatchReport
```

关键规则：
- LangGraph 图不直接访问 DB connection 或 repository
- 模型输出校验失败 → `MatchReport.status = 'failed'`，不抛异常
- 证据引用指向不存在或未确认的 evidence → `status = 'failed'`
- 使用 `analysis_session_id` 关联已有分析会话

### Task 2.5：实现 Pydantic Schemas

在 `backend/app/api/match_schemas.py`：
- `CreateMatchRequest`：`job_id`, `profile_version_id`, `analysis_session_id`（optional）
- `MatchReportResponse`：完整 MatchReport DTO（不含内部 ID 引用）
- `RequirementAssessmentResponse`：单个评估项
- `MatchReportListResponse`：分页列表

### Task 2.6：实现 API 路由

在 `backend/app/api/routes/matches.py`：
- `POST /api/matches` — 创建匹配报告
  - 鉴权：当前用户
  - 校验：job_id 对应 verified 职位、profile_version_id 属于当前用户
  - 返回：`MatchReportResponse`
- `GET /api/matches/{match_id}` — 读取单个报告
  - 鉴权：报告属于当前用户
- `GET /api/matches?analysis_session_id=...` — 按会话列出报告

挂载到 `backend/app/api/router.py`。

### Task 2.7：测试

- [ ] VerifiedJobSnapshot 加载器：verified job 正常加载，非 verified 抛异常
- [ ] ConfirmedProfileSnapshot 加载器：正常加载，敏感字段被过滤
- [ ] MatchService：正常流程产生 completed MatchReport
- [ ] MatchService：非 verified job → 422
- [ ] MatchService：模型输出校验失败 → failed MatchReport（不抛异常）
- [ ] MatchService：证据引用无效 → failed MatchReport
- [ ] API：POST/GET 鉴权和所有权
- [ ] LangGraph 不再调用 `load_jobs()`（通过代码检查 + 测试验证）

### 阶段 2 完成定义

- [ ] `POST /api/matches` 可从 verified job + confirmed profile 产生 MatchReport
- [ ] 所有评估结论可追溯到职位字段和档案证据
- [ ] 模型输出校验失败不崩溃，返回 failed 状态
- [ ] `src/graph.py` 不再修改 `resume_text`
- [ ] LangGraph 不直接写 MySQL

---

## 6. 阶段 3：ResumeDraft、批准版本与附件

**目标**：基于 MatchReport 生成差异型简历草稿，用户审批后生成不可变批准版本和加密附件。

### Task 6.1：实现 ResumeDraftService

在 `backend/app/services/resume_draft_service.py`：

```python
class ResumeDraftService:
    async def create_draft(
        self, user_id: str, match_report_id: str
    ) -> ResumeDraft:
        # 1. 加载 MatchReport（必须 completed）
        # 2. 加载 ConfirmedProfileVersion
        # 3. 调用模型生成结构化差异（ResumeDiffOp 列表）
        # 4. 校验差异操作（每项引用已确认事实）
        # 5. 持久化 ResumeDraft（status='draft'）
        # 6. 返回 ResumeDraft

    async def approve_draft(
        self, user_id: str, draft_id: str
    ) -> ApprovedResumeVersion:
        # 1. 加载 ResumeDraft（必须 status='draft'，属于 user）
        # 2. 从已确认事实生成批准简历正文
        # 3. 生成 PDF/DOCX 附件 → 加密对象存储写入
        # 4. 同一事务：创建 ApprovedResumeVersion + 更新 draft status='approved'
        # 5. 对象写入失败 → 事务回滚，不产生批准版本

    async def reject_draft(self, user_id: str, draft_id: str) -> ResumeDraft:
        # 更新 status='rejected'
```

关键规则：
- 差异只允许排序、措辞、摘要和内容取舍 — 由 `draft_validators` 校验
- 批准时附件写入失败 → 整体失败，draft 保持 `draft` 状态
- 使用现有 AES-256-GCM 对象存储适配器

### Task 6.2：实现 Pydantic Schemas

在 `backend/app/api/draft_schemas.py`：
- `CreateDraftRequest`：`match_report_id`
- `ResumeDraftResponse`：`draft_id`, `match_report_id`, `job_title`, `company_name`, `diffs`, `status`, `created_at`
- `ResumeDiffOpResponse`：单个差异操作
- `ApprovedResumeVersionResponse`：`id`, `draft_id`, `approved_at`, `attachment_count`
- `DraftListResponse`：分页列表

### Task 6.3：实现 API 路由

在 `backend/app/api/routes/resume_drafts.py`：
- `POST /api/resume-drafts` — 基于 MatchReport 创建草稿
- `GET /api/resume-drafts/{draft_id}` — 读取草稿（含差异详情）
- `GET /api/resume-drafts` — 列出当前用户的草稿
- `POST /api/resume-drafts/{draft_id}/approve` — 批准草稿
- `POST /api/resume-drafts/{draft_id}/reject` — 拒绝草稿

挂载到 `router.py`。

### Task 6.4：附件生成

在 `backend/app/services/attachment_service.py`：
- `generate_resume_pdf(approved_facts: dict, diffs: list) → bytes`
- `generate_resume_docx(approved_facts: dict, diffs: list) → bytes`
- 使用现有加密对象存储适配器写入，返回对象引用

### Task 6.5：测试

- [ ] 从 completed MatchReport 创建草稿成功
- [ ] 从 failed MatchReport 创建草稿 → 422
- [ ] 差异操作校验：非法的 op 类型被拒绝
- [ ] 差异操作校验：fact_ref 不存在 → 失败
- [ ] 批准草稿：生成 ApprovedResumeVersion + 附件
- [ ] 批准草稿：对象存储写入失败 → 事务回滚，draft 保持 draft
- [ ] 重复批准同一 draft → 409
- [ ] 批准非本人 draft → 404
- [ ] 附件加密验证（MinIO 中只有 AES-GCM 密文）

### 阶段 3 完成定义

- [ ] 草稿可创建、审查、批准、拒绝
- [ ] 每项差异引用已确认事实
- [ ] 批准生成不可变版本和加密附件
- [ ] 附件写入失败不会产生孤儿批准版本

---

## 7. 阶段 4：ApplicationSnapshot 与 Executor 接入

**目标**：创建不可变投递快照，接入 Executor 真实非敏感快照 DTO。

### Task 7.1：实现 ApplicationSnapshotService

在 `backend/app/services/application_snapshot_service.py`：

```python
class ApplicationSnapshotService:
    async def create_snapshot(
        self,
        user_id: str,
        job_id: str,
        approved_resume_version_id: str,
        dynamic_answers: dict[str, str]
    ) -> ApplicationSnapshot:
        # 1. 加载 verified job + approved resume version
        # 2. 加载 confirmed profile facts（过滤敏感字段）
        # 3. 构建快照内容（白名单过滤）
        # 4. 记录创建时的 gui_eligible、job_status、review_version
        # 5. 持久化不可变快照
        # 6. 返回 ApplicationSnapshot（不返回对象 key）

    async def create_application_task(
        self,
        user_id: str,
        snapshot_id: str,
        device_id: str | None = None
    ) -> ApplicationTask:
        # 1. 加载 snapshot，校验属于 user
        # 2. 校验 snapshot.gui_eligible == True
        # 3. 校验 job 当前 status == verified（未失效）
        # 4. 校验 approved_resume_version 可用
        # 5. 创建 ApplicationTask (status=CREATED)
        # 6. 包含 snapshot_id 引用
        # 校验失败返回对应错误码
```

关键规则：
- 快照创建后永不修改
- `gui_eligible=false` → 可以创建快照，但不能创建 ApplicationTask（返回 `SNAPSHOT_GUI_NOT_ELIGIBLE`）
- 职位已失效 → 不能创建 ApplicationTask（返回 `SNAPSHOT_JOB_EXPIRED`）
- 快照中的 `gui_eligible`/`job_status`/`review_version` 是创建时的值，后续职位变化不影响已有快照
- 用户修改档案或简历后 → 必须创建新快照，旧快照保留审计
- API DTO 不返回对象 key、敏感字段明文、内部存储引用

### Task 7.2：实现 Task 派发校验

在 `backend/app/services/application_task_service.py`（扩展现有 or 新建）：

```python
async def dispatch_task(self, task_id: str, device_id: str) -> ApplicationTask:
    # 1. 加载 task，校验 snapshot 存在且属于同一 user
    # 2. 重新校验 snapshot.gui_eligible == True
    # 3. 重新校验 job 未失效
    # 4. 校验 device 配对有效且属于同一 user
    # 5. 执行状态转换 CREATED/WAITING_FOR_DEVICE → DISPATCHED
    # 6. 返回 task + snapshot 的非敏感字段 DTO
```

### Task 7.3：实现 Pydantic Schemas

在 `backend/app/api/snapshot_schemas.py`：
- `CreateSnapshotRequest`：`job_id`, `approved_resume_version_id`, `dynamic_answers`
- `ApplicationSnapshotResponse`：不含对象 key、敏感字段
- `ApplicationSnapshotListResponse`：分页
- `CreateApplicationTaskRequest`：`snapshot_id`, `device_id`（optional）
- `SnapshotTaskEligibilityResponse`：`can_create_task`, `reason_code`

### Task 7.4：实现 API 路由

在 `backend/app/api/routes/application_snapshots.py`：
- `POST /api/application-snapshots` — 创建快照
- `GET /api/application-snapshots/{snapshot_id}` — 读取快照
- `GET /api/application-snapshots` — 列出当前用户的快照
- `POST /api/application-snapshots/{snapshot_id}/create-task` — 从快照创建 ApplicationTask
- `GET /api/application-snapshots/{snapshot_id}/task-eligibility` — 查询是否可创建 GUI 任务

挂载到 `router.py`。

### Task 7.5：Executor 真实快照对接

- 修改 Executor task payload（`executor_schemas.py`）：从 fixture 字段切换到 `ApplicationSnapshot` 的非敏感字段
- `ExecutorTaskPayload` 包含：`task_id`, `snapshot_id`, `job_url`, 非敏感字段需求, `local_sensitive_requirements`（语义键）, 附件下载 token
- 确保不回传：对象 key、完整简历、完整表单值、敏感字段语义以外的任何信息

### Task 7.6：测试

- [ ] 创建快照：内容白名单正确，无敏感字段
- [ ] `gui_eligible=false` → 可创建快照
- [ ] `gui_eligible=false` → 创建 ApplicationTask → `SNAPSHOT_GUI_NOT_ELIGIBLE`
- [ ] `gui_eligible=true` + 职位 verified → 可创建 ApplicationTask
- [ ] 职位后续失效 → 已有快照不受影响，但新建 Task → `SNAPSHOT_JOB_EXPIRED`
- [ ] 快照属主校验：非本人快照 → 404
- [ ] Executor 获取的 task payload 不含对象 key 和敏感字段
- [ ] 旧快照不能被静默修改

### 阶段 4 完成定义

- [ ] 不可变快照可创建，内容白名单正确
- [ ] `gui_eligible` 双重校验逻辑正确
- [ ] ApplicationTask 创建/派发时校验 snapshot 完整约束
- [ ] Executor 获取真实快照 DTO

---

## 8. 阶段 5：前端路由与三页闭环

**目标**：引入 vue-router，机械迁移现有视图，新增匹配/草稿/快照页面。

### Task 8.1：引入 vue-router

- 安装 `vue-router@4`
- 创建 `frontend/src/router/index.ts`：
  - 路由表按功能域组织：
    - `/analysis` → 分析工作台
    - `/jobs` → 职位中心
    - `/jobs/submissions` → 手动提交
    - `/profile` → 档案工作台
    - `/matching` → 匹配工作台（新增）
    - `/matching/:matchId/draft` → 简历草稿审查（新增）
    - `/snapshots` → 投递快照列表（新增）
    - `/snapshots/:id` → 快照详情（新增）
    - `/devices` → 设备（槽位）
    - `/admin/jobs` → 管理：职位审核
    - `/admin/submissions` → 管理：提交审核
    - `/admin/feedbacks` → 管理：反馈
- 创建 `frontend/src/router/guards.ts`：
  - `requireAuth`：未登录重定向到登录
  - `requireAdmin`：非管理员重定向到首页
- 创建 `frontend/src/components/AppShell.vue`：
  - 导航栏 + `<router-view>` + 用户信息
  - 根据角色显示不同的导航项

### Task 8.2：机械迁移现有视图

将 `App.vue` 中的 7 个 `v-if` 视图拆分为独立页面组件：

| 现有视图 | 新页面组件 | 路由 |
| --- | --- | --- |
| `analysis` | `features/analysis/AnalysisWorkspace.vue` | `/analysis` |
| `jobs` | `features/jobs/JobCenter.vue` | `/jobs` |
| `job_submissions` | `features/job-submissions/JobSubmissions.vue` | `/jobs/submissions` |
| `profile` | `features/profile/ProfileWorkspace.vue` | `/profile` |
| `job_review` | `features/jobs/AdminJobReview.vue` | `/admin/jobs` |
| `admin_job_submissions` | `features/job-submissions/AdminJobSubmissions.vue` | `/admin/submissions` |
| `admin_feedbacks` | `features/jobs/AdminJobFeedback.vue` | `/admin/feedbacks` |

迁移规则：
- 只做代码移动和路由接线
- 不改变现有页面内部逻辑
- 现有 `api.ts` / types 保持兼容
- `App.vue` 精简为 `<AppShell>` + `<router-view>`

### Task 8.3：新增匹配工作台页面

`frontend/src/features/matching/MatchingWorkspace.vue`：
- 选择 verified 职位（下拉/搜索）
- 选择 confirmed profile version（下拉）
- 点击"开始匹配" → 调用 `POST /api/matches`
- 展示 MatchReport：
  - 评分和优先级建议
  - strengths / gaps / unknowns 分类展示
  - 每项评估可展开查看证据引用
- "生成定制简历"按钮 → 导航到草稿页面

新增 `frontend/src/features/matching/matchingApi.ts` 和 `matchingTypes.ts`。

### Task 8.4：新增简历草稿审查页面

`frontend/src/features/matching/ResumeDraftReview.vue`：
- 左右对比视图：原始档案事实 vs 定制后简历
- 差异高亮（按 ResumeDiffOp 渲染）
- 每项差异显示引用的已确认事实
- "批准"按钮 → `POST /api/resume-drafts/{id}/approve`
- "拒绝"按钮 → `POST /api/resume-drafts/{id}/reject`
- 批准后显示 ApprovedResumeVersion 信息和附件链接

新增 `frontend/src/features/matching/draftApi.ts` 和 `draftTypes.ts`。

### Task 8.5：新增投递快照页面

`frontend/src/features/snapshots/SnapshotList.vue`：
- 显示当前用户的所有快照
- 每个快照显示：公司、职位、创建时间、`gui_eligible` 状态
- "查看详情" → SnapshotDetail

`frontend/src/features/snapshots/SnapshotDetail.vue`：
- 显示快照内容摘要（不含敏感字段）
- 显示已批准简历引用和附件
- `gui_eligible=true` → "创建投递任务"按钮（需选择设备）
- `gui_eligible=false` → 显示"仅可人工投递"提示
- 查看任务创建 eligibility

新增 `frontend/src/features/snapshots/snapshotApi.ts` 和 `snapshotTypes.ts`。

### Task 8.6：测试

- [ ] vue-router 路由导航正确
- [ ] 认证 guard：未登录 → 重定向
- [ ] 管理员 guard：非 admin → 重定向
- [ ] 现有 7 个视图功能不变（冒烟测试）
- [ ] 匹配工作台：可创建和查看匹配报告
- [ ] 草稿审查：可查看差异、批准、拒绝
- [ ] 快照页面：可查看列表和详情
- [ ] `vue-tsc` 类型检查通过
- [ ] production build 成功
- [ ] 现有 Vitest 通过

### 阶段 5 完成定义

- [ ] vue-router 正常工作，路由 guard 生效
- [ ] 现有功能无回归
- [ ] 匹配/草稿/快照三页面可用
- [ ] `vue-tsc` + production build 通过

---

## 9. 阶段 6：切换旧入口与发布门禁

**目标**：删除旧分析入口，运行全局门禁，验证完整纵向闭环。

### Task 9.1：删除旧分析路由

- 删除 `backend/app/api/routes/analysis.py` 中 `POST /api/analysis/run` 端点
- 从 `backend/app/api/router.py` 移除 analysis router 挂载
- 确认 `data/jobs.json` 不再被 Web 路径导入
- CLI 保留本地 demo 路径（`cli.py` 或 `src/cli/`），与 Web application service 分离
- 如果需要保留旧 analysis router 用于 session 列表/激活/历史，只保留非 `/run` 端点

### Task 9.2：全局测试与门禁

运行以下所有验证并确认通过：

**后端：**
- [ ] Python 完整回归测试（pytest）
- [ ] Ruff lint 无新问题
- [ ] `alembic upgrade 0004→0008` 一次成功
- [ ] `alembic downgrade 0008→0004` 一次成功
- [ ] MySQL 事务隔离测试
- [ ] Redis lease 测试
- [ ] MinIO 加密对象存储测试

**前端：**
- [ ] Vitest 全部通过
- [ ] `vue-tsc` 类型检查通过
- [ ] Production build 成功

**系统：**
- [ ] Docker Compose 重建 + migration + live/ready 探针
- [ ] Nginx 代理链验证
- [ ] 核心页面冒烟测试

**安全与隐私：**
- [ ] 越权测试：学生不能访问其他用户的 match/draft/snapshot
- [ ] 越权测试：学生不能访问管理端点
- [ ] 日志泄漏测试：match/draft/snapshot 日志不含敏感字段
- [ ] API 响应不含对象 key、完整简历、敏感字段
- [ ] ApplicationSnapshot 不含本地敏感字段明文
- [ ] task:submit scope 不存在
- [ ] 模型不能改变档案确认状态或职位核验状态

### Task 9.3：纵向 E2E 闭环验证

使用 fixture 数据完成以下完整链路：

```
verified JobPosting + ConfirmedProfileVersion
  → POST /api/matches → MatchReport (completed, 所有结论有证据引用)
  → POST /api/resume-drafts → ResumeDraft (每项差异引用已确认事实)
  → POST /api/resume-drafts/{id}/approve → ApprovedResumeVersion + 加密附件
  → POST /api/application-snapshots → ApplicationSnapshot (不可变, 无敏感字段)
  → POST /api/application-snapshots/{id}/create-task → ApplicationTask (CREATED)
  → Executor 获取 task payload (非敏感字段 only)
```

额外验证：
- [ ] Redis checkpoint 丢失 → MySQL 权威产物不受影响
- [ ] Snapshot 创建后修改职位状态 → 已有快照不变
- [ ] `gui_eligible=false` 职位 → 快照可创建，Task 创建被拒绝
- [ ] 模型输出包含虚构证据引用 → MatchReport status = failed
- [ ] Web 路径不导入 `load_jobs()` 或 `load_sample_resume()`

### Task 9.4：清理

- [ ] 删除 `data/jobs.json`（或移至 CLI-only 目录）
- [ ] 确认 `docker-compose.yml` 不依赖 `data/jobs.json`
- [ ] 更新 `docs/runbooks/` 中的相关文档
- [ ] 代码库中搜索并清理所有 `load_jobs` / `load_sample_resume` 引用（CLI 路径除外）

### 阶段 6 完成定义

- [ ] 所有门禁通过
- [ ] E2E 闭环验证完成
- [ ] 旧分析入口已删除
- [ ] 隐私和安全测试全部通过

---

## 10. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
| --- | --- | --- | --- |
| 模型输出不稳定，证据引用无法通过校验 | 中 | MatchReport 大量 failed | 阶段 2 先做单职位匹配，prompt 迭代稳定后再放量 |
| vue-router 迁移影响现有功能 | 中 | 回归 | 阶段 5 先建路由壳 + 机械迁移，不变逻辑 |
| 对象存储写入失败导致批准版本丢失 | 低 | 重试 | 阶段 3 明确事务回滚策略，对象先写后提交 DB |
| `gui_eligible` 判断逻辑在快照和 Task 创建间不一致 | 低 | 高危 | 阶段 4 使用同一校验函数，快照保存时值 |
| 前端三页需求膨胀为全量信息架构改版 | 中 | 延期 | 阶段 5 只做路由迁移 + 三新页面，不做整站 redesign |

## 11. 不在此计划范围内

- 真实招聘网站适配（大疆 Moka / 小鹏 / 科大讯飞）— 属于后续 Wave
- 批量投递、高并发网站自动化
- 自动点击最终提交按钮
- 验证码/人机验证处理
- 公众号图片 OCR、二维码解析
- macOS/移动端执行器
- 云端保存本地敏感字段明文
- 使用模型自动确认档案事实或自动发布 verified 职位
- 前端全量信息架构改版/redesign

## 12. 预计工作量

| 阶段 | 预估工作日 | 备注 |
| --- | --- | --- |
| 阶段 0 | 1–2 天 | 契约评审 + fixture |
| 阶段 1 | 2–3 天 | migration + ORM + 校验器 + repository |
| 阶段 2 | 3–4 天 | LangGraph 改造 + MatchService + API |
| 阶段 3 | 2–3 天 | Draft + 批准 + 附件 |
| 阶段 4 | 2–3 天 | Snapshot + Task + Executor 接入 |
| 阶段 5 | 3–4 天 | vue-router + 迁移 + 三新页面 |
| 阶段 6 | 1–2 天 | 门禁 + E2E + 清理 |
| **合计** | **14–21 天** | 单人，含测试和修复 |
