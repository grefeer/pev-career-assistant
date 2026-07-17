# Manual Job Import and Deduplication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让学生私有地提交职位链接或 JD 文本，获得稳定、可解释且不自动合并的重复候选，并让管理员在一个 MySQL 事务中将提交关联到既有职位或提升为 `pending_completion` 职位。

**Architecture:** 新增独立的 `job_submissions` 领域、仓储、服务、API 和前端 feature；`UserJobSubmission` 保存私有输入，`JobDuplicateCandidate` 保存按提交版本生成的解释性候选，`JobSourceLink` 把腾讯来源和用户提交关联到唯一权威 `JobPosting`。纯规范化、候选算法、DTO 和前端 fixture 可与工作流 A 并行；模型与 migration 集成必须等待 `20260717_0005`，并保持 Alembic 单 head。

**Tech Stack:** Python 3.13、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、MySQL 8.4、Vue 3、TypeScript、Vitest、现有 Redis/MinIO/Docker Compose 基础。

## Global Constraints

- 所有新增业务实体使用 36 字符 UUID；所有时间保存为 UTC，并在 API 边界规范化。
- MySQL 是 `UserJobSubmission`、`JobDuplicateCandidate`、`JobSourceLink` 和公开 `JobPosting` 的唯一权威来源；Redis 不保存这些实体的真相。
- 用户提交默认仅本人可见；所有用户数据查询从认证主体确定 `user_id`，跨用户资源统一返回 404。
- 可变提交使用整数 `version` 和请求中的 `expected_version` 乐观并发；候选、来源关系和审计事件只追加，不原地覆盖历史版本。
- 重复识别只能产生稳定、可解释的候选，不得自动合并、自动核验或自动发布职位。
- 管理员只能把提交关联到已有职位，或创建 `pending_completion`；后者仍须通过现有补全、审核和 `JobVerification` 流程才能成为 `verified`。
- 链接只接受 `http/https`，最大 4096 字符，并拒绝 URL userinfo、localhost、环回、链路本地、私网和保留地址；本计划不抓取 URL、不解析二维码、不做 OCR。
- JD 文本最大 100000 字符；公开 DTO、普通管理员列表 DTO、错误响应和日志不得包含完整原始 JD、提交者身份或 URL userinfo/token。
- 学生和公开职位 DTO 使用显式白名单；`JobSourceLink.submission_id`、`UserJobSubmission.user_id` 和完整私有输入不得进入公开职位响应。
- 腾讯重同步不得删除 `JobSourceLink`，不得覆盖已进入人工流程的规范字段，只能更新现有 `source_candidate` 规则允许的字段。
- 所有新 API 错误使用稳定 `detail.code`，404 表示不存在或所有权隐藏，409 表示版本/状态冲突，422 表示领域校验失败，503 表示必需依赖不可用。
- 日志和 `AuditEvent.redacted_payload` 只记录实体 ID、动作、稳定错误码和脱敏计数，不记录完整 JD、提交者账号、token、外部错误正文或带敏感查询参数的 URL。
- migration 顺序固定：`20260717_0006` 的 `down_revision` 必须是 `20260717_0005`；不得创建 Alembic 分支、merge revision 或第二个 head。
- `backend/app/db/models.py`、`backend/app/db/__init__.py`、`backend/app/api/router.py`、`frontend/src/App.vue`、`frontend/src/api.ts`、`alembic/env.py` 和 `docs/runbooks/platform-foundation.md` 是共享集成点；修改前必须确认没有其他工作流同时编辑。
- 本计划不实现通用网页抓取、模型自动确认、跨用户公开评论、真实站点 GUI、证据化匹配、定制简历或 `ApplicationSnapshot`。

---

## File Structure

### 新建文件

- `backend/app/domain/job_submissions.py`：输入安全校验、URL/JD 规范化、稳定指纹和可解释重复候选算法。
- `backend/app/repositories/job_submissions.py`：私有提交、版本化候选、来源关系和管理员行锁查询。
- `backend/app/services/job_submissions.py`：学生生命周期、候选生成失败恢复和管理员事务化提升。
- `backend/app/api/job_submission_schemas.py`：学生/管理员白名单 DTO 和判别式请求校验。
- `backend/app/api/routes/job_submissions.py`：学生与管理员手动职位端点及稳定错误映射。
- `alembic/versions/20260717_0006_manual_job_import_deduplication.py`：在 `0005` 后创建三张表、扩展来源 provider、写入手动来源并回填腾讯来源关系。
- `tests/unit/test_job_submission_domain.py`：URL 安全、文本规范化、指纹和候选解释测试。
- `tests/unit/test_job_submission_repository.py`：所有权、版本候选、来源关系幂等和查询可见性测试。
- `tests/unit/test_job_submission_service.py`：学生状态机、失败恢复、管理员提升与审计测试。
- `tests/contract/test_job_submissions_api.py`：认证、白名单、错误码、并发版本和管理员契约测试。
- `tests/integration/test_job_submissions_mysql.py`：真实 MySQL 行锁、单次提升、migration 往返和腾讯重同步保护测试。
- `frontend/src/features/job-submissions/jobSubmissionTypes.ts`：前端稳定 DTO。
- `frontend/src/features/job-submissions/jobSubmissionsApi.ts`：学生和管理员手动职位 API 客户端。
- `frontend/src/features/job-submissions/JobSubmissions.vue`：学生提交、编辑、候选和送审界面。
- `frontend/src/features/job-submissions/AdminJobSubmissions.vue`：管理员队列、关联、创建待补全和拒绝界面。
- `frontend/src/features/job-submissions/__tests__/jobSubmissionsApi.spec.ts`：API URL、编码和请求体测试。
- `frontend/src/features/job-submissions/__tests__/JobSubmissions.spec.ts`：学生私有流程组件测试。
- `frontend/src/features/job-submissions/__tests__/AdminJobSubmissions.spec.ts`：管理员决策组件测试。

### 修改文件

- `backend/app/db/models.py`：增加四个枚举和三张表模型，并扩展 `JobSourceProvider.USER_SUBMISSION`。
- `backend/app/db/__init__.py`：显式导出新模型和枚举。
- `backend/app/repositories/jobs.py`：每次腾讯 upsert 幂等保留腾讯 `JobSourceLink`。
- `backend/app/api/router.py`：只挂载独立 `job_submissions.router`。
- `tests/unit/test_job_models.py`：验证列、唯一约束、外键与枚举值。
- `tests/unit/test_job_repository.py`、`tests/unit/test_job_sync_service.py`：验证腾讯来源关系回填和重同步不删除关系。
- `tests/integration/test_mysql_migration.py`：验证 `0005 → 0006 → 0005` 和单 head。
- `tests/security/test_no_sensitive_logging.py`：验证原始 JD、提交者身份和敏感 URL 不进入日志/错误响应。
- `frontend/src/api.ts`、`frontend/src/__tests__/api.spec.ts`：让共享请求层识别稳定 `detail.code`，保留既有 `error_code` 兼容。
- `frontend/src/App.vue`：在共享入口挂载学生手动职位与管理员处理页，不内联领域逻辑。
- `frontend/src/__tests__/App.spec.ts`：验证导航、角色边界和草稿离开保护。
- `docs/runbooks/platform-foundation.md`：记录 migration、人工处理、冲突恢复和验收命令。

## Interfaces and Dependency Gates

| Task | Produces | Consumes | Parallel / Blocking |
| --- | --- | --- | --- |
| 0 | 冻结的 ID、状态、DTO、隐私、migration 与共享文件门禁 | 已批准规格、当前 head | 阻塞共享模型和 migration；不阻塞 Task 1–2、7–8 的 fixture 开发 |
| 1 | `normalize_submission_input`、`DuplicateDetector` | 仅 Python 标准库 | 可与 A/C/D 和 Task 2 并行 |
| 2 | Pydantic/TypeScript DTO 与 API 客户端 | Task 0 契约 | 可使用 fixture 并行，不修改共享入口 |
| 3 | `0006`、SQLAlchemy 模型、手动来源和腾讯回填 | 已合并 `0005` | 严格等待 `0005`；阻塞 Task 4–6 的数据库实现 |
| 4 | 私有仓储、来源关系、腾讯 upsert 保留 | Task 1、Task 3 | 阻塞服务与真实 API |
| 5 | `JobSubmissionService` | Task 1、Task 4 | 阻塞写 API |
| 6 | 完整 HTTP 契约 | Task 2、Task 5 | 阻塞真实前端联调 |
| 7 | 学生 feature | Task 2 fixture；最终消费 Task 6 | 可在 Task 3–5 期间并行 |
| 8 | 管理员 feature | Task 2 fixture；最终消费 Task 6 | 可与 Task 7 并行 |
| 9 | 共享 router/App/runbook 接线 | Task 6–8；共享文件协调锁 | 必须短暂串行合并 |
| 10 | MySQL、安全、Compose 与全局回归证据 | Task 3–9 | 阻塞工作流 B 完成声明 |

### Frozen Types and HTTP Contract

```text
SubmissionInputType = "url" | "jd_text"
SubmissionStatus = "draft" | "submitted" | "promoted" | "rejected"
DeduplicationStatus = "pending" | "succeeded" | "failed"
JobSourceLinkType = "tencent_smartsheet" | "user_submission"
Duplicate algorithm version = "manual-job-dedup-v1"
Duplicate score = integer basis points in [0, 10000]
Manual source id = "00000000-0000-4000-8000-000000000006"
Manual source key = "manual-user-submissions"
Manual mapper version = "manual-submission-v1"
```

学生 DTO 只返回 `id,input_type,input_preview,normalized_url,status,version,deduplication_status,deduplication_error_code,promoted_job_id,created_at,updated_at`。管理员 DTO 增加 `content_sha256`，但仍不返回 `user_id`、账号或完整 JD。候选 DTO 只返回职位白名单摘要、`score_basis_points,reasons,score_components,algorithm_version`；学生候选只包含当前 `verified` 职位。

---

### Task 0: Verify the shared contract and integration gates

**Files:**
- Read: `docs/superpowers/specs/2026-07-16-post-foundation-parallel-workstreams-design.md`
- Read: `docs/platform-foundation-handover-summary.md`
- Read: `backend/app/db/models.py`
- Read: `backend/app/api/router.py`
- Read: `frontend/src/App.vue`
- Read: `alembic/versions/`

**Interfaces:**
- Consumes: approved Wave 1 shared contract and workstream A migration `20260717_0005`.
- Produces: a go/no-go decision for shared files; no source-code change and no commit.

- [ ] **Step 1: Verify the checked-out baseline and working tree**

Run:

```powershell
git status --short
git log -1 --oneline
```

Expected: unrelated user changes, if present, are identified and preserved; the approved design commit is reachable. Do not reset or overwrite a dirty shared file.

- [ ] **Step 2: Verify the shared contract has not drifted**

Run:

```powershell
rg -n "20260717_0006|UserJobSubmission|JobDuplicateCandidate|JobSourceLink|不得自动合并|默认.*本人" docs/superpowers/specs/2026-07-16-post-foundation-parallel-workstreams-design.md
```

Expected: matches in sections 4.2, 5.3, 6.2, 7.3, 8, 9 and 10.2; no conflicting status or migration name appears.

- [ ] **Step 3: Record the current Alembic gate**

Run:

```powershell
& .\.venv\Scripts\python.exe -m alembic heads
Get-ChildItem alembic\versions\*.py | Sort-Object Name | Select-Object -ExpandProperty Name
```

Expected before A merges: exactly one head, currently `20260716_0004`; Tasks 1, 2, 7 and 8 may proceed, but Task 3 must not create `0006`. Expected after A merges: exactly one head `20260717_0005`; only then start Task 3.

- [ ] **Step 4: Acquire the shared-file integration window**

Run immediately before Tasks 3 and 9:

```powershell
git status --short -- backend/app/db/models.py backend/app/db/__init__.py backend/app/api/router.py frontend/src/App.vue frontend/src/api.ts alembic/env.py docs/runbooks/platform-foundation.md
```

Expected: no unowned edits in the shared files. If another Wave 1 branch is editing them, continue only the independent feature files and wait for its merge; do not manufacture a second copy or Alembic head.

---

### Task 1: Build safe input normalization and explainable duplicate detection

**Files:**
- Create: `backend/app/domain/job_submissions.py`
- Create: `tests/unit/test_job_submission_domain.py`

**Interfaces:**
- Consumes: `SubmissionInputType` string values `url|jd_text`; Python standard library only.
- Produces: `NormalizedSubmission`, `JobFingerprint`, `DuplicateMatch`, `normalize_submission_input(input_type: SubmissionInputType, raw_value: str) -> NormalizedSubmission`, and `DuplicateDetector.find_candidates(submission, jobs) -> list[DuplicateMatch]`.

- [ ] **Step 1: Write failing URL boundary tests**

Create `tests/unit/test_job_submission_domain.py` with:

```python
import pytest

from backend.app.domain.job_submissions import (
    DuplicateDetector,
    InvalidSubmissionInput,
    JobFingerprint,
    SubmissionInputType,
    normalize_submission_input,
)


@pytest.mark.parametrize(
    "value",
    [
        "ftp://jobs.example.com/1",
        "https://user:secret@jobs.example.com/1",
        "http://localhost/jobs/1",
        "http://127.0.0.1/jobs/1",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.8/jobs/1",
        "http://192.168.1.8/jobs/1",
        "http://[::1]/jobs/1",
        "https://careers.internal/jobs/1",
    ],
)
def test_url_input_rejects_unsafe_targets(value: str) -> None:
    with pytest.raises(InvalidSubmissionInput) as exc_info:
        normalize_submission_input(SubmissionInputType.URL, value)
    assert exc_info.value.error_code == "unsafe_job_url"


def test_url_input_canonicalizes_without_fetching() -> None:
    result = normalize_submission_input(
        SubmissionInputType.URL,
        "HTTPS://Jobs.Example.COM:443/opening/1?utm_source=feed&lang=zh#apply",
    )
    assert result.normalized_url == "https://jobs.example.com/opening/1?lang=zh"
    assert result.content_sha256 == result.fingerprint
    assert result.preview == "https://jobs.example.com/opening/1?lang=zh"
```

- [ ] **Step 2: Write failing JD and duplicate explanation tests**

Append:

```python
def test_jd_text_has_explicit_size_boundary_and_redacted_preview() -> None:
    result = normalize_submission_input(
        SubmissionInputType.JD_TEXT,
        "  后端实习生\r\n负责 FastAPI   与 MySQL 开发。  ",
    )
    assert result.normalized_text == "后端实习生 负责 fastapi 与 mysql 开发。"
    assert result.preview == "后端实习生 负责 FastAPI 与 MySQL 开发。"
    assert len(result.preview) <= 240
    with pytest.raises(InvalidSubmissionInput) as exc_info:
        normalize_submission_input(SubmissionInputType.JD_TEXT, "x" * 100_001)
    assert exc_info.value.error_code == "job_description_too_large"


def test_duplicate_detector_returns_stable_explanations_without_merging() -> None:
    submission = normalize_submission_input(
        SubmissionInputType.URL,
        "https://jobs.example.com/opening/1?utm_campaign=summer",
    )
    matches = DuplicateDetector().find_candidates(
        submission,
        [
            JobFingerprint(
                job_id="job-1",
                apply_url="https://jobs.example.com/opening/1",
                description_text="不同文本",
            ),
            JobFingerprint(
                job_id="job-2",
                apply_url="https://jobs.example.com/opening/2",
                description_text="不同文本",
            ),
        ],
    )
    assert [(item.job_id, item.score_basis_points) for item in matches] == [
        ("job-1", 10_000)
    ]
    assert matches[0].reasons == ("canonical_apply_url_exact",)
    assert matches[0].score_components == {"canonical_url": 10_000}
    assert matches[0].algorithm_version == "manual-job-dedup-v1"


def test_jd_overlap_below_threshold_is_not_a_candidate() -> None:
    submission = normalize_submission_input(
        SubmissionInputType.JD_TEXT,
        "负责 python fastapi mysql redis 后端服务开发和测试",
    )
    matches = DuplicateDetector().find_candidates(
        submission,
        [
            JobFingerprint("job-match", None, "负责 Python FastAPI MySQL Redis 后端服务开发和测试"),
            JobFingerprint("job-noise", None, "市场运营 内容编辑 品牌活动"),
        ],
    )
    assert [item.job_id for item in matches] == ["job-match"]
    assert matches[0].reasons == ("jd_token_overlap",)
    assert matches[0].score_basis_points >= 7200
```

- [ ] **Step 3: Run the domain tests and verify they fail**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_job_submission_domain.py -q
```

Expected: collection FAIL with `ModuleNotFoundError: backend.app.domain.job_submissions`.

- [ ] **Step 4: Implement the pure domain contract**

Create `backend/app/domain/job_submissions.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


MAX_JOB_URL_LENGTH = 4096
MAX_JD_TEXT_LENGTH = 100_000
DUPLICATE_ALGORITHM_VERSION = "manual-job-dedup-v1"
MIN_TEXT_OVERLAP_BPS = 7200


class SubmissionInputType(StrEnum):
    URL = "url"
    JD_TEXT = "jd_text"


class SubmissionStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    PROMOTED = "promoted"
    REJECTED = "rejected"


class DeduplicationStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobSourceLinkType(StrEnum):
    TENCENT_SMARTSHEET = "tencent_smartsheet"
    USER_SUBMISSION = "user_submission"


class InvalidSubmissionInput(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class NormalizedSubmission:
    input_type: SubmissionInputType
    original_url: str | None
    original_jd: str | None
    normalized_url: str | None
    normalized_text: str | None
    content_sha256: str
    fingerprint: str
    preview: str


@dataclass(frozen=True)
class JobFingerprint:
    job_id: str
    apply_url: str | None
    description_text: str | None


@dataclass(frozen=True)
class DuplicateMatch:
    job_id: str
    score_basis_points: int
    reasons: tuple[str, ...]
    score_components: dict[str, int]
    algorithm_version: str = DUPLICATE_ALGORITHM_VERSION


def _canonicalize_url(value: str) -> str:
    if len(value) > MAX_JOB_URL_LENGTH:
        raise InvalidSubmissionInput("job_url_too_large")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise InvalidSubmissionInput("invalid_job_url") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise InvalidSubmissionInput("invalid_job_url")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidSubmissionInput("unsafe_job_url")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise InvalidSubmissionInput("unsafe_job_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise InvalidSubmissionInput("unsafe_job_url")
    scheme = parsed.scheme.lower()
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host if port is None or default_port else f"{host}:{port}"
    safe_query = sorted(
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    )
    return urlunsplit((scheme, netloc, parsed.path or "/", urlencode(safe_query), ""))


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def _preview(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())[:240]


def normalize_submission_input(
    input_type: SubmissionInputType, raw_value: str
) -> NormalizedSubmission:
    if input_type is SubmissionInputType.URL:
        canonical = _canonicalize_url(raw_value)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return NormalizedSubmission(
            input_type=input_type,
            original_url=raw_value.strip(),
            original_jd=None,
            normalized_url=canonical,
            normalized_text=None,
            content_sha256=digest,
            fingerprint=digest,
            preview=canonical[:240],
        )
    if len(raw_value) > MAX_JD_TEXT_LENGTH:
        raise InvalidSubmissionInput("job_description_too_large")
    normalized = _normalize_text(raw_value)
    if not normalized:
        raise InvalidSubmissionInput("empty_job_description")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return NormalizedSubmission(
        input_type=input_type,
        original_url=None,
        original_jd=raw_value,
        normalized_url=None,
        normalized_text=normalized,
        content_sha256=digest,
        fingerprint=digest,
        preview=_preview(raw_value),
    )


def _tokens(value: str) -> set[str]:
    ascii_tokens = set(re.findall(r"[a-z0-9+#.]{2,}", value.lower()))
    han_runs = re.findall(r"[\u4e00-\u9fff]+", value)
    han_bigrams = {
        run[index : index + 2]
        for run in han_runs
        for index in range(max(0, len(run) - 1))
    }
    return ascii_tokens | han_bigrams


class DuplicateDetector:
    def find_candidates(
        self,
        submission: NormalizedSubmission,
        jobs: list[JobFingerprint],
    ) -> list[DuplicateMatch]:
        matches: list[DuplicateMatch] = []
        for job in jobs:
            if submission.normalized_url and job.apply_url:
                try:
                    canonical_job_url = _canonicalize_url(job.apply_url)
                except InvalidSubmissionInput:
                    canonical_job_url = None
                if canonical_job_url == submission.normalized_url:
                    matches.append(
                        DuplicateMatch(
                            job_id=job.job_id,
                            score_basis_points=10_000,
                            reasons=("canonical_apply_url_exact",),
                            score_components={"canonical_url": 10_000},
                        )
                    )
                    continue
            if submission.normalized_text and job.description_text:
                left = _tokens(submission.normalized_text)
                right = _tokens(_normalize_text(job.description_text))
                union = left | right
                score = round(10_000 * len(left & right) / len(union)) if union else 0
                if score >= MIN_TEXT_OVERLAP_BPS:
                    matches.append(
                        DuplicateMatch(
                            job_id=job.job_id,
                            score_basis_points=score,
                            reasons=("jd_token_overlap",),
                            score_components={"jd_token_jaccard": score},
                        )
                    )
        return sorted(matches, key=lambda item: (-item.score_basis_points, item.job_id))
```

- [ ] **Step 5: Run focused tests and lint**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_job_submission_domain.py -q
& .\.venv\Scripts\python.exe -m ruff check backend/app/domain/job_submissions.py tests/unit/test_job_submission_domain.py
```

Expected: all domain tests PASS and Ruff prints `All checks passed!`.

- [ ] **Step 6: Commit Task 1**

```powershell
git add backend/app/domain/job_submissions.py tests/unit/test_job_submission_domain.py
git commit -m "feat: add safe manual job normalization"
```

---

### Task 2: Freeze backend and frontend DTOs before database integration

**Files:**
- Create: `backend/app/api/job_submission_schemas.py`
- Create: `frontend/src/features/job-submissions/jobSubmissionTypes.ts`
- Create: `frontend/src/features/job-submissions/jobSubmissionsApi.ts`
- Create: `frontend/src/features/job-submissions/__tests__/jobSubmissionsApi.spec.ts`

**Interfaces:**
- Consumes: Task 1 enum string values and Frozen Types contract.
- Produces: `JobSubmissionCreateRequest`, `JobSubmissionUpdateRequest`, `AdminJobSubmissionDecisionRequest`, `JobSubmissionResponse`, `DuplicateCandidateResponse`, and matching TypeScript types/functions.

- [ ] **Step 1: Write failing schema tests**

Append to `tests/unit/test_job_submission_domain.py`:

```python
from pydantic import ValidationError

from backend.app.api.job_submission_schemas import (
    AdminJobSubmissionDecisionRequest,
    JobSubmissionCreateRequest,
)


def test_create_request_requires_exactly_one_matching_input() -> None:
    request = JobSubmissionCreateRequest(input_type="url", url="https://jobs.example.com/1")
    assert request.jd_text is None
    with pytest.raises(ValidationError):
        JobSubmissionCreateRequest(input_type="url", url=None, jd_text="JD")


def test_admin_decision_is_discriminated_and_complete() -> None:
    request = AdminJobSubmissionDecisionRequest(
        expected_version=2,
        action="create_pending",
        company_name="示例科技",
        title="后端实习生",
        apply_url="https://jobs.example.com/1",
    )
    assert request.job_id is None
    with pytest.raises(ValidationError):
        AdminJobSubmissionDecisionRequest(expected_version=2, action="link_existing")
```

- [ ] **Step 2: Run schema tests and verify they fail**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_job_submission_domain.py -q
```

Expected: collection FAIL because `backend.app.api.job_submission_schemas` does not exist.

- [ ] **Step 3: Implement explicit Pydantic request and response schemas**

Create `backend/app/api/job_submission_schemas.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.domain.job_submissions import (
    DeduplicationStatus,
    SubmissionInputType,
    SubmissionStatus,
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class JobSubmissionCreateRequest(BaseModel):
    input_type: SubmissionInputType
    url: str | None = Field(default=None, max_length=4096)
    jd_text: str | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def validate_matching_input(self) -> Self:
        if self.input_type is SubmissionInputType.URL and self.url and self.jd_text is None:
            return self
        if self.input_type is SubmissionInputType.JD_TEXT and self.jd_text and self.url is None:
            return self
        raise ValueError("input_type must select exactly one non-empty input")


class JobSubmissionUpdateRequest(JobSubmissionCreateRequest):
    expected_version: int = Field(ge=0)


class JobSubmissionSubmitRequest(BaseModel):
    expected_version: int = Field(ge=0)


class JobSubmissionResponse(BaseModel):
    id: str
    input_type: SubmissionInputType
    input_preview: str
    normalized_url: str | None
    status: SubmissionStatus
    version: int
    deduplication_status: DeduplicationStatus
    deduplication_error_code: str | None
    promoted_job_id: str | None
    created_at: datetime
    updated_at: datetime

    _normalize_created = field_validator("created_at", mode="before")(_as_utc)
    _normalize_updated = field_validator("updated_at", mode="before")(_as_utc)


class AdminJobSubmissionResponse(JobSubmissionResponse):
    content_sha256: str


class JobSubmissionListResponse(BaseModel):
    total: int
    submissions: list[JobSubmissionResponse]


class AdminJobSubmissionListResponse(BaseModel):
    total: int
    submissions: list[AdminJobSubmissionResponse]


class DuplicateJobSummary(BaseModel):
    id: str
    company_name: str
    title: str
    status: str
    apply_url: str


class DuplicateCandidateResponse(BaseModel):
    job: DuplicateJobSummary
    score_basis_points: int = Field(ge=0, le=10_000)
    reasons: list[str]
    score_components: dict[str, int]
    algorithm_version: Literal["manual-job-dedup-v1"]


class DuplicateCandidateListResponse(BaseModel):
    candidates: list[DuplicateCandidateResponse]


class AdminJobSubmissionDecisionRequest(BaseModel):
    expected_version: int = Field(ge=0)
    action: Literal["link_existing", "create_pending", "reject"]
    job_id: str | None = Field(default=None, min_length=36, max_length=36)
    company_name: str | None = Field(default=None, min_length=1, max_length=255)
    title: str | None = Field(default=None, min_length=1, max_length=2000)
    apply_url: str | None = Field(default=None, max_length=4096)
    reason_code: Literal[
        "not_a_job", "insufficient_evidence", "unsafe_link", "duplicate_submission"
    ] | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> Self:
        if self.action == "link_existing" and self.job_id and not any(
            (self.company_name, self.title, self.apply_url, self.reason_code)
        ):
            return self
        if self.action == "create_pending" and self.company_name and self.title:
            if self.job_id is None and self.reason_code is None:
                return self
        if self.action == "reject" and self.reason_code and not any(
            (self.job_id, self.company_name, self.title, self.apply_url)
        ):
            return self
        raise ValueError("decision fields do not match action")
```

- [ ] **Step 4: Write the failing frontend API test**

Create `frontend/src/features/job-submissions/__tests__/jobSubmissionsApi.spec.ts`:

```typescript
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createJobSubmission,
  decideJobSubmission,
  fetchDuplicateCandidates,
  submitJobSubmission,
} from "../jobSubmissionsApi";

describe("job submissions API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses private student routes and encodes ids", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "submission/1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await createJobSubmission("token", { input_type: "url", url: "https://jobs.example/1" });
    await fetchDuplicateCandidates("token", "submission/1");
    await submitJobSubmission("token", "submission/1", 3);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/job-submissions");
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/job-submissions/submission%2F1/duplicate-candidates",
    );
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({ expected_version: 3 });
  });

  it("sends an explicit administrator decision", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "submission-1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await decideJobSubmission("admin", "submission-1", {
      expected_version: 2,
      action: "link_existing",
      job_id: "00000000-0000-4000-8000-000000000001",
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/admin/job-submissions/submission-1/decision",
    );
  });
});
```

- [ ] **Step 5: Run the frontend test and verify it fails**

Run:

```powershell
npm.cmd --prefix frontend run test -- jobSubmissionsApi.spec.ts
```

Expected: FAIL because `jobSubmissionTypes.ts` and `jobSubmissionsApi.ts` do not exist.

- [ ] **Step 6: Implement matching TypeScript DTOs and API functions**

Create `frontend/src/features/job-submissions/jobSubmissionTypes.ts`:

```typescript
export type SubmissionInputType = "url" | "jd_text";
export type SubmissionStatus = "draft" | "submitted" | "promoted" | "rejected";
export type DeduplicationStatus = "pending" | "succeeded" | "failed";

export interface JobSubmission {
  id: string;
  input_type: SubmissionInputType;
  input_preview: string;
  normalized_url: string | null;
  status: SubmissionStatus;
  version: number;
  deduplication_status: DeduplicationStatus;
  deduplication_error_code: string | null;
  promoted_job_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdminJobSubmission extends JobSubmission { content_sha256: string }
export interface JobSubmissionList { total: number; submissions: JobSubmission[] }
export interface AdminJobSubmissionList { total: number; submissions: AdminJobSubmission[] }

export interface DuplicateCandidate {
  job: { id: string; company_name: string; title: string; status: string; apply_url: string };
  score_basis_points: number;
  reasons: string[];
  score_components: Record<string, number>;
  algorithm_version: "manual-job-dedup-v1";
}

export type JobSubmissionCreate =
  | { input_type: "url"; url: string; jd_text?: never }
  | { input_type: "jd_text"; jd_text: string; url?: never };

export type AdminJobSubmissionDecision =
  | { expected_version: number; action: "link_existing"; job_id: string }
  | {
      expected_version: number;
      action: "create_pending";
      company_name: string;
      title: string;
      apply_url?: string;
    }
  | {
      expected_version: number;
      action: "reject";
      reason_code: "not_a_job" | "insufficient_evidence" | "unsafe_link" | "duplicate_submission";
    };
```

Create `frontend/src/features/job-submissions/jobSubmissionsApi.ts`:

```typescript
import { request } from "../../api";
import type {
  AdminJobSubmission,
  AdminJobSubmissionDecision,
  AdminJobSubmissionList,
  DuplicateCandidate,
  JobSubmission,
  JobSubmissionCreate,
  JobSubmissionList,
} from "./jobSubmissionTypes";

export const fetchJobSubmissions = (token: string, limit = 20, offset = 0) =>
  request<JobSubmissionList>(`/job-submissions?limit=${limit}&offset=${offset}`, {}, token);

export const createJobSubmission = (token: string, payload: JobSubmissionCreate) =>
  request<JobSubmission>("/job-submissions", { method: "POST", body: JSON.stringify(payload) }, token);

export const updateJobSubmission = (
  token: string, id: string, expectedVersion: number, payload: JobSubmissionCreate,
) => request<JobSubmission>(`/job-submissions/${encodeURIComponent(id)}`, {
  method: "PATCH", body: JSON.stringify({ ...payload, expected_version: expectedVersion }),
}, token);

export const submitJobSubmission = (token: string, id: string, expectedVersion: number) =>
  request<JobSubmission>(`/job-submissions/${encodeURIComponent(id)}/submit`, {
    method: "POST", body: JSON.stringify({ expected_version: expectedVersion }),
  }, token);

export const fetchDuplicateCandidates = (token: string, id: string) =>
  request<{ candidates: DuplicateCandidate[] }>(
    `/job-submissions/${encodeURIComponent(id)}/duplicate-candidates`, {}, token,
  );

export const fetchAdminJobSubmissions = (token: string, limit = 20, offset = 0) =>
  request<AdminJobSubmissionList>(
    `/admin/job-submissions?status=submitted&limit=${limit}&offset=${offset}`, {}, token,
  );

export const decideJobSubmission = (
  token: string, id: string, payload: AdminJobSubmissionDecision,
) => request<AdminJobSubmission>(`/admin/job-submissions/${encodeURIComponent(id)}/decision`, {
  method: "POST", body: JSON.stringify(payload),
}, token);
```

- [ ] **Step 7: Run schema/API tests and typecheck**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_job_submission_domain.py -q
npm.cmd --prefix frontend run test -- jobSubmissionsApi.spec.ts
npm.cmd --prefix frontend run typecheck
```

Expected: Python tests PASS, the frontend test PASS, and `vue-tsc` exits 0.

- [ ] **Step 8: Commit Task 2**

```powershell
git add backend/app/api/job_submission_schemas.py tests/unit/test_job_submission_domain.py frontend/src/features/job-submissions/jobSubmissionTypes.ts frontend/src/features/job-submissions/jobSubmissionsApi.ts frontend/src/features/job-submissions/__tests__/jobSubmissionsApi.spec.ts
git commit -m "feat: define manual job submission contracts"
```

---

### Task 3: Add the `0006` authoritative schema after migration `0005`

**Files:**
- Create: `alembic/versions/20260717_0006_manual_job_import_deduplication.py`
- Modify: `backend/app/db/models.py`
- Modify: `backend/app/db/__init__.py`
- Modify: `tests/unit/test_job_models.py`
- Modify: `tests/integration/test_mysql_migration.py`

**Interfaces:**
- Consumes: the single Alembic head `20260717_0005`; Task 1 enums.
- Produces: `UserJobSubmission`, `JobDuplicateCandidate`, `JobSourceLink`, `JobSourceProvider.USER_SUBMISSION`, fixed manual source row, and migration head `20260717_0006`.

- [ ] **Step 1: Re-run the hard migration gate**

Run:

```powershell
& .\.venv\Scripts\python.exe -m alembic heads
```

Expected: exactly `20260717_0005 (head)`. If the output is `20260716_0004`, stop this task and continue only Tasks 7–8 against fixtures. If more than one head appears, resolve ownership with the other workstream; do not create `0006`.

- [ ] **Step 2: Write failing model tests**

Append to `tests/unit/test_job_models.py`:

```python
from backend.app.db.models import (
    JobDuplicateCandidate,
    JobSourceLink,
    JobSourceProvider,
    UserJobSubmission,
)


def test_manual_job_entities_use_uuid_and_versioned_private_ownership() -> None:
    assert JobSourceProvider.USER_SUBMISSION.value == "user_submission"
    assert {
        "user_id", "input_type", "original_url", "original_jd", "input_preview",
        "normalized_url", "content_sha256", "status", "version",
        "deduplication_status", "deduplication_error_code", "promoted_job_id",
        "rejected_reason_code",
    } <= set(UserJobSubmission.__table__.columns.keys())
    assert UserJobSubmission.__table__.columns.id.type.length == 36


def test_duplicate_candidate_and_source_link_preserve_explanations() -> None:
    assert {
        "submission_id", "candidate_job_id", "generated_for_version",
        "score_basis_points", "reasons", "score_components", "algorithm_version",
    } <= set(JobDuplicateCandidate.__table__.columns.keys())
    assert {
        "job_id", "source_type", "source_id", "submission_id",
        "source_record_ref", "normalized_url", "created_at",
    } <= set(JobSourceLink.__table__.columns.keys())
```

- [ ] **Step 3: Run model tests and verify they fail**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_job_models.py -q
```

Expected: FAIL because the new models and provider value do not exist.

- [ ] **Step 4: Add the SQLAlchemy models**

In `backend/app/db/models.py`, import the Task 1 enums:

```python
from sqlalchemy import CheckConstraint

from backend.app.domain.job_submissions import (
    DeduplicationStatus,
    JobSourceLinkType,
    SubmissionInputType,
    SubmissionStatus,
)
```

Extend `JobSourceProvider` and append the models after `JobVerification`:

```python
class JobSourceProvider(StrEnum):
    TENCENT_SMARTSHEET = "tencent_smartsheet"
    USER_SUBMISSION = "user_submission"


class UserJobSubmission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_job_submissions"
    __table_args__ = (
        Index("ix_user_job_submissions_user_status_updated", "user_id", "status", "updated_at"),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    input_type: Mapped[SubmissionInputType] = mapped_column(
        Enum(SubmissionInputType, name="job_submission_input_type", **enum_kwargs),
        nullable=False,
    )
    original_url: Mapped[str | None] = mapped_column(Text)
    original_jd: Mapped[str | None] = mapped_column(Text)
    input_preview: Mapped[str] = mapped_column(String(240), nullable=False)
    normalized_url: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[SubmissionStatus] = mapped_column(
        Enum(SubmissionStatus, name="job_submission_status", **enum_kwargs),
        default=SubmissionStatus.DRAFT,
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deduplication_status: Mapped[DeduplicationStatus] = mapped_column(
        Enum(DeduplicationStatus, name="job_deduplication_status", **enum_kwargs),
        default=DeduplicationStatus.PENDING,
        nullable=False,
    )
    deduplication_error_code: Mapped[str | None] = mapped_column(String(80))
    promoted_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_postings.id", ondelete="SET NULL"), index=True
    )
    rejected_reason_code: Mapped[str | None] = mapped_column(String(80))


class JobDuplicateCandidate(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_duplicate_candidates"
    __table_args__ = (
        UniqueConstraint(
            "submission_id", "candidate_job_id", "generated_for_version",
            "algorithm_version", name="uq_job_duplicate_candidate_version",
        ),
        CheckConstraint(
            "score_basis_points >= 0 AND score_basis_points <= 10000",
            name="ck_job_duplicate_candidate_score",
        ),
        Index("ix_job_duplicate_candidates_submission_version", "submission_id", "generated_for_version"),
    )
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("user_job_submissions.id", ondelete="CASCADE"), nullable=False
    )
    candidate_job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    generated_for_version: Mapped[int] = mapped_column(Integer, nullable=False)
    score_basis_points: Mapped[int] = mapped_column(Integer, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    score_components: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class JobSourceLink(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_source_links"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "source_type", "source_record_ref", name="uq_job_source_link_record"
        ),
        CheckConstraint(
            "(source_type = 'tencent_smartsheet' AND source_id IS NOT NULL AND submission_id IS NULL) OR "
            "(source_type = 'user_submission' AND source_id IS NULL AND submission_id IS NOT NULL)",
            name="ck_job_source_link_reference",
        ),
        Index("ix_job_source_links_job_created", "job_id", "created_at"),
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[JobSourceLinkType] = mapped_column(
        Enum(JobSourceLinkType, name="job_source_link_type", **enum_kwargs), nullable=False
    )
    source_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_sources.id", ondelete="RESTRICT")
    )
    submission_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_job_submissions.id", ondelete="RESTRICT")
    )
    source_record_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
```

Export the three models and four domain enums from `backend/app/db/__init__.py`; keep the existing exports unchanged.

- [ ] **Step 5: Create migration `0006` with a deterministic manual source and Tencent backfill**

Create `alembic/versions/20260717_0006_manual_job_import_deduplication.py`:

```python
"""add manual job import and deduplication

Revision ID: 20260717_0006
Revises: 20260717_0005
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260717_0006"
down_revision: Union[str, Sequence[str], None] = "20260717_0005"
branch_labels = None
depends_on = None

MANUAL_SOURCE_ID = "00000000-0000-4000-8000-000000000006"
MANUAL_SOURCE_KEY = "manual-user-submissions"


def upgrade() -> None:
    op.drop_constraint("job_source_provider", "job_sources", type_="check")
    op.create_check_constraint(
        "job_source_provider",
        "job_sources",
        "provider IN ('tencent_smartsheet','user_submission')",
    )
    op.create_table(
        "user_job_submissions",
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column(
            "input_type",
            sa.Enum("url", "jd_text", name="job_submission_input_type", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("original_url", sa.Text(), nullable=True),
        sa.Column("original_jd", sa.Text(), nullable=True),
        sa.Column("input_preview", sa.String(240), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "submitted", "promoted", "rejected", name="job_submission_status", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "deduplication_status",
            sa.Enum("pending", "succeeded", "failed", name="job_deduplication_status", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("deduplication_error_code", sa.String(80), nullable=True),
        sa.Column("promoted_job_id", sa.String(36), nullable=True),
        sa.Column("rejected_reason_code", sa.String(80), nullable=True),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["promoted_job_id"], ["job_postings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_job_submissions_user_status_updated",
        "user_job_submissions", ["user_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_user_job_submissions_promoted_job_id",
        "user_job_submissions", ["promoted_job_id"],
    )
    op.create_table(
        "job_duplicate_candidates",
        sa.Column("submission_id", sa.String(36), nullable=False),
        sa.Column("candidate_job_id", sa.String(36), nullable=False),
        sa.Column("generated_for_version", sa.Integer(), nullable=False),
        sa.Column("score_basis_points", sa.Integer(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("score_components", sa.JSON(), nullable=False),
        sa.Column("algorithm_version", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.CheckConstraint(
            "score_basis_points >= 0 AND score_basis_points <= 10000",
            name="ck_job_duplicate_candidate_score",
        ),
        sa.ForeignKeyConstraint(["candidate_job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["submission_id"], ["user_job_submissions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "submission_id", "candidate_job_id", "generated_for_version",
            "algorithm_version", name="uq_job_duplicate_candidate_version",
        ),
    )
    op.create_index(
        "ix_job_duplicate_candidates_submission_version",
        "job_duplicate_candidates", ["submission_id", "generated_for_version"],
    )
    op.create_table(
        "job_source_links",
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column(
            "source_type",
            sa.Enum("tencent_smartsheet", "user_submission", name="job_source_link_type", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("source_id", sa.String(36), nullable=True),
        sa.Column("submission_id", sa.String(36), nullable=True),
        sa.Column("source_record_ref", sa.String(200), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.CheckConstraint(
            "(source_type = 'tencent_smartsheet' AND source_id IS NOT NULL AND submission_id IS NULL) OR "
            "(source_type = 'user_submission' AND source_id IS NULL AND submission_id IS NOT NULL)",
            name="ck_job_source_link_reference",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["job_sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["submission_id"], ["user_job_submissions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "source_type", "source_record_ref", name="uq_job_source_link_record"),
    )
    op.create_index("ix_job_source_links_job_created", "job_source_links", ["job_id", "created_at"])
    op.execute(
        sa.text(
            "INSERT INTO job_sources "
            "(id, source_key, provider, name, file_id, sheet_id, mapper_version, enabled, created_at, updated_at) "
            "VALUES (:id, :source_key, 'user_submission', '用户手动提交', 'manual', 'manual', "
            "'manual-submission-v1', 0, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
        ).bindparams(id=MANUAL_SOURCE_ID, source_key=MANUAL_SOURCE_KEY)
    )
    op.execute(
        """
        INSERT INTO job_source_links
            (id, job_id, source_type, source_id, submission_id, source_record_ref, normalized_url, created_at)
        SELECT UUID(), jp.id, 'tencent_smartsheet', jp.source_id, NULL,
               CONCAT(jp.source_id, ':', jp.external_record_id), jp.apply_url, UTC_TIMESTAMP()
        FROM job_postings AS jp
        JOIN job_sources AS js ON js.id = jp.source_id
        WHERE js.provider = 'tencent_smartsheet'
        """
    )


def downgrade() -> None:
    op.drop_table("job_source_links")
    op.drop_table("job_duplicate_candidates")
    op.drop_table("user_job_submissions")
    op.execute(
        f"DELETE jv FROM job_verifications jv JOIN job_postings jp ON jp.id = jv.job_id WHERE jp.source_id = '{MANUAL_SOURCE_ID}'"
    )
    op.execute(f"DELETE FROM job_postings WHERE source_id = '{MANUAL_SOURCE_ID}'")
    op.execute(f"DELETE FROM raw_job_records WHERE source_id = '{MANUAL_SOURCE_ID}'")
    op.execute(f"DELETE FROM job_sync_runs WHERE source_id = '{MANUAL_SOURCE_ID}'")
    op.execute(f"DELETE FROM job_sources WHERE id = '{MANUAL_SOURCE_ID}'")
    op.drop_constraint("job_source_provider", "job_sources", type_="check")
    op.create_check_constraint(
        "job_source_provider", "job_sources", "provider IN ('tencent_smartsheet')"
    )
```

- [ ] **Step 6: Extend the destructive migration assertion**

In `tests/integration/test_mysql_migration.py`, add a `0006` phase after the existing `0005` phase:

```python
with engine.connect() as connection:
    seeded_job_count = int(connection.scalar(sa.text("SELECT COUNT(*) FROM job_postings")) or 0)
command.upgrade(config, "20260717_0006")
inspector = sa.inspect(engine)
assert {
    "user_job_submissions", "job_duplicate_candidates", "job_source_links"
} <= set(inspector.get_table_names())
with engine.connect() as connection:
    assert connection.scalar(sa.text("SELECT COUNT(*) FROM job_source_links")) >= seeded_job_count
    manual = connection.execute(
        sa.text("SELECT provider, enabled FROM job_sources WHERE source_key=:key"),
        {"key": "manual-user-submissions"},
    ).one()
    assert manual == ("user_submission", 0)
command.downgrade(config, "20260717_0005")
assert "user_job_submissions" not in sa.inspect(engine).get_table_names()
command.upgrade(config, "head")
```

- [ ] **Step 7: Run model and migration tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_job_models.py -q
& .\.venv\Scripts\python.exe -m pytest tests/integration/test_mysql_migration.py -q -rs
& .\.venv\Scripts\python.exe -m alembic heads
```

Expected: model tests PASS; migration test PASS with the dedicated MySQL variables or skip only with the exact documented opt-in reason; Alembic prints exactly `20260717_0006 (head)`.

- [ ] **Step 8: Commit Task 3**

```powershell
git add backend/app/db/models.py backend/app/db/__init__.py alembic/versions/20260717_0006_manual_job_import_deduplication.py tests/unit/test_job_models.py tests/integration/test_mysql_migration.py
git commit -m "feat: add manual job submission schema"
```

---

### Task 4: Add private repositories and preserve every source relationship

**Files:**
- Create: `backend/app/repositories/job_submissions.py`
- Create: `tests/unit/test_job_submission_repository.py`
- Modify: `backend/app/repositories/jobs.py`
- Modify: `tests/unit/test_job_repository.py`
- Modify: `tests/unit/test_job_sync_service.py`

**Interfaces:**
- Consumes: Task 3 models; Task 1 `JobFingerprint` and `DuplicateMatch`.
- Produces: ownership-scoped CRUD, latest-generated-input-version candidate reads, `ensure_tencent_source_link`, `create_manual_pending_posting`, and administrator `SELECT ... FOR UPDATE` reads.

- [ ] **Step 1: Write failing ownership and versioned-candidate tests**

Create `tests/unit/test_job_submission_repository.py` with a complete isolated fixture and seed helper:

```python
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import (
    DeduplicationStatus, JobPosting, JobPostingStatus, JobSource,
    JobSourceProvider, RawJobRecord, SubmissionInputType,
    SubmissionStatus, User, UserJobSubmission,
)
from backend.app.repositories import job_submissions, jobs
from backend.app.services.job_mappers import NormalizedJobCandidate


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _posting(db: Session, source: JobSource, record_id: str, status: JobPostingStatus) -> JobPosting:
    raw = RawJobRecord(
        source_id=source.id, external_record_id=record_id,
        payload_hash=(record_id[0] * 64), raw_fields=[],
        observed_at=datetime.now(timezone.utc),
    )
    db.add(raw)
    db.flush()
    posting = JobPosting(
        source_id=source.id, external_record_id=record_id, raw_record_id=raw.id,
        status=status, company_name="示例科技", title=f"岗位 {record_id}",
        description_text="负责 Python FastAPI MySQL 后端服务开发",
        locations=[], recruitment_types=[], industries=[],
        apply_url=f"https://jobs.example.com/{record_id}",
        mapper_version=source.mapper_version, source_candidate={},
    )
    db.add(posting)
    db.flush()
    return posting


def test_owned_reads_hide_another_users_submission(db: Session) -> None:
    owner = User(account="owner", nickname="Owner", password_hash="hash")
    other = User(account="other", nickname="Other", password_hash="hash")
    db.add_all([owner, other])
    db.flush()
    item = UserJobSubmission(
        user_id=owner.id, input_type=SubmissionInputType.URL,
        original_url="https://jobs.example.com/1", original_jd=None,
        input_preview="https://jobs.example.com/1",
        normalized_url="https://jobs.example.com/1", content_sha256="a" * 64,
        status=SubmissionStatus.DRAFT, version=0,
        deduplication_status=DeduplicationStatus.PENDING,
    )
    db.add(item)
    db.flush()
    assert job_submissions.get_owned(db, user_id=owner.id, submission_id=item.id) is item
    assert job_submissions.get_owned(db, user_id=other.id, submission_id=item.id) is None


def test_candidate_reads_use_only_current_submission_version(db: Session) -> None:
    owner = User(account="candidate-owner", nickname="Owner", password_hash="hash")
    source = JobSource(
        source_key="candidate-source", provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Candidate Source", file_id="candidate-file", sheet_id="candidate-sheet",
        mapper_version="candidate-v1", enabled=True,
    )
    db.add_all([owner, source])
    db.flush()
    verified = _posting(db, source, "verified", JobPostingStatus.VERIFIED)
    pending = _posting(db, source, "pending", JobPostingStatus.PENDING_COMPLETION)
    submission = UserJobSubmission(
        user_id=owner.id, input_type=SubmissionInputType.JD_TEXT,
        original_url=None, original_jd="负责 Python FastAPI MySQL 后端服务开发",
        input_preview="负责 Python FastAPI MySQL 后端服务开发", normalized_url=None,
        content_sha256="c" * 64, status=SubmissionStatus.DRAFT, version=0,
        deduplication_status=DeduplicationStatus.PENDING,
    )
    db.add(submission)
    db.flush()
    job_submissions.add_candidates(db, submission=submission, matches=[
        job_submissions.PersistedMatch(verified.id, 9000, ["old"], {"x": 9000}, "manual-job-dedup-v1")
    ])
    submission.version = 1
    job_submissions.add_candidates(db, submission=submission, matches=[
        job_submissions.PersistedMatch(pending.id, 9500, ["new"], {"x": 9500}, "manual-job-dedup-v1")
    ])
    submission.version = 2  # submit transition increments the aggregate version without changing input
    student_rows = job_submissions.list_candidates(db, submission=submission, public_only=True)
    admin_rows = job_submissions.list_candidates(db, submission=submission, public_only=False)
    assert student_rows == []
    assert [row[1].id for row in admin_rows] == [pending.id]
```

- [ ] **Step 2: Write failing source-link preservation tests**

Append to `tests/unit/test_job_repository.py`:

```python
def test_tencent_upsert_creates_one_stable_source_link(db: Session) -> None:
    source = seeded_source(db)
    raw = snapshot(db, source_id=source.id, external_record_id="r-link", payload_hash="a" * 64)
    posting, _ = jobs.upsert_posting(db, source=source, raw_record=raw, candidate=candidate())
    posting_again, _ = jobs.upsert_posting(db, source=source, raw_record=raw, candidate=candidate())
    links = db.scalars(select(JobSourceLink).where(JobSourceLink.job_id == posting.id)).all()
    assert posting_again.id == posting.id
    assert len(links) == 1
    assert links[0].source_record_ref == f"{source.id}:r-link"
```

Append to `tests/unit/test_job_sync_service.py`:

```python
def test_tencent_resync_preserves_manual_source_link(db: Session) -> None:
    outcome = sync_once(db)
    posting = db.scalar(select(JobPosting))
    assert posting is not None
    manual = seeded_private_submission(db)
    db.add(JobSourceLink(
        job_id=posting.id, source_type=JobSourceLinkType.USER_SUBMISSION,
        source_id=None, submission_id=manual.id,
        source_record_ref=manual.id, normalized_url=manual.normalized_url,
    ))
    db.commit()
    sync_once(db)
    links = db.scalars(select(JobSourceLink).where(JobSourceLink.job_id == posting.id)).all()
    assert {link.source_type for link in links} == {
        JobSourceLinkType.TENCENT_SMARTSHEET,
        JobSourceLinkType.USER_SUBMISSION,
    }
    assert outcome.postings_created == 1
```

- [ ] **Step 3: Run repository tests and verify they fail**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_job_submission_repository.py tests/unit/test_job_repository.py tests/unit/test_job_sync_service.py -q
```

Expected: FAIL because the repository and source-link upsert do not exist.

- [ ] **Step 4: Implement the focused repository interfaces**

Create `backend/app/repositories/job_submissions.py` with these exact public signatures and implementations:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.db.models import (
    JobDuplicateCandidate, JobPosting, JobPostingStatus, JobSource, JobSourceLink,
    JobSourceLinkType, RawJobRecord, SubmissionStatus, UserJobSubmission,
)


MANUAL_SOURCE_ID = "00000000-0000-4000-8000-000000000006"
MANUAL_MAPPER_VERSION = "manual-submission-v1"


@dataclass(frozen=True)
class PersistedMatch:
    job_id: str
    score_basis_points: int
    reasons: list[str]
    score_components: dict[str, int]
    algorithm_version: str


def get_owned(db: Session, *, user_id: str, submission_id: str) -> UserJobSubmission | None:
    return db.scalar(select(UserJobSubmission).where(
        UserJobSubmission.id == submission_id, UserJobSubmission.user_id == user_id,
    ))


def list_owned(db: Session, *, user_id: str, limit: int, offset: int) -> tuple[int, list[UserJobSubmission]]:
    condition = UserJobSubmission.user_id == user_id
    total = db.scalar(select(func.count()).select_from(UserJobSubmission).where(condition)) or 0
    rows = db.scalars(select(UserJobSubmission).where(condition).order_by(
        UserJobSubmission.updated_at.desc(), UserJobSubmission.id.desc(),
    ).limit(limit).offset(offset)).all()
    return int(total), list(rows)


def get_for_admin(db: Session, *, submission_id: str, lock: bool = False) -> UserJobSubmission | None:
    statement = select(UserJobSubmission).where(UserJobSubmission.id == submission_id)
    if lock:
        statement = statement.execution_options(populate_existing=True).with_for_update()
    return db.scalar(statement)


def list_for_admin(
    db: Session, *, status: SubmissionStatus, limit: int, offset: int,
) -> tuple[int, list[UserJobSubmission]]:
    condition = UserJobSubmission.status == status
    total = db.scalar(select(func.count()).select_from(UserJobSubmission).where(condition)) or 0
    rows = db.scalars(select(UserJobSubmission).where(condition).order_by(
        UserJobSubmission.updated_at.asc(), UserJobSubmission.id.asc(),
    ).limit(limit).offset(offset)).all()
    return int(total), list(rows)


def list_job_fingerprints(db: Session) -> list[JobPosting]:
    return list(db.scalars(select(JobPosting).where(
        JobPosting.status.not_in({JobPostingStatus.REJECTED, JobPostingStatus.EXPIRED})
    ).order_by(JobPosting.updated_at.desc(), JobPosting.id.desc())))


def add_candidates(
    db: Session, *, submission: UserJobSubmission, matches: Sequence[PersistedMatch],
) -> None:
    existing = db.scalar(select(func.count()).select_from(JobDuplicateCandidate).where(
        JobDuplicateCandidate.submission_id == submission.id,
        JobDuplicateCandidate.generated_for_version == submission.version,
    )) or 0
    if existing:
        return
    db.add_all([JobDuplicateCandidate(
        submission_id=submission.id, candidate_job_id=item.job_id,
        generated_for_version=submission.version, score_basis_points=item.score_basis_points,
        reasons=item.reasons, score_components=item.score_components,
        algorithm_version=item.algorithm_version,
    ) for item in matches])
    db.flush()


def list_candidates(
    db: Session, *, submission: UserJobSubmission, public_only: bool,
) -> list[tuple[JobDuplicateCandidate, JobPosting]]:
    latest_generated_version = select(func.max(JobDuplicateCandidate.generated_for_version)).where(
        JobDuplicateCandidate.submission_id == submission.id,
        JobDuplicateCandidate.generated_for_version <= submission.version,
    ).scalar_subquery()
    statement = select(JobDuplicateCandidate, JobPosting).join(
        JobPosting, JobPosting.id == JobDuplicateCandidate.candidate_job_id
    ).where(
        JobDuplicateCandidate.submission_id == submission.id,
        JobDuplicateCandidate.generated_for_version == latest_generated_version,
    )
    if public_only:
        statement = statement.where(JobPosting.status == JobPostingStatus.VERIFIED)
    statement = statement.order_by(
        JobDuplicateCandidate.score_basis_points.desc(), JobPosting.id.asc()
    )
    return [(candidate, posting) for candidate, posting in db.execute(statement)]


def ensure_tencent_source_link(
    db: Session, *, posting: JobPosting, source: JobSource,
) -> JobSourceLink:
    reference = f"{source.id}:{posting.external_record_id}"
    existing = db.scalar(select(JobSourceLink).where(
        JobSourceLink.job_id == posting.id,
        JobSourceLink.source_type == JobSourceLinkType.TENCENT_SMARTSHEET,
        JobSourceLink.source_record_ref == reference,
    ))
    if existing is not None:
        return existing
    link = JobSourceLink(
        job_id=posting.id, source_type=JobSourceLinkType.TENCENT_SMARTSHEET,
        source_id=source.id, submission_id=None, source_record_ref=reference,
        normalized_url=posting.apply_url,
    )
    db.add(link)
    db.flush()
    return link


def create_manual_pending_posting(
    db: Session, *, submission: UserJobSubmission, company_name: str,
    title: str, apply_url: str, now: datetime,
) -> JobPosting:
    source = db.get(JobSource, MANUAL_SOURCE_ID)
    if source is None:
        raise RuntimeError("manual job source is missing")
    raw = RawJobRecord(
        source_id=source.id, external_record_id=submission.id,
        payload_hash=submission.content_sha256,
        raw_fields=[{"field_name": "submission_reference", "value": submission.id}],
        source_updated_at=None, observed_at=now,
    )
    db.add(raw)
    db.flush()
    posting = JobPosting(
        source_id=source.id, external_record_id=submission.id, raw_record_id=raw.id,
        status=JobPostingStatus.PENDING_COMPLETION, company_name=company_name,
        title=title, description_text=submission.original_jd,
        locations=[], recruitment_types=[], industries=[], apply_url=apply_url,
        referral_code=None, deadline_text=None, source_updated_at=None,
        mapper_version=MANUAL_MAPPER_VERSION,
        source_candidate={
            "company_name": company_name, "title": title, "locations": [],
            "recruitment_types": [], "industries": [], "apply_url": apply_url,
            "referral_code": None, "deadline_text": None,
        },
    )
    db.add(posting)
    db.flush()
    db.add(JobSourceLink(
        job_id=posting.id, source_type=JobSourceLinkType.USER_SUBMISSION,
        source_id=None, submission_id=submission.id, source_record_ref=submission.id,
        normalized_url=submission.normalized_url,
    ))
    db.flush()
    return posting
```

- [ ] **Step 5: Wire Tencent upsert to the idempotent source-link helper**

In `backend/app/repositories/jobs.py`, import `ensure_tencent_source_link`. In every return path of `upsert_posting`, call it after the posting has an ID and before returning:

```python
from backend.app.repositories.job_submissions import ensure_tencent_source_link

# unchanged, created, and updated paths all execute this before return
ensure_tencent_source_link(db, posting=posting, source=source)
```

Do not delete or replace any `USER_SUBMISSION` link during Tencent resync.

- [ ] **Step 6: Run repository and sync tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_job_submission_repository.py tests/unit/test_job_repository.py tests/unit/test_job_sync_service.py -q
& .\.venv\Scripts\python.exe -m ruff check backend/app/repositories/job_submissions.py backend/app/repositories/jobs.py tests/unit/test_job_submission_repository.py
```

Expected: all focused tests PASS and Ruff prints `All checks passed!`.

- [ ] **Step 7: Commit Task 4**

```powershell
git add backend/app/repositories/job_submissions.py backend/app/repositories/jobs.py tests/unit/test_job_submission_repository.py tests/unit/test_job_repository.py tests/unit/test_job_sync_service.py
git commit -m "feat: persist manual job sources and candidates"
```

---

### Task 5: Implement the student lifecycle and transaction-safe administrator promotion

**Files:**
- Create: `backend/app/services/job_submissions.py`
- Create: `tests/unit/test_job_submission_service.py`
- Modify: `backend/app/repositories/job_submissions.py`

**Interfaces:**
- Consumes: Task 1 normalization/detector and Task 4 repository functions.
- Produces: `JobSubmissionService.create`, `.update`, `.submit`, `.link_existing`, `.create_pending`, `.reject`; errors `SubmissionNotFoundError`, `StaleSubmissionError`, `InvalidSubmissionTransition`, and `InvalidPromotionTarget`.

- [ ] **Step 1: Write failing student lifecycle and recovery tests**

Create `tests/unit/test_job_submission_service.py` with this complete fixture setup:

```python
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import (
    JobPosting, JobPostingStatus, JobSource, JobSourceProvider,
    RawJobRecord, User, UserRole,
)
from backend.app.repositories.job_submissions import MANUAL_SOURCE_ID
from backend.app.services.job_submissions import JobSubmissionService


@pytest.fixture
def service_db() -> Iterator[tuple[JobSubmissionService, Session, User, User]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(account="service-owner", nickname="Owner", password_hash="hash")
        admin = User(
            account="service-admin", nickname="Admin", password_hash="hash",
            role=UserRole.ADMIN,
        )
        manual_source = JobSource(
            id=MANUAL_SOURCE_ID, source_key="manual-user-submissions",
            provider=JobSourceProvider.USER_SUBMISSION, name="用户手动提交",
            file_id="manual", sheet_id="manual", mapper_version="manual-submission-v1",
            enabled=False,
        )
        db.add_all([user, admin, manual_source])
        db.flush()
        yield JobSubmissionService(), db, user, admin
    engine.dispose()


@pytest.fixture
def verified_job(
    service_db: tuple[JobSubmissionService, Session, User, User],
) -> JobPosting:
    _service, db, _user, _admin = service_db
    now = datetime.now(timezone.utc)
    source = JobSource(
        source_key="service-tencent", provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Service Tencent", file_id="service-file", sheet_id="service-sheet",
        mapper_version="service-v1", enabled=True,
    )
    db.add(source)
    db.flush()
    raw = RawJobRecord(
        source_id=source.id, external_record_id="service-record",
        payload_hash="a" * 64, raw_fields=[], observed_at=now,
    )
    db.add(raw)
    db.flush()
    posting = JobPosting(
        source_id=source.id, external_record_id=raw.external_record_id,
        raw_record_id=raw.id, status=JobPostingStatus.VERIFIED,
        company_name="示例科技", title="后端实习生",
        description_text="负责 FastAPI MySQL 后端服务开发",
        locations=["上海"], recruitment_types=["实习"], industries=["软件"],
        apply_url="https://jobs.example.com/service-role",
        mapper_version=source.mapper_version, source_candidate={}, verified_at=now,
    )
    db.add(posting)
    db.flush()
    return posting
```

Then add the lifecycle tests:

```python
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from backend.app.db.models import DeduplicationStatus, SubmissionStatus
from backend.app.services.job_submissions import (
    InvalidSubmissionTransition,
    JobSubmissionService,
    StaleSubmissionError,
)


def test_student_create_update_and_submit_are_versioned(service_db) -> None:
    service, db, user, _admin = service_db
    item = service.create(
        db, user_id=user.id, input_type="url", raw_value="https://jobs.example.com/1"
    )
    assert (item.status, item.version, item.deduplication_status) == (
        SubmissionStatus.DRAFT, 0, DeduplicationStatus.SUCCEEDED,
    )
    updated = service.update(
        db, user_id=user.id, submission_id=item.id, expected_version=0,
        input_type="jd_text", raw_value="负责 Python FastAPI MySQL 后端服务开发",
    )
    assert updated.version == 1
    submitted = service.submit(
        db, user_id=user.id, submission_id=item.id, expected_version=1
    )
    assert (submitted.status, submitted.version) == (SubmissionStatus.SUBMITTED, 2)
    with pytest.raises(InvalidSubmissionTransition):
        service.update(
            db, user_id=user.id, submission_id=item.id, expected_version=2,
            input_type="url", raw_value="https://jobs.example.com/2",
        )


def test_stale_update_does_not_mutate_submission(service_db) -> None:
    service, db, user, _admin = service_db
    item = service.create(db, user_id=user.id, input_type="url", raw_value="https://jobs.example.com/1")
    with pytest.raises(StaleSubmissionError):
        service.update(
            db, user_id=user.id, submission_id=item.id, expected_version=9,
            input_type="url", raw_value="https://jobs.example.com/2",
        )
    assert item.version == 0


def test_duplicate_detection_failure_keeps_private_submission_editable(service_db) -> None:
    service, db, user, _admin = service_db
    with patch(
        "backend.app.repositories.job_submissions.list_job_fingerprints",
        side_effect=OperationalError("select", {}, RuntimeError("database detail")),
    ):
        item = service.create(
            db, user_id=user.id, input_type="url", raw_value="https://jobs.example.com/1"
        )
    assert item.status is SubmissionStatus.DRAFT
    assert item.deduplication_status is DeduplicationStatus.FAILED
    assert item.deduplication_error_code == "duplicate_detection_failed"
    assert "database detail" not in item.deduplication_error_code
```

- [ ] **Step 2: Write failing administrator transaction tests**

Append:

```python
from sqlalchemy import func, select

from backend.app.db.models import AuditEvent, JobPostingStatus, JobSourceLink, JobVerification


def test_admin_link_existing_appends_source_and_safe_audit(service_db, verified_job) -> None:
    service, db, user, admin = service_db
    item = service.create(db, user_id=user.id, input_type="url", raw_value=verified_job.apply_url)
    service.submit(db, user_id=user.id, submission_id=item.id, expected_version=0)
    promoted = service.link_existing(
        db, submission_id=item.id, actor_user_id=admin.id,
        expected_version=1, job_id=verified_job.id,
    )
    assert promoted.status is SubmissionStatus.PROMOTED
    assert promoted.promoted_job_id == verified_job.id
    assert db.scalar(select(JobSourceLink).where(JobSourceLink.submission_id == item.id)) is not None
    event = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc())).first()
    assert event.redacted_payload == {"action": "link_existing", "job_id": verified_job.id}
    assert user.id not in str(event.redacted_payload)


def test_admin_create_pending_does_not_create_verification(service_db) -> None:
    service, db, user, admin = service_db
    item = service.create(
        db, user_id=user.id, input_type="jd_text",
        raw_value="示例科技招聘后端实习生，负责 FastAPI 与 MySQL。",
    )
    service.submit(db, user_id=user.id, submission_id=item.id, expected_version=0)
    promoted, posting = service.create_pending(
        db, submission_id=item.id, actor_user_id=admin.id, expected_version=1,
        company_name="示例科技", title="后端实习生", apply_url="",
    )
    assert posting.status is JobPostingStatus.PENDING_COMPLETION
    assert promoted.promoted_job_id == posting.id
    assert db.scalar(select(JobVerification).where(JobVerification.job_id == posting.id)) is None


def test_second_admin_decision_is_stale_and_has_no_second_side_effect(service_db, verified_job) -> None:
    service, db, user, admin = service_db
    item = service.create(db, user_id=user.id, input_type="url", raw_value=verified_job.apply_url)
    service.submit(db, user_id=user.id, submission_id=item.id, expected_version=0)
    service.link_existing(
        db, submission_id=item.id, actor_user_id=admin.id,
        expected_version=1, job_id=verified_job.id,
    )
    with pytest.raises(StaleSubmissionError):
        service.link_existing(
            db, submission_id=item.id, actor_user_id=admin.id,
            expected_version=1, job_id=verified_job.id,
        )
    assert db.scalar(select(func.count()).select_from(JobSourceLink).where(
        JobSourceLink.submission_id == item.id
    )) == 1
```

- [ ] **Step 3: Run the service tests and verify they fail**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_job_submission_service.py -q
```

Expected: collection FAIL because `JobSubmissionService` does not exist.

- [ ] **Step 4: Add the missing idempotent manual-link repository function**

Append to `backend/app/repositories/job_submissions.py`:

```python
def link_submission_to_posting(
    db: Session, *, submission: UserJobSubmission, posting: JobPosting,
) -> JobSourceLink:
    existing = db.scalar(select(JobSourceLink).where(
        JobSourceLink.job_id == posting.id,
        JobSourceLink.source_type == JobSourceLinkType.USER_SUBMISSION,
        JobSourceLink.source_record_ref == submission.id,
    ))
    if existing is not None:
        return existing
    link = JobSourceLink(
        job_id=posting.id, source_type=JobSourceLinkType.USER_SUBMISSION,
        source_id=None, submission_id=submission.id, source_record_ref=submission.id,
        normalized_url=submission.normalized_url,
    )
    db.add(link)
    db.flush()
    return link
```

- [ ] **Step 5: Implement the service and stable errors**

Create `backend/app/services/job_submissions.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.db.models import (
    AuditEvent, DeduplicationStatus, JobPosting, JobPostingStatus,
    SubmissionInputType, SubmissionStatus, UserJobSubmission,
)
from backend.app.domain.job_submissions import (
    DuplicateDetector, JobFingerprint, normalize_submission_input,
)
from backend.app.repositories import job_submissions


class SubmissionNotFoundError(LookupError):
    error_code = "job_submission_not_found"


class StaleSubmissionError(RuntimeError):
    error_code = "stale_job_submission"


class InvalidSubmissionTransition(RuntimeError):
    error_code = "invalid_job_submission_transition"


class InvalidPromotionTarget(ValueError):
    error_code = "invalid_promotion_target"


class JobSubmissionService:
    def __init__(self, detector: DuplicateDetector | None = None) -> None:
        self.detector = detector or DuplicateDetector()

    def _owned(self, db: Session, *, user_id: str, submission_id: str) -> UserJobSubmission:
        item = job_submissions.get_owned(db, user_id=user_id, submission_id=submission_id)
        if item is None:
            raise SubmissionNotFoundError(submission_id)
        return item

    @staticmethod
    def _check_version(item: UserJobSubmission, expected_version: int) -> None:
        if item.version != expected_version:
            raise StaleSubmissionError(item.id)

    def _generate_candidates(self, db: Session, item: UserJobSubmission) -> None:
        try:
            with db.begin_nested():
                postings = job_submissions.list_job_fingerprints(db)
                normalized = normalize_submission_input(
                    item.input_type, item.original_url if item.input_type is SubmissionInputType.URL else item.original_jd or ""
                )
                matches = self.detector.find_candidates(normalized, [
                    JobFingerprint(row.id, row.apply_url, row.description_text) for row in postings
                ])
                job_submissions.add_candidates(db, submission=item, matches=[
                    job_submissions.PersistedMatch(
                        match.job_id, match.score_basis_points, list(match.reasons),
                        match.score_components, match.algorithm_version,
                    ) for match in matches
                ])
        except SQLAlchemyError:
            item.deduplication_status = DeduplicationStatus.FAILED
            item.deduplication_error_code = "duplicate_detection_failed"
        else:
            item.deduplication_status = DeduplicationStatus.SUCCEEDED
            item.deduplication_error_code = None
        db.flush()

    def create(
        self, db: Session, *, user_id: str, input_type: str, raw_value: str,
    ) -> UserJobSubmission:
        normalized = normalize_submission_input(SubmissionInputType(input_type), raw_value)
        item = UserJobSubmission(
            user_id=user_id, input_type=normalized.input_type,
            original_url=normalized.original_url, original_jd=normalized.original_jd,
            input_preview=normalized.preview, normalized_url=normalized.normalized_url,
            content_sha256=normalized.content_sha256, status=SubmissionStatus.DRAFT,
            version=0, deduplication_status=DeduplicationStatus.PENDING,
        )
        db.add(item)
        db.flush()
        self._generate_candidates(db, item)
        return item

    def update(
        self, db: Session, *, user_id: str, submission_id: str,
        expected_version: int, input_type: str, raw_value: str,
    ) -> UserJobSubmission:
        item = self._owned(db, user_id=user_id, submission_id=submission_id)
        self._check_version(item, expected_version)
        if item.status is not SubmissionStatus.DRAFT:
            raise InvalidSubmissionTransition(item.status.value)
        normalized = normalize_submission_input(SubmissionInputType(input_type), raw_value)
        item.input_type = normalized.input_type
        item.original_url = normalized.original_url
        item.original_jd = normalized.original_jd
        item.input_preview = normalized.preview
        item.normalized_url = normalized.normalized_url
        item.content_sha256 = normalized.content_sha256
        item.version += 1
        item.deduplication_status = DeduplicationStatus.PENDING
        item.deduplication_error_code = None
        db.flush()
        self._generate_candidates(db, item)
        return item

    def submit(
        self, db: Session, *, user_id: str, submission_id: str, expected_version: int,
    ) -> UserJobSubmission:
        item = self._owned(db, user_id=user_id, submission_id=submission_id)
        self._check_version(item, expected_version)
        if item.status is not SubmissionStatus.DRAFT:
            raise InvalidSubmissionTransition(item.status.value)
        item.status = SubmissionStatus.SUBMITTED
        item.version += 1
        db.flush()
        return item

    def _lock_submitted(
        self, db: Session, *, submission_id: str, expected_version: int,
    ) -> UserJobSubmission:
        item = job_submissions.get_for_admin(db, submission_id=submission_id, lock=True)
        if item is None:
            raise SubmissionNotFoundError(submission_id)
        self._check_version(item, expected_version)
        if item.status is not SubmissionStatus.SUBMITTED:
            raise InvalidSubmissionTransition(item.status.value)
        return item

    @staticmethod
    def _audit(
        db: Session, *, item: UserJobSubmission, actor_user_id: str,
        action: str, job_id: str | None,
    ) -> None:
        payload = {"action": action}
        if job_id is not None:
            payload["job_id"] = job_id
        db.add(AuditEvent(
            actor_user_id=actor_user_id, actor_device_id=None,
            event_type=f"job_submission.{action}", entity_type="user_job_submission",
            entity_id=item.id, correlation_id=str(uuid.uuid4()), redacted_payload=payload,
        ))

    def link_existing(
        self, db: Session, *, submission_id: str, actor_user_id: str,
        expected_version: int, job_id: str,
    ) -> UserJobSubmission:
        item = self._lock_submitted(db, submission_id=submission_id, expected_version=expected_version)
        posting = db.scalar(select(JobPosting).where(JobPosting.id == job_id).with_for_update())
        if posting is None or posting.status in {JobPostingStatus.REJECTED, JobPostingStatus.EXPIRED}:
            raise InvalidPromotionTarget(job_id)
        job_submissions.link_submission_to_posting(db, submission=item, posting=posting)
        item.status = SubmissionStatus.PROMOTED
        item.promoted_job_id = posting.id
        item.version += 1
        self._audit(db, item=item, actor_user_id=actor_user_id, action="link_existing", job_id=posting.id)
        db.flush()
        return item

    def create_pending(
        self, db: Session, *, submission_id: str, actor_user_id: str,
        expected_version: int, company_name: str, title: str, apply_url: str,
    ) -> tuple[UserJobSubmission, JobPosting]:
        item = self._lock_submitted(db, submission_id=submission_id, expected_version=expected_version)
        if apply_url:
            normalize_submission_input(SubmissionInputType.URL, apply_url)
        posting = job_submissions.create_manual_pending_posting(
            db, submission=item, company_name=company_name.strip(), title=title.strip(),
            apply_url=apply_url.strip(), now=datetime.now(timezone.utc),
        )
        item.status = SubmissionStatus.PROMOTED
        item.promoted_job_id = posting.id
        item.version += 1
        self._audit(db, item=item, actor_user_id=actor_user_id, action="create_pending", job_id=posting.id)
        db.flush()
        return item, posting

    def reject(
        self, db: Session, *, submission_id: str, actor_user_id: str,
        expected_version: int, reason_code: str,
    ) -> UserJobSubmission:
        item = self._lock_submitted(db, submission_id=submission_id, expected_version=expected_version)
        allowed = {"not_a_job", "insufficient_evidence", "unsafe_link", "duplicate_submission"}
        if reason_code not in allowed:
            raise InvalidPromotionTarget(reason_code)
        item.status = SubmissionStatus.REJECTED
        item.rejected_reason_code = reason_code
        item.version += 1
        self._audit(db, item=item, actor_user_id=actor_user_id, action="reject", job_id=None)
        db.flush()
        return item
```

The route owns `commit/rollback`; the service deliberately never commits, so row lock, posting/source-link creation, submission transition, and audit event remain one transaction.

- [ ] **Step 6: Run lifecycle tests and focused existing review regression**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_job_submission_service.py tests/unit/test_job_review_service.py -q
& .\.venv\Scripts\python.exe -m ruff check backend/app/services/job_submissions.py tests/unit/test_job_submission_service.py
```

Expected: all tests PASS; the existing review service remains green; Ruff prints `All checks passed!`.

- [ ] **Step 7: Commit Task 5**

```powershell
git add backend/app/repositories/job_submissions.py backend/app/services/job_submissions.py tests/unit/test_job_submission_service.py
git commit -m "feat: add manual job submission lifecycle"
```

---

### Task 6: Expose private student and administrator APIs without identity leakage

**Files:**
- Create: `backend/app/api/routes/job_submissions.py`
- Create: `tests/contract/test_job_submissions_api.py`
- Modify: `tests/security/test_no_sensitive_logging.py`

**Interfaces:**
- Consumes: Task 2 schemas and Task 5 service.
- Produces: `/api/job-submissions...` and `/api/admin/job-submissions...`; shared router mounting remains Task 9.

- [ ] **Step 1: Write failing student ownership and whitelist contract tests**

Create `tests/contract/test_job_submissions_api.py` with these exact app and identity fixtures before the tests:

```python
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api import dependencies
from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    DeduplicationStatus, JobDuplicateCandidate, JobPosting, JobPostingStatus,
    JobSource, JobSourceProvider, RawJobRecord, SubmissionInputType,
    SubmissionStatus, User, UserJobSubmission, UserRole,
)
from backend.app.main import create_app
from backend.app.services.auth import AuthService


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    redis = fakeredis.FakeRedis()

    def override_db() -> Iterator[Session]:
        with factory() as db:
            yield db

    app = create_app(settings, session_factory=factory)
    app.state.redis = redis
    app.dependency_overrides[dependencies._get_db] = override_db
    app.dependency_overrides[dependencies.get_redis] = lambda: redis
    with TestClient(app) as test_client:
        test_client.session_factory = factory  # type: ignore[attr-defined]
        yield test_client
    engine.dispose()


@pytest.fixture
def submission_seed(client: TestClient) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with client.session_factory() as db:  # type: ignore[attr-defined]
        owner = User(account="submission-owner", nickname="Owner", password_hash="hash")
        other = User(account="submission-other", nickname="Other", password_hash="hash")
        admin = User(
            account="submission-admin", nickname="Admin", password_hash="hash",
            role=UserRole.ADMIN,
        )
        source = JobSource(
            source_key="submission-contract-source",
            provider=JobSourceProvider.TENCENT_SMARTSHEET,
            name="Contract Source", file_id="contract-file", sheet_id="contract-sheet",
            mapper_version="contract-v1", enabled=True,
        )
        db.add_all([owner, other, admin, source])
        db.flush()
        postings: list[JobPosting] = []
        for index, job_status in enumerate(
            [JobPostingStatus.VERIFIED, JobPostingStatus.PENDING_COMPLETION], start=1
        ):
            raw = RawJobRecord(
                source_id=source.id, external_record_id=f"contract-{index}",
                payload_hash=str(index) * 64, raw_fields=[], observed_at=now,
            )
            db.add(raw)
            db.flush()
            posting = JobPosting(
                source_id=source.id, external_record_id=f"contract-{index}", raw_record_id=raw.id,
                status=job_status, company_name="示例科技", title=f"后端实习生 {index}",
                description_text="负责 FastAPI 与 MySQL 后端服务开发",
                locations=["上海"], recruitment_types=["实习"], industries=["软件"],
                apply_url=f"https://jobs.example.com/{index}", mapper_version=source.mapper_version,
                source_candidate={},
            )
            db.add(posting)
            postings.append(posting)
        db.flush()
        item = UserJobSubmission(
            user_id=owner.id, input_type=SubmissionInputType.JD_TEXT,
            original_url=None, original_jd="PRIVATE CONTRACT JD",
            input_preview="示例科技招聘后端实习生", normalized_url=None,
            content_sha256="a" * 64, status=SubmissionStatus.SUBMITTED, version=2,
            deduplication_status=DeduplicationStatus.SUCCEEDED,
        )
        db.add(item)
        db.flush()
        db.add_all([JobDuplicateCandidate(
            submission_id=item.id, candidate_job_id=posting.id,
            generated_for_version=item.version, score_basis_points=9000 - index,
            reasons=["jd_token_overlap"], score_components={"jd_token_jaccard": 9000 - index},
            algorithm_version="manual-job-dedup-v1",
        ) for index, posting in enumerate(postings)])
        db.commit()
        auth = AuthService(client.app.state.settings)
        return {
            "owner_headers": {"Authorization": f"Bearer {auth.issue_user_token(owner)}"},
            "other_headers": {"Authorization": f"Bearer {auth.issue_user_token(other)}"},
            "admin_headers": {"Authorization": f"Bearer {auth.issue_user_token(admin)}"},
            "owner_account": owner.account, "submission_id": item.id,
            "version": item.version, "verified_job_id": postings[0].id,
        }
```

Then add the student contract tests:

```python
def test_student_create_list_and_cross_user_404(client, submission_seed) -> None:
    owner_headers = submission_seed["owner_headers"]
    other_headers = submission_seed["other_headers"]
    secret_jd = "PRIVATE-JD-DO-NOT-RETURN " + "负责 FastAPI 与 MySQL。" * 30
    created = client.post(
        "/api/job-submissions",
        headers=owner_headers,
        json={"input_type": "jd_text", "jd_text": secret_jd},
    )
    assert created.status_code == 201
    body = created.json()
    assert set(body) == {
        "id", "input_type", "input_preview", "normalized_url", "status", "version",
        "deduplication_status", "deduplication_error_code", "promoted_job_id",
        "created_at", "updated_at",
    }
    assert secret_jd not in created.text
    assert "user_id" not in created.text
    assert client.get(f"/api/job-submissions/{body['id']}", headers=other_headers).status_code == 404
    listed = client.get("/api/job-submissions", headers=owner_headers).json()
    assert body["id"] in {item["id"] for item in listed["submissions"]}


def test_student_duplicate_candidates_only_expose_verified_jobs(client, submission_seed) -> None:
    response = client.get(
        f"/api/job-submissions/{submission_seed['submission_id']}/duplicate-candidates",
        headers=submission_seed["owner_headers"],
    )
    assert response.status_code == 200
    assert {item["job"]["status"] for item in response.json()["candidates"]} <= {"verified"}
    assert "submission_id" not in response.text
```

- [ ] **Step 2: Write failing error and administrator contract tests**

Append:

```python
def test_stale_update_returns_stable_409_without_input(client, submission_seed) -> None:
    response = client.patch(
        f"/api/job-submissions/{submission_seed['submission_id']}",
        headers=submission_seed["owner_headers"],
        json={
            "expected_version": 99, "input_type": "url",
            "url": "https://jobs.example.com/new",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "stale_job_submission", "message": "提交版本已过期，请重新加载。"
    }


def test_admin_queue_and_decision_never_return_submitter_identity(client, submission_seed) -> None:
    response = client.get(
        "/api/admin/job-submissions?status=submitted&limit=20&offset=0",
        headers=submission_seed["admin_headers"],
    )
    assert response.status_code == 200
    assert "user_id" not in response.text
    assert submission_seed["owner_account"] not in response.text
    decision = client.post(
        f"/api/admin/job-submissions/{submission_seed['submission_id']}/decision",
        headers=submission_seed["admin_headers"],
        json={
            "expected_version": submission_seed["version"],
            "action": "link_existing", "job_id": submission_seed["verified_job_id"],
        },
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "promoted"


def test_student_cannot_use_admin_submission_queue(client, submission_seed) -> None:
    response = client.get(
        "/api/admin/job-submissions?status=submitted",
        headers=submission_seed["owner_headers"],
    )
    assert response.status_code == 403
```

- [ ] **Step 3: Run contract tests and verify they fail**

Temporarily include `job_submissions.router` directly in the test app fixture so this task does not edit the shared production router:

```python
from backend.app.api.routes import job_submissions

app.include_router(job_submissions.router, prefix="/api")
```

Then run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/contract/test_job_submissions_api.py -q
```

Expected: FAIL because the route module does not exist.

- [ ] **Step 4: Implement DTO conversion and stable error mapping**

Create `backend/app/api/routes/job_submissions.py` with these helpers:

```python
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, get_current_user, require_admin
from backend.app.api.job_submission_schemas import (
    AdminJobSubmissionDecisionRequest, AdminJobSubmissionListResponse,
    AdminJobSubmissionResponse, DuplicateCandidateListResponse,
    DuplicateCandidateResponse, DuplicateJobSummary, JobSubmissionCreateRequest,
    JobSubmissionListResponse, JobSubmissionResponse, JobSubmissionSubmitRequest,
    JobSubmissionUpdateRequest,
)
from backend.app.db.models import SubmissionStatus, User, UserJobSubmission
from backend.app.domain.job_submissions import InvalidSubmissionInput
from backend.app.repositories import job_submissions
from backend.app.services.job_submissions import (
    InvalidPromotionTarget, InvalidSubmissionTransition, JobSubmissionService,
    StaleSubmissionError, SubmissionNotFoundError,
)


router = APIRouter(tags=["job-submissions"])


def _response(item: UserJobSubmission) -> JobSubmissionResponse:
    return JobSubmissionResponse(
        id=item.id, input_type=item.input_type, input_preview=item.input_preview,
        normalized_url=item.normalized_url, status=item.status, version=item.version,
        deduplication_status=item.deduplication_status,
        deduplication_error_code=item.deduplication_error_code,
        promoted_job_id=item.promoted_job_id,
        created_at=item.created_at, updated_at=item.updated_at,
    )


def _admin_response(item: UserJobSubmission) -> AdminJobSubmissionResponse:
    return AdminJobSubmissionResponse(**_response(item).model_dump(), content_sha256=item.content_sha256)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, SubmissionNotFoundError):
        return HTTPException(404, detail={"code": exc.error_code, "message": "职位提交不存在。"})
    if isinstance(exc, StaleSubmissionError):
        return HTTPException(409, detail={"code": exc.error_code, "message": "提交版本已过期，请重新加载。"})
    if isinstance(exc, InvalidSubmissionTransition):
        return HTTPException(409, detail={"code": exc.error_code, "message": "当前提交状态不允许此操作。"})
    if isinstance(exc, (InvalidSubmissionInput, InvalidPromotionTarget)):
        return HTTPException(422, detail={"code": exc.error_code, "message": "职位输入或提升目标不合法。"})
    raise exc


def _raw_value(body: JobSubmissionCreateRequest) -> str:
    return body.url if body.input_type.value == "url" else body.jd_text or ""
```

- [ ] **Step 5: Implement the student routes with ownership from the bearer token**

Append to the same route file:

```python
@router.post("/job-submissions", response_model=JobSubmissionResponse, status_code=status.HTTP_201_CREATED)
def create_submission(
    body: JobSubmissionCreateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> JobSubmissionResponse:
    try:
        item = JobSubmissionService().create(
            db, user_id=current_user.id, input_type=body.input_type.value, raw_value=_raw_value(body)
        )
        response = _response(item)
        db.commit()
        return response
    except (InvalidSubmissionInput,) as exc:
        db.rollback()
        raise _error(exc) from None


@router.get("/job-submissions", response_model=JobSubmissionListResponse)
def list_submissions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobSubmissionListResponse:
    total, items = job_submissions.list_owned(db, user_id=current_user.id, limit=limit, offset=offset)
    return JobSubmissionListResponse(total=total, submissions=[_response(item) for item in items])


@router.get("/job-submissions/{submission_id}", response_model=JobSubmissionResponse)
def get_submission(
    submission_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> JobSubmissionResponse:
    item = job_submissions.get_owned(db, user_id=current_user.id, submission_id=submission_id)
    if item is None:
        raise _error(SubmissionNotFoundError(submission_id))
    return _response(item)


@router.patch("/job-submissions/{submission_id}", response_model=JobSubmissionResponse)
def update_submission(
    submission_id: str, body: JobSubmissionUpdateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> JobSubmissionResponse:
    try:
        item = JobSubmissionService().update(
            db, user_id=current_user.id, submission_id=submission_id,
            expected_version=body.expected_version, input_type=body.input_type.value,
            raw_value=_raw_value(body),
        )
        response = _response(item)
        db.commit()
        return response
    except (SubmissionNotFoundError, StaleSubmissionError, InvalidSubmissionTransition, InvalidSubmissionInput) as exc:
        db.rollback()
        raise _error(exc) from None


@router.post("/job-submissions/{submission_id}/submit", response_model=JobSubmissionResponse)
def submit_submission(
    submission_id: str, body: JobSubmissionSubmitRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> JobSubmissionResponse:
    try:
        item = JobSubmissionService().submit(
            db, user_id=current_user.id, submission_id=submission_id,
            expected_version=body.expected_version,
        )
        response = _response(item)
        db.commit()
        return response
    except (SubmissionNotFoundError, StaleSubmissionError, InvalidSubmissionTransition) as exc:
        db.rollback()
        raise _error(exc) from None


@router.get(
    "/job-submissions/{submission_id}/duplicate-candidates",
    response_model=DuplicateCandidateListResponse,
)
def duplicate_candidates(
    submission_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> DuplicateCandidateListResponse:
    item = job_submissions.get_owned(db, user_id=current_user.id, submission_id=submission_id)
    if item is None:
        raise _error(SubmissionNotFoundError(submission_id))
    rows = job_submissions.list_candidates(db, submission=item, public_only=True)
    return DuplicateCandidateListResponse(candidates=[DuplicateCandidateResponse(
        job=DuplicateJobSummary(
            id=posting.id, company_name=posting.company_name, title=posting.title,
            status=posting.status.value, apply_url=posting.apply_url,
        ),
        score_basis_points=candidate.score_basis_points,
        reasons=candidate.reasons, score_components=candidate.score_components,
        algorithm_version=candidate.algorithm_version,
    ) for candidate, posting in rows])
```

- [ ] **Step 6: Implement administrator queue and decision routes**

Append:

```python
@router.get("/admin/job-submissions", response_model=AdminJobSubmissionListResponse)
def admin_submission_queue(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    submission_status: Annotated[SubmissionStatus, Query(alias="status")] = SubmissionStatus.SUBMITTED,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminJobSubmissionListResponse:
    del admin
    total, items = job_submissions.list_for_admin(
        db, status=submission_status, limit=limit, offset=offset
    )
    return AdminJobSubmissionListResponse(
        total=total, submissions=[_admin_response(item) for item in items]
    )


@router.post(
    "/admin/job-submissions/{submission_id}/decision",
    response_model=AdminJobSubmissionResponse,
)
def decide_submission(
    submission_id: str, body: AdminJobSubmissionDecisionRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> AdminJobSubmissionResponse:
    service = JobSubmissionService()
    try:
        if body.action == "link_existing":
            assert body.job_id is not None
            item = service.link_existing(
                db, submission_id=submission_id, actor_user_id=admin.id,
                expected_version=body.expected_version, job_id=body.job_id,
            )
        elif body.action == "create_pending":
            assert body.company_name is not None and body.title is not None
            item, _posting = service.create_pending(
                db, submission_id=submission_id, actor_user_id=admin.id,
                expected_version=body.expected_version, company_name=body.company_name,
                title=body.title, apply_url=body.apply_url or "",
            )
        else:
            assert body.reason_code is not None
            item = service.reject(
                db, submission_id=submission_id, actor_user_id=admin.id,
                expected_version=body.expected_version, reason_code=body.reason_code,
            )
        response = _admin_response(item)
        db.commit()
        return response
    except (
        SubmissionNotFoundError, StaleSubmissionError, InvalidSubmissionTransition,
        InvalidSubmissionInput, InvalidPromotionTarget,
    ) as exc:
        db.rollback()
        raise _error(exc) from None
```

- [ ] **Step 7: Add the sensitive logging regression**

Append to `tests/security/test_no_sensitive_logging.py`:

```python
def test_manual_job_failures_do_not_log_original_jd_or_sensitive_url(client, caplog) -> None:
    secret_marker = "SECRET-PRIVATE-JD-7788"
    secret_jd = ("公开职位描述" * 50) + secret_marker
    unsafe_url = "https://user:token-9988@jobs.example.com/opening"
    first = client.post(
        "/api/job-submissions", headers=student_headers(client),
        json={"input_type": "jd_text", "jd_text": secret_jd},
    )
    second = client.post(
        "/api/job-submissions", headers=student_headers(client),
        json={"input_type": "url", "url": unsafe_url},
    )
    combined = caplog.text + first.text + second.text
    assert secret_marker not in combined
    assert "token-9988" not in combined
    assert "unsafe_job_url" in second.text
```

- [ ] **Step 8: Run contract and security tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/contract/test_job_submissions_api.py tests/security/test_no_sensitive_logging.py -q
& .\.venv\Scripts\python.exe -m ruff check backend/app/api/job_submission_schemas.py backend/app/api/routes/job_submissions.py tests/contract/test_job_submissions_api.py
```

Expected: contract and security tests PASS; Ruff prints `All checks passed!`.

- [ ] **Step 9: Commit Task 6**

```powershell
git add backend/app/api/routes/job_submissions.py tests/contract/test_job_submissions_api.py tests/security/test_no_sensitive_logging.py
git commit -m "feat: expose private manual job workflow"
```

---

### Task 7: Build the student private-submission feature against fixtures, then the real API

**Files:**
- Create: `frontend/src/features/job-submissions/JobSubmissions.vue`
- Create: `frontend/src/features/job-submissions/__tests__/JobSubmissions.spec.ts`

**Interfaces:**
- Consumes: Task 2 TypeScript DTO/client; Task 6 HTTP behavior at final integration.
- Produces: `<JobSubmissions :token="token" @dirty-change="..." />` with create, replace-draft, candidate, and submit actions.

- [ ] **Step 1: Write the failing student component test**

Create `frontend/src/features/job-submissions/__tests__/JobSubmissions.spec.ts`:

```typescript
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import JobSubmissions from "../JobSubmissions.vue";

const api = vi.hoisted(() => ({
  createJobSubmission: vi.fn(),
  fetchDuplicateCandidates: vi.fn(),
  fetchJobSubmissions: vi.fn(),
  submitJobSubmission: vi.fn(),
  updateJobSubmission: vi.fn(),
}));
vi.mock("../jobSubmissionsApi", () => api);

const draft = {
  id: "submission-1", input_type: "url", input_preview: "https://jobs.example/1",
  normalized_url: "https://jobs.example/1", status: "draft", version: 0,
  deduplication_status: "succeeded", deduplication_error_code: null,
  promoted_job_id: null, created_at: "2026-07-17T00:00:00Z",
  updated_at: "2026-07-17T00:00:00Z",
};

describe("JobSubmissions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchJobSubmissions.mockResolvedValue({ total: 1, submissions: [draft] });
    api.fetchDuplicateCandidates.mockResolvedValue({ candidates: [{
      job: { id: "job-1", company_name: "示例科技", title: "后端实习生", status: "verified", apply_url: "https://jobs.example/1" },
      score_basis_points: 10000, reasons: ["canonical_apply_url_exact"],
      score_components: { canonical_url: 10000 }, algorithm_version: "manual-job-dedup-v1",
    }] });
    api.submitJobSubmission.mockResolvedValue({ ...draft, status: "submitted", version: 1 });
  });

  it("creates a private URL submission and clears the raw form", async () => {
    api.createJobSubmission.mockResolvedValue(draft);
    const wrapper = mount(JobSubmissions, { props: { token: "student-token" } });
    await flushPromises();
    await wrapper.get('[data-test="input-mode-url"]').setValue(true);
    await wrapper.get('[data-test="submission-value"]').setValue("https://jobs.example/1");
    await wrapper.get('[data-test="create-submission"]').trigger("click");
    await flushPromises();
    expect(api.createJobSubmission).toHaveBeenCalledWith("student-token", {
      input_type: "url", url: "https://jobs.example/1",
    });
    expect((wrapper.get('[data-test="submission-value"]').element as HTMLTextAreaElement).value).toBe("");
  });

  it("shows explanations and never labels a candidate as merged", async () => {
    const wrapper = mount(JobSubmissions, { props: { token: "student-token" } });
    await flushPromises();
    await wrapper.get('[data-test="show-candidates-submission-1"]').trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("canonical_apply_url_exact");
    expect(wrapper.text()).toContain("候选，不会自动合并");
    expect(wrapper.text()).not.toContain("已自动合并");
  });

  it("submits the current version and emits dirty state for unsaved input", async () => {
    const wrapper = mount(JobSubmissions, { props: { token: "student-token" } });
    await flushPromises();
    await wrapper.get('[data-test="submission-value"]').setValue("未保存 JD");
    expect(wrapper.emitted("dirty-change")?.at(-1)).toEqual([true]);
    await wrapper.get('[data-test="submit-submission-1"]').trigger("click");
    await flushPromises();
    expect(api.submitJobSubmission).toHaveBeenCalledWith("student-token", "submission-1", 0);
  });
});
```

- [ ] **Step 2: Run the component test and verify it fails**

Run:

```powershell
npm.cmd --prefix frontend run test -- JobSubmissions.spec.ts
```

Expected: FAIL because `JobSubmissions.vue` does not exist.

- [ ] **Step 3: Implement the student component**

Create `frontend/src/features/job-submissions/JobSubmissions.vue`:

```vue
<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";

import {
  createJobSubmission, fetchDuplicateCandidates, fetchJobSubmissions,
  submitJobSubmission, updateJobSubmission,
} from "./jobSubmissionsApi";
import type { DuplicateCandidate, JobSubmission, SubmissionInputType } from "./jobSubmissionTypes";

const props = defineProps<{ token: string }>();
const emit = defineEmits<{ (event: "dirty-change", dirty: boolean): void }>();
const submissions = ref<JobSubmission[]>([]);
const candidates = ref<Record<string, DuplicateCandidate[]>>({});
const inputMode = ref<SubmissionInputType>("url");
const rawValue = ref("");
const editing = ref<JobSubmission | null>(null);
const loading = ref(false);
const message = ref("");
const dirty = computed(() => rawValue.value.trim().length > 0);
watch(dirty, (value) => emit("dirty-change", value), { immediate: true });

async function load() {
  const response = await fetchJobSubmissions(props.token);
  submissions.value = response.submissions;
}

async function save() {
  const value = rawValue.value.trim();
  if (!value) return;
  loading.value = true;
  try {
    const payload = inputMode.value === "url"
      ? { input_type: "url" as const, url: value }
      : { input_type: "jd_text" as const, jd_text: value };
    if (editing.value) {
      await updateJobSubmission(props.token, editing.value.id, editing.value.version, payload);
    } else {
      await createJobSubmission(props.token, payload);
    }
    rawValue.value = "";
    editing.value = null;
    message.value = "私有提交已保存。";
    await load();
  } finally {
    loading.value = false;
  }
}

function replaceDraft(item: JobSubmission) {
  editing.value = item;
  inputMode.value = item.input_type;
  rawValue.value = "";
  message.value = "请输入完整的新内容替换草稿；服务端不会把完整原文回传到列表。";
}

async function showCandidates(item: JobSubmission) {
  candidates.value[item.id] = (await fetchDuplicateCandidates(props.token, item.id)).candidates;
}

async function submit(item: JobSubmission) {
  await submitJobSubmission(props.token, item.id, item.version);
  message.value = "已送交管理员处理。";
  await load();
}

onMounted(load);
</script>

<template>
  <section class="submission-workspace">
    <header><h2>手动职位</h2><p>提交默认仅本人可见；重复结果只是候选，不会自动合并。</p></header>
    <div class="submission-form">
      <label><input v-model="inputMode" data-test="input-mode-url" type="radio" value="url"> 职位链接</label>
      <label><input v-model="inputMode" type="radio" value="jd_text"> JD 文本</label>
      <textarea
        v-model="rawValue" data-test="submission-value"
        :maxlength="inputMode === 'url' ? 4096 : 100000"
        :placeholder="inputMode === 'url' ? 'https://careers.example.com/job/1' : '粘贴完整 JD 文本'"
      />
      <button data-test="create-submission" :disabled="loading || !rawValue.trim()" @click="save">
        {{ editing ? "替换草稿" : "保存私有提交" }}
      </button>
      <p role="status">{{ message }}</p>
    </div>
    <article v-for="item in submissions" :key="item.id" class="submission-card">
      <strong>{{ item.input_preview }}</strong>
      <span>{{ item.status }} · v{{ item.version }} · 去重 {{ item.deduplication_status }}</span>
      <button v-if="item.status === 'draft'" @click="replaceDraft(item)">替换内容</button>
      <button :data-test="`show-candidates-${item.id}`" @click="showCandidates(item)">查看重复候选</button>
      <button
        v-if="item.status === 'draft'" :data-test="`submit-${item.id}`"
        @click="submit(item)"
      >送交审核</button>
      <ul v-if="candidates[item.id]">
        <li v-for="candidate in candidates[item.id]" :key="candidate.job.id">
          {{ candidate.job.company_name }} · {{ candidate.job.title }} ·
          {{ candidate.score_basis_points / 100 }}% · {{ candidate.reasons.join("、") }}
          <em>候选，不会自动合并</em>
        </li>
      </ul>
    </article>
  </section>
</template>

<style scoped>
.submission-workspace { display: grid; gap: 1rem; }
.submission-form, .submission-card { display: grid; gap: .75rem; padding: 1rem; border: 1px solid #dbe3ea; border-radius: 16px; background: #fff; }
textarea { min-height: 9rem; padding: .8rem; border: 1px solid #cbd5e1; border-radius: 12px; }
button { width: fit-content; padding: .65rem 1rem; border: 0; border-radius: 10px; background: #17634e; color: #fff; }
em { color: #8a5a00; }
</style>
```

- [ ] **Step 4: Run the student component test and typecheck**

Run:

```powershell
npm.cmd --prefix frontend run test -- JobSubmissions.spec.ts
npm.cmd --prefix frontend run typecheck
```

Expected: component tests PASS and `vue-tsc` exits 0.

- [ ] **Step 5: Commit Task 7**

```powershell
git add frontend/src/features/job-submissions/JobSubmissions.vue frontend/src/features/job-submissions/__tests__/JobSubmissions.spec.ts
git commit -m "feat: add private job submission workspace"
```

---

### Task 8: Build the administrator promotion queue with explicit actions

**Files:**
- Create: `frontend/src/features/job-submissions/AdminJobSubmissions.vue`
- Create: `frontend/src/features/job-submissions/__tests__/AdminJobSubmissions.spec.ts`
- Modify: `frontend/src/features/job-submissions/jobSubmissionsApi.ts`
- Modify: `backend/app/api/routes/job_submissions.py`
- Modify: `tests/contract/test_job_submissions_api.py`

**Interfaces:**
- Consumes: Task 6 admin queue/decision and Task 4 `list_candidates(public_only=False)`.
- Produces: administrator-only queue, all-status duplicate evidence, explicit `link_existing|create_pending|reject` forms, and `dirty-change`.

- [ ] **Step 1: Add the failing administrator-candidate API contract**

Append to `tests/contract/test_job_submissions_api.py`:

```python
def test_admin_candidates_can_include_non_public_jobs_without_submitter_identity(client, submission_seed) -> None:
    response = client.get(
        f"/api/admin/job-submissions/{submission_seed['submission_id']}/duplicate-candidates",
        headers=submission_seed["admin_headers"],
    )
    assert response.status_code == 200
    assert {item["job"]["status"] for item in response.json()["candidates"]} == {
        "verified", "pending_completion"
    }
    assert "user_id" not in response.text
    assert submission_seed["owner_account"] not in response.text
```

- [ ] **Step 2: Add the administrator candidate route**

Append this complete administrator candidate handler to `backend/app/api/routes/job_submissions.py`:

```python
@router.get(
    "/admin/job-submissions/{submission_id}/duplicate-candidates",
    response_model=DuplicateCandidateListResponse,
)
def admin_duplicate_candidates(
    submission_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> DuplicateCandidateListResponse:
    del admin
    item = job_submissions.get_for_admin(db, submission_id=submission_id)
    if item is None:
        raise _error(SubmissionNotFoundError(submission_id))
    rows = job_submissions.list_candidates(db, submission=item, public_only=False)
    return DuplicateCandidateListResponse(candidates=[DuplicateCandidateResponse(
        job=DuplicateJobSummary(
            id=posting.id, company_name=posting.company_name, title=posting.title,
            status=posting.status.value, apply_url=posting.apply_url,
        ),
        score_basis_points=candidate.score_basis_points,
        reasons=candidate.reasons, score_components=candidate.score_components,
        algorithm_version=candidate.algorithm_version,
    ) for candidate, posting in rows])
```

Add to `jobSubmissionsApi.ts`:

```typescript
export const fetchAdminDuplicateCandidates = (token: string, id: string) =>
  request<{ candidates: DuplicateCandidate[] }>(
    `/admin/job-submissions/${encodeURIComponent(id)}/duplicate-candidates`, {}, token,
  );
```

- [ ] **Step 3: Write the failing administrator component test**

Create `frontend/src/features/job-submissions/__tests__/AdminJobSubmissions.spec.ts`:

```typescript
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AdminJobSubmissions from "../AdminJobSubmissions.vue";

const api = vi.hoisted(() => ({
  decideJobSubmission: vi.fn(),
  fetchAdminDuplicateCandidates: vi.fn(),
  fetchAdminJobSubmissions: vi.fn(),
}));
vi.mock("../jobSubmissionsApi", () => api);

const submitted = {
  id: "submission-1", input_type: "jd_text", input_preview: "示例科技招聘后端实习生",
  normalized_url: null, status: "submitted", version: 2,
  deduplication_status: "succeeded", deduplication_error_code: null,
  promoted_job_id: null, content_sha256: "a".repeat(64),
  created_at: "2026-07-17T00:00:00Z", updated_at: "2026-07-17T00:00:00Z",
};

describe("AdminJobSubmissions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchAdminJobSubmissions.mockResolvedValue({ total: 1, submissions: [submitted] });
    api.fetchAdminDuplicateCandidates.mockResolvedValue({ candidates: [{
      job: { id: "job-1", company_name: "示例科技", title: "后端实习生", status: "verified", apply_url: "https://jobs.example/1" },
      score_basis_points: 9300, reasons: ["jd_token_overlap"],
      score_components: { jd_token_jaccard: 9300 }, algorithm_version: "manual-job-dedup-v1",
    }] });
    api.decideJobSubmission.mockResolvedValue({ ...submitted, status: "promoted", version: 3 });
  });

  it("links only after an explicit administrator action", async () => {
    const wrapper = mount(AdminJobSubmissions, { props: { token: "admin-token" } });
    await flushPromises();
    await wrapper.get('[data-test="select-submission-1"]').trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("jd_token_overlap");
    await wrapper.get('[data-test="candidate-job-1"]').trigger("click");
    await wrapper.get('[data-test="decide-submission"]').trigger("click");
    await flushPromises();
    expect(api.decideJobSubmission).toHaveBeenCalledWith("admin-token", "submission-1", {
      expected_version: 2, action: "link_existing", job_id: "job-1",
    });
  });

  it("creates pending completion rather than verified", async () => {
    const wrapper = mount(AdminJobSubmissions, { props: { token: "admin-token" } });
    await flushPromises();
    await wrapper.get('[data-test="select-submission-1"]').trigger("click");
    await wrapper.get('[data-test="action-create-pending"]').setValue(true);
    await wrapper.get('[data-test="company-name"]').setValue("示例科技");
    await wrapper.get('[data-test="job-title"]').setValue("后端实习生");
    await wrapper.get('[data-test="decide-submission"]').trigger("click");
    await flushPromises();
    expect(api.decideJobSubmission.mock.calls[0][2].action).toBe("create_pending");
    expect(wrapper.text()).toContain("创建后进入 pending_completion");
  });
});
```

- [ ] **Step 4: Run the administrator tests and verify they fail**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/contract/test_job_submissions_api.py -q
npm.cmd --prefix frontend run test -- AdminJobSubmissions.spec.ts
```

Expected: the new API contract passes after Step 2; the frontend test FAILS because `AdminJobSubmissions.vue` does not exist.

- [ ] **Step 5: Implement the administrator component**

Create `frontend/src/features/job-submissions/AdminJobSubmissions.vue`:

```vue
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";

import {
  decideJobSubmission, fetchAdminDuplicateCandidates, fetchAdminJobSubmissions,
} from "./jobSubmissionsApi";
import type { AdminJobSubmission, DuplicateCandidate } from "./jobSubmissionTypes";

const props = defineProps<{ token: string }>();
const emit = defineEmits<{ (event: "dirty-change", dirty: boolean): void }>();
const rows = ref<AdminJobSubmission[]>([]);
const selected = ref<AdminJobSubmission | null>(null);
const candidates = ref<DuplicateCandidate[]>([]);
const action = ref<"link_existing" | "create_pending" | "reject">("link_existing");
const form = reactive({ jobId: "", companyName: "", title: "", applyUrl: "", reasonCode: "insufficient_evidence" });
const message = ref("");
const dirty = computed(() => selected.value !== null && Boolean(
  form.jobId || form.companyName || form.title || form.applyUrl || action.value !== "link_existing"
));
watch(dirty, (value) => emit("dirty-change", value), { immediate: true });

async function load() {
  rows.value = (await fetchAdminJobSubmissions(props.token)).submissions;
}

async function choose(item: AdminJobSubmission) {
  selected.value = item;
  candidates.value = (await fetchAdminDuplicateCandidates(props.token, item.id)).candidates;
  action.value = "link_existing";
  Object.assign(form, { jobId: "", companyName: "", title: "", applyUrl: "", reasonCode: "insufficient_evidence" });
}

function chooseCandidate(candidate: DuplicateCandidate) {
  action.value = "link_existing";
  form.jobId = candidate.job.id;
}

async function decide() {
  if (!selected.value) return;
  const expected_version = selected.value.version;
  const payload = action.value === "link_existing"
    ? { expected_version, action: "link_existing" as const, job_id: form.jobId }
    : action.value === "create_pending"
      ? {
          expected_version, action: "create_pending" as const,
          company_name: form.companyName, title: form.title,
          ...(form.applyUrl ? { apply_url: form.applyUrl } : {}),
        }
      : {
          expected_version, action: "reject" as const,
          reason_code: form.reasonCode as "not_a_job" | "insufficient_evidence" | "unsafe_link" | "duplicate_submission",
        };
  await decideJobSubmission(props.token, selected.value.id, payload);
  message.value = action.value === "create_pending"
    ? "已创建；职位进入 pending_completion，尚未核验。"
    : "处理完成。";
  selected.value = null;
  candidates.value = [];
  await load();
}

onMounted(load);
</script>

<template>
  <section class="admin-submissions">
    <header><h2>手动职位处理</h2><p>关联和提升均需人工明确选择；创建后进入 pending_completion。</p></header>
    <div class="queue">
      <button
        v-for="item in rows" :key="item.id" :data-test="`select-${item.id}`"
        @click="choose(item)"
      >{{ item.input_preview }} · v{{ item.version }}</button>
    </div>
    <form v-if="selected" @submit.prevent="decide">
      <p>内容指纹：{{ selected.content_sha256 }}</p>
      <ul>
        <li v-for="candidate in candidates" :key="candidate.job.id">
          <button type="button" :data-test="`candidate-${candidate.job.id}`" @click="chooseCandidate(candidate)">
            {{ candidate.job.company_name }} · {{ candidate.job.title }} · {{ candidate.reasons.join("、") }}
          </button>
        </li>
      </ul>
      <label><input v-model="action" type="radio" value="link_existing">关联已有职位</label>
      <label><input v-model="action" data-test="action-create-pending" type="radio" value="create_pending">创建待补全职位</label>
      <label><input v-model="action" type="radio" value="reject">拒绝</label>
      <input v-if="action === 'link_existing'" v-model="form.jobId" aria-label="职位 ID">
      <template v-if="action === 'create_pending'">
        <input v-model="form.companyName" data-test="company-name" placeholder="公司名称">
        <input v-model="form.title" data-test="job-title" placeholder="岗位名称">
        <input v-model="form.applyUrl" placeholder="投递链接（可留空，后续补全）">
        <p>创建后进入 pending_completion，仍须走现有补全和核验流程。</p>
      </template>
      <select v-if="action === 'reject'" v-model="form.reasonCode">
        <option value="not_a_job">不是职位</option>
        <option value="insufficient_evidence">证据不足</option>
        <option value="unsafe_link">链接不安全</option>
        <option value="duplicate_submission">重复提交</option>
      </select>
      <button data-test="decide-submission" type="submit">确认处理</button>
    </form>
    <p role="status">{{ message }}</p>
  </section>
</template>

<style scoped>
.admin-submissions, form, .queue { display: grid; gap: .75rem; }
form { padding: 1rem; border: 1px solid #dbe3ea; border-radius: 16px; background: #fff; }
input, select, button { padding: .65rem .8rem; border: 1px solid #cbd5e1; border-radius: 10px; }
</style>
```

- [ ] **Step 6: Run administrator feature tests and typecheck**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/contract/test_job_submissions_api.py -q
npm.cmd --prefix frontend run test -- AdminJobSubmissions.spec.ts jobSubmissionsApi.spec.ts
npm.cmd --prefix frontend run typecheck
```

Expected: all API and component tests PASS; `vue-tsc` exits 0.

- [ ] **Step 7: Commit Task 8**

```powershell
git add backend/app/api/routes/job_submissions.py tests/contract/test_job_submissions_api.py frontend/src/features/job-submissions/jobSubmissionsApi.ts frontend/src/features/job-submissions/AdminJobSubmissions.vue frontend/src/features/job-submissions/__tests__/AdminJobSubmissions.spec.ts
git commit -m "feat: add manual job promotion queue"
```

---

### Task 9: Integrate shared router, navigation, error rendering, and operations documentation

**Files:**
- Modify: `backend/app/api/router.py`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/__tests__/App.spec.ts`
- Modify: `docs/runbooks/platform-foundation.md`

**Interfaces:**
- Consumes: Task 6 route module, Tasks 7–8 components, and an exclusive shared-file integration window.
- Produces: production `/api` mounting, role-aware navigation, unsaved-input guards, stable `detail.code` rendering, and operator recovery instructions.

- [ ] **Step 1: Reacquire the shared-file gate**

Run:

```powershell
git status --short -- backend/app/api/router.py frontend/src/api.ts frontend/src/App.vue frontend/src/__tests__/App.spec.ts docs/runbooks/platform-foundation.md
& .\.venv\Scripts\python.exe -m alembic heads
```

Expected: no unowned shared-file edits and exactly `20260717_0006 (head)`. Merge already-approved A changes before editing; do not copy an old `App.vue` or router over them.

- [ ] **Step 2: Write failing production-mount and navigation tests**

Remove the test-only `app.include_router(job_submissions.router, prefix="/api")` from `tests/contract/test_job_submissions_api.py`; its contract tests must now use the production router.

Append to `frontend/src/__tests__/App.spec.ts`:

```typescript
it("shows private submissions to students and admin processing only to administrators", async () => {
  const student = mount(App, {
    global: { stubs: { JobSubmissions: { template: "<section>私有手动职位</section>" } } },
  });
  await flushPromises();
  expect(student.get('[data-test="job-submissions-view"]').exists()).toBe(true);
  expect(student.find('[data-test="job-submission-review-view"]').exists()).toBe(false);
  student.unmount();

  apiMocks.fetchMe.mockResolvedValue({ ...profile, role: "admin" });
  const admin = mount(App, {
    global: { stubs: { AdminJobSubmissions: { template: "<section>手动职位处理</section>" } } },
  });
  await flushPromises();
  expect(admin.get('[data-test="job-submission-review-view"]').exists()).toBe(true);
});


it("guards navigation away from unsaved manual input", async () => {
  const wrapper = mount(App, { global: { stubs: {
    JobSubmissions: {
      template: '<section><button data-test="make-dirty" @click="$emit(\'dirty-change\', true)">dirty</button></section>',
    },
  } } });
  await flushPromises();
  await wrapper.get('[data-test="job-submissions-view"]').trigger("click");
  await wrapper.get('[data-test="make-dirty"]').trigger("click");
  vi.stubGlobal("confirm", vi.fn(() => false));
  await wrapper.get('[data-test="jobs-view"]').trigger("click");
  expect(confirm).toHaveBeenCalledOnce();
  expect(wrapper.text()).not.toContain("已核验职位");
});
```

- [ ] **Step 3: Run the production contract and navigation tests and verify they fail**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/contract/test_job_submissions_api.py -q
npm.cmd --prefix frontend run test -- App.spec.ts
```

Expected: backend tests return 404 because the production router is not mounted; frontend tests fail because the two views do not exist.

- [ ] **Step 4: Mount the feature route without domain logic in the shared router**

Update `backend/app/api/router.py` imports and includes only:

```python
from backend.app.api.routes import (
    analysis, auth, devices, health, job_submissions, jobs, sessions,
)

# keep existing includes in their current order
api_router.include_router(job_submissions.router)
```

- [ ] **Step 5: Teach the shared request helper to render `detail.code`**

In `frontend/src/api.ts`, extend the structured-detail branch without removing current `error_code` support:

```typescript
if (typeof structuredDetail.message === "string" && structuredDetail.message.trim()) {
  message = structuredDetail.message;
} else if (typeof structuredDetail.code === "string" && structuredDetail.code.trim()) {
  message = structuredDetail.code;
} else if (
  typeof structuredDetail.error_code === "string" && structuredDetail.error_code.trim()
) {
  message = structuredDetail.error_code;
}
```

Append to `frontend/src/__tests__/api.spec.ts` a response with `detail: { code: "stale_job_submission" }` and assert the thrown `ApiError.message` equals `stale_job_submission`.

- [ ] **Step 6: Add role-aware navigation and dirty guards to `App.vue`**

Add imports:

```typescript
import JobSubmissions from "./features/job-submissions/JobSubmissions.vue";
import AdminJobSubmissions from "./features/job-submissions/AdminJobSubmissions.vue";
```

Replace the workspace union and add flags:

```typescript
type WorkspaceView =
  | "analysis" | "jobs" | "job_submissions"
  | "job_review" | "job_submission_review";
const workspaceView = ref<WorkspaceView>("analysis");
const adminReviewDirty = ref(false);
const manualSubmissionDirty = ref(false);
const adminSubmissionDirty = ref(false);
```

Replace the beginning of `selectWorkspace` with a complete role/dirty guard:

```typescript
function selectWorkspace(next: WorkspaceView) {
  if (["job_review", "job_submission_review"].includes(next) && profile.value?.role !== "admin") return;
  const hasDirtyFeature =
    (workspaceView.value === "job_review" && adminReviewDirty.value)
    || (workspaceView.value === "job_submissions" && manualSubmissionDirty.value)
    || (workspaceView.value === "job_submission_review" && adminSubmissionDirty.value);
  if (next !== workspaceView.value && hasDirtyFeature && !window.confirm("当前草稿尚未保存，确定离开吗？")) {
    return;
  }
  workspaceView.value = next;
}
```

Replace the opening guard in `logout` and reset both new dirty refs:

```typescript
function logout() {
  const hasDirtyFeature =
    (workspaceView.value === "job_review" && adminReviewDirty.value)
    || (workspaceView.value === "job_submissions" && manualSubmissionDirty.value)
    || (workspaceView.value === "job_submission_review" && adminSubmissionDirty.value);
  if (hasDirtyFeature && !window.confirm("当前草稿尚未保存，确定退出吗？")) return;
  workspaceView.value = "analysis";
  adminReviewDirty.value = false;
  manualSubmissionDirty.value = false;
  adminSubmissionDirty.value = false;
  token.value = "";
  profile.value = null;
  sessions.value = [];
  selectedThreadId.value = "";
  sessionState.value = null;
  lastAnalysis.value = null;
  historyRows.value = [];
  localStorage.removeItem("job_assistant_token");
  setSuccess("已退出登录。");
}
```

In the role watcher, redirect both `job_review` and `job_submission_review` to `analysis` for non-admin users and clear their dirty flags. Add navigation buttons beside the existing job buttons:

```vue
<button
  data-test="job-submissions-view"
  :class="{ active: workspaceView === 'job_submissions' }"
  @click="selectWorkspace('job_submissions')"
>手动职位</button>
<button
  v-if="profile.role === 'admin'"
  data-test="job-submission-review-view"
  :class="{ active: workspaceView === 'job_submission_review' }"
  @click="selectWorkspace('job_submission_review')"
>手动职位处理</button>
```

Mount the feature components before the analysis workspace:

```vue
<JobSubmissions
  v-if="workspaceView === 'job_submissions'"
  :token="token"
  @dirty-change="manualSubmissionDirty = $event"
/>
<AdminJobSubmissions
  v-if="workspaceView === 'job_submission_review' && profile.role === 'admin'"
  :token="token"
  @dirty-change="adminSubmissionDirty = $event"
/>
```

- [ ] **Step 7: Add exact operator instructions to the runbook**

Append a `### 手动 JD 导入、去重和提升` subsection under the job operations chapter in `docs/runbooks/platform-foundation.md`:

```markdown
### 手动 JD 导入、去重和提升

1. 学生在“手动职位”中粘贴 `http/https` 链接或最多 100000 字符的 JD。提交默认私有；页面只回显 240 字符预览。
2. 重复结果是 `manual-job-dedup-v1` 候选，不能当作已合并结果。去重失败显示 `duplicate_detection_failed` 时，学生仍可修改和送审。
3. 管理员只能明确选择“关联已有职位”“创建待补全职位”或稳定原因码拒绝。创建结果固定为 `pending_completion`，随后进入现有职位补全与核验台。
4. `stale_job_submission` 表示版本冲突：重新加载队列后再处理，不得重放旧请求。
5. `unsafe_job_url` 表示链接命中协议、userinfo 或非公网地址边界；本功能不会抓取该 URL。
6. 每个腾讯和用户提交来源都在 `job_source_links` 中保留；腾讯重同步不得删除用户来源关系。

升级前确认 `alembic heads` 只有 `20260717_0005`，再执行 `alembic upgrade 20260717_0006`。降级到 `0005` 会删除 `0006` 期间创建的手动来源职位、候选和提交数据，必须先备份 MySQL 并确认业务影响。
```

- [ ] **Step 8: Run shared integration tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/contract/test_job_submissions_api.py tests/contract/test_jobs_api.py -q
npm.cmd --prefix frontend run test -- App.spec.ts api.spec.ts JobSubmissions.spec.ts AdminJobSubmissions.spec.ts
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build
```

Expected: backend contracts PASS; all selected frontend tests PASS; `vue-tsc` exits 0; Vite production build succeeds.

- [ ] **Step 9: Commit Task 9**

```powershell
git add backend/app/api/router.py tests/contract/test_job_submissions_api.py frontend/src/api.ts frontend/src/App.vue frontend/src/__tests__/api.spec.ts frontend/src/__tests__/App.spec.ts docs/runbooks/platform-foundation.md
git commit -m "feat: integrate manual job workflow"
```

---

### Task 10: Prove MySQL concurrency, privacy, migration, resync, and release gates

**Files:**
- Create: `tests/integration/test_job_submissions_mysql.py`
- Modify: `tests/integration/test_mysql_migration.py`
- Modify: `tests/security/test_no_sensitive_logging.py`

**Interfaces:**
- Consumes: completed Tasks 1–9 and dedicated `TEST_MYSQL_URL`.
- Produces: fresh evidence for one-winner promotion, source-link durability, migration round-trip, privacy boundaries, frontend build, and Compose readiness.

- [ ] **Step 1: Write the failing real-MySQL concurrent promotion test**

Create `tests/integration/test_job_submissions_mysql.py`; the test function parameter consumes the project-level `destructive_mysql_url: str` fixture from `tests/conftest.py`:

```python
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.models import (
    AuditEvent, JobDuplicateCandidate, JobPosting, JobPostingStatus,
    JobSource, JobSourceLink, JobSourceProvider, JobSyncRun,
    JobVerification, RawJobRecord, SubmissionStatus,
    User, UserJobSubmission, UserRole,
)
from backend.app.repositories import job_submissions
from backend.app.services.job_submissions import JobSubmissionService, StaleSubmissionError


def seed_verified_job(db: Session, marker: str) -> JobPosting:
    now = datetime.now(timezone.utc)
    source = JobSource(
        source_key=f"manual-concurrency-{marker}",
        provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Manual concurrency source", file_id=f"file-{marker}",
        sheet_id=f"sheet-{marker}", mapper_version="mysql-test-v1", enabled=True,
    )
    db.add(source)
    db.flush()
    raw = RawJobRecord(
        source_id=source.id, external_record_id=f"record-{marker}",
        payload_hash="a" * 64, raw_fields=[], observed_at=now,
    )
    db.add(raw)
    db.flush()
    posting = JobPosting(
        source_id=source.id, external_record_id=raw.external_record_id,
        raw_record_id=raw.id, status=JobPostingStatus.VERIFIED,
        company_name="并发测试公司", title="后端实习生",
        description_text="负责 FastAPI MySQL 服务开发",
        locations=["上海"], recruitment_types=["实习"], industries=["软件"],
        apply_url=f"https://jobs.example.com/{marker}",
        mapper_version=source.mapper_version, source_candidate={},
        verified_at=now,
    )
    db.add(posting)
    db.flush()
    return posting


def cleanup_manual_job_test(
    db: Session, *, owner_id: str, admin_id: str, marker: str,
) -> None:
    source_ids = list(db.scalars(select(JobSource.id).where(
        JobSource.source_key == f"manual-concurrency-{marker}"
    )))
    posting_ids = list(db.scalars(select(JobPosting.id).where(
        JobPosting.source_id.in_(source_ids)
    ))) if source_ids else []
    submission_ids = list(db.scalars(select(UserJobSubmission.id).where(
        UserJobSubmission.user_id == owner_id
    )))
    db.execute(delete(AuditEvent).where(AuditEvent.actor_user_id.in_([owner_id, admin_id])))
    if posting_ids or submission_ids:
        db.execute(delete(JobSourceLink).where(
            JobSourceLink.job_id.in_(posting_ids) | JobSourceLink.submission_id.in_(submission_ids)
        ))
    if submission_ids:
        db.execute(delete(JobDuplicateCandidate).where(
            JobDuplicateCandidate.submission_id.in_(submission_ids)
        ))
        db.execute(delete(UserJobSubmission).where(UserJobSubmission.id.in_(submission_ids)))
    if posting_ids:
        db.execute(delete(JobVerification).where(JobVerification.job_id.in_(posting_ids)))
        db.execute(delete(JobPosting).where(JobPosting.id.in_(posting_ids)))
    if source_ids:
        db.execute(delete(RawJobRecord).where(RawJobRecord.source_id.in_(source_ids)))
        db.execute(delete(JobSyncRun).where(JobSyncRun.source_id.in_(source_ids)))
        db.execute(delete(JobSource).where(JobSource.id.in_(source_ids)))
    db.execute(delete(User).where(User.id.in_([owner_id, admin_id])))


def test_mysql_concurrent_admin_promotion_has_one_winner(
    destructive_mysql_url: str,
) -> None:
    engine = create_engine(destructive_mysql_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    marker = uuid4().hex
    with factory() as setup:
        owner = User(account=f"owner-{marker}", nickname="Owner", password_hash="hash")
        admin = User(
            account=f"admin-{marker}", nickname="Admin", password_hash="hash",
            role=UserRole.ADMIN,
        )
        setup.add_all([owner, admin])
        setup.flush()
        target = seed_verified_job(setup, marker)
        item = JobSubmissionService().create(
            setup, user_id=owner.id, input_type="url", raw_value=target.apply_url
        )
        JobSubmissionService().submit(
            setup, user_id=owner.id, submission_id=item.id, expected_version=0
        )
        ids = (owner.id, admin.id, item.id, target.id)
        setup.commit()

    def promote() -> str:
        with factory() as db:
            try:
                JobSubmissionService().link_existing(
                    db, submission_id=ids[2], actor_user_id=ids[1],
                    expected_version=1, job_id=ids[3],
                )
                db.commit()
                return "success"
            except StaleSubmissionError:
                db.rollback()
                return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(lambda _index: promote(), range(2)))
    assert outcomes == ["stale", "success"]

    with factory() as verify:
        persisted = verify.get(UserJobSubmission, ids[2])
        assert persisted is not None
        assert (persisted.status, persisted.version) == (SubmissionStatus.PROMOTED, 2)
        assert verify.scalar(select(func.count()).select_from(JobSourceLink).where(
            JobSourceLink.submission_id == ids[2]
        )) == 1
        assert verify.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.entity_id == ids[2]
        )) == 1
        cleanup_manual_job_test(verify, owner_id=ids[0], admin_id=ids[1], marker=marker)
        verify.commit()
    engine.dispose()
```

- [ ] **Step 2: Add a real-MySQL source-link resync test**

Append:

```python
def test_mysql_tencent_resync_preserves_user_submission_link(
    destructive_mysql_url: str,
) -> None:
    engine = create_engine(destructive_mysql_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    marker = uuid4().hex
    with factory() as db:
        owner = User(account=f"resync-owner-{marker}", nickname="Owner", password_hash="hash")
        admin = User(
            account=f"resync-admin-{marker}", nickname="Admin", password_hash="hash",
            role=UserRole.ADMIN,
        )
        db.add_all([owner, admin])
        db.flush()
        posting = seed_verified_job(db, marker)
        source = db.get(JobSource, posting.source_id)
        assert source is not None
        posting.company_name = "人工规范公司"
        posting.review_version = 1
        db.add(JobVerification(
            job_id=posting.id, actor_user_id=admin.id, action="verify",
            from_status="pending_review", to_status="verified", review_version=1,
            field_snapshot={"company_name": "人工规范公司"}, reason_code=None,
        ))
        item = JobSubmissionService().create(
            db, user_id=owner.id, input_type="url", raw_value=posting.apply_url
        )
        job_submissions.link_submission_to_posting(db, submission=item, posting=posting)
        db.commit()
        raw, created = jobs.insert_raw_snapshot(
            db, source_id=source.id, external_record_id=posting.external_record_id,
            raw_fields=[{"title": "来源更新岗位"}], payload_hash="b" * 64,
            source_updated_at=datetime.now(timezone.utc), observed_at=datetime.now(timezone.utc),
        )
        assert created is True
        jobs.upsert_posting(
            db, source=source, raw_record=raw,
            candidate=NormalizedJobCandidate(
                company_name="来源不得覆盖公司", title="来源更新岗位",
                locations=["深圳"], recruitment_types=["实习"], industries=["软件"],
                apply_url=posting.apply_url, referral_code=None, deadline_text=None,
                source_updated_at=datetime.now(timezone.utc),
            ),
        )
        db.commit()
        links = db.scalars(select(JobSourceLink).where(JobSourceLink.job_id == posting.id)).all()
        assert {link.source_type.value for link in links} == {
            "tencent_smartsheet", "user_submission"
        }
        assert posting.company_name == "人工规范公司"
        cleanup_manual_job_test(
            db, owner_id=owner.id, admin_id=admin.id, marker=marker
        )
        db.commit()
    engine.dispose()
```

- [ ] **Step 3: Run the new test without opt-in and verify the guard**

Run:

```powershell
Remove-Item Env:ALLOW_DESTRUCTIVE_MYSQL_TESTS -ErrorAction SilentlyContinue
& .\.venv\Scripts\python.exe -m pytest tests/integration/test_job_submissions_mysql.py -q -rs
```

Expected: tests are skipped with exactly `requires ALLOW_DESTRUCTIVE_MYSQL_TESTS=1`; they must not connect to an arbitrary database.

- [ ] **Step 4: Run the destructive MySQL gate against the dedicated `_test` database**

Load `DB_PASSWORD` from the environment without printing it, then use the runbook's guarded `TEST_MYSQL_URL` construction. Run:

```powershell
$env:ALLOW_DESTRUCTIVE_MYSQL_TESTS = '1'
& .\.venv\Scripts\python.exe -m pytest tests/integration/test_mysql_migration.py tests/integration/test_job_submissions_mysql.py tests/integration/test_job_sync_mysql.py -q
```

Expected: migration `0005 → 0006 → 0005 → head`, both manual-job MySQL tests, and existing sync/review concurrency tests PASS. The guard must reject a URL whose database name does not end in `_test`.

- [ ] **Step 5: Run focused security and contract gates**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_job_submission_domain.py tests/unit/test_job_submission_repository.py tests/unit/test_job_submission_service.py tests/contract/test_job_submissions_api.py tests/contract/test_jobs_api.py tests/security/test_no_sensitive_logging.py -q
```

Expected: PASS with evidence that cross-user reads return 404; student candidates expose only `verified`; public/admin list DTOs contain no submitter identity or complete JD; unsafe URLs are rejected; promotion creates no `JobVerification`; and duplicate candidates do not merge automatically.

- [ ] **Step 6: Run full static, backend, and frontend regression**

Run:

```powershell
& .\.venv\Scripts\python.exe -m ruff check backend src tests scripts
& .\.venv\Scripts\python.exe -m pytest -q -rs
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build
git diff --check
```

Expected: Ruff prints `All checks passed!`; the full Python suite has no unexpected failures; Vitest, `vue-tsc`, and Vite build succeed; `git diff --check` emits no output. Record the exact pass/skip counts in the task handoff rather than copying counts from an older commit.

- [ ] **Step 7: Rebuild Compose and smoke the migrated application**

Use the current runbook ports and environment variables; never print `DB_PASSWORD` or `REDIS_PASSWORD`. Run:

```powershell
docker compose up --build -d
docker compose run --rm migrate alembic current
docker compose ps
Invoke-RestMethod http://127.0.0.1:8000/api/health/live
Invoke-RestMethod http://127.0.0.1:8000/api/health/ready
Invoke-WebRequest http://127.0.0.1:5173/ -UseBasicParsing
```

Expected: migration current is `20260717_0006`; MySQL, Redis, MinIO and backend are healthy; live and ready return HTTP 200; frontend returns HTTP 200. If configured host ports differ, substitute the six runbook-derived port variables instead of assuming these defaults.

- [ ] **Step 8: Perform the vertical-slice acceptance scenario**

Using one student and one administrator account in the Compose environment:

1. Submit one link matching an existing `verified` Tencent job and one original JD text.
2. Confirm both remain invisible to another student and the public `/api/jobs` DTO contains no submitter identity.
3. Confirm the first submission shows an explained candidate and remains unmerged.
4. Associate the first submission with the existing job; verify two `job_source_links` remain after a Tencent resync.
5. Promote the JD submission; verify its `JobPosting.status` is exactly `pending_completion`, then complete and verify it only through the existing administrator review flow.
6. Repeat a stale administrator request; verify HTTP 409 `stale_job_submission` and exactly one source link/audit event.

Expected: every numbered observation holds. If the real Tencent token is absent, record only the external resync step as unverified; do not claim the complete external release gate passed.

- [ ] **Step 9: Commit Task 10**

```powershell
git add tests/integration/test_job_submissions_mysql.py tests/integration/test_mysql_migration.py tests/security/test_no_sensitive_logging.py
git commit -m "test: verify manual job import gates"
```

---

## Completion Checklist

- [ ] Task 0 verified one Alembic head and an exclusive shared-file integration window.
- [ ] URL validation rejects non-HTTP(S), userinfo, localhost, loopback, link-local, private and reserved targets without fetching them.
- [ ] JD input is capped at 100000 characters; ordinary DTOs return only a 240-character preview.
- [ ] Student create/read/update/list/submit operations derive ownership only from bearer authentication; cross-user IDs return 404.
- [ ] Duplicate candidates are stable, versioned, explainable and never auto-merged; failure leaves the private submission editable.
- [ ] Student candidate responses contain only `verified` jobs; administrator candidates may include reviewable non-public jobs without submitter identity.
- [ ] Administrator link/create/reject uses a locked submission row plus `expected_version`; one transaction includes source link/posting, state change, and redacted audit.
- [ ] A newly promoted job is exactly `pending_completion`; only the existing review service may later append `JobVerification` and make it `verified`.
- [ ] Tencent upsert creates/preserves one Tencent link and never deletes user-submission links or overwrites reviewed canonical fields.
- [ ] Public jobs, student lists, administrator queue lists, errors and logs exclude `user_id`, account, full JD and sensitive URLs.
- [ ] Migration is exactly `20260717_0006` over `20260717_0005`, Alembic remains single-head, and guarded MySQL upgrade/downgrade passes.
- [ ] Python full regression/Ruff, Vitest/`vue-tsc`/Vite build, Compose migration, live/ready, and the vertical slice all have fresh recorded evidence.
