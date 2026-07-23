# Job Discovery 全量抓取与筛选重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 Job Discovery 模块改造成“完整遍历全部岗位列表 → 只保留指定招聘范围 → 抓取全部唯一 JD → 基于 JD 主体去重 → 执行硬条件过滤和相关性评分”的可靠流水线，并重新定义 PATH A/B/C 的职责边界。

**Architecture:** 保留 `StrategyRouter` 和 A/B/C 三路框架。PATH A 继续承担网站专用的确定性抓取；PATH B 升级为同时支持 `snapshot_plan` 和 `crawl_plan` 的确定性执行器；PATH C 从“直接浏览并返回岗位”改为“识别网站结构、生成或修复 CrawlPlan”，最终全量抓取必须回到 PATH B 执行。所有完成状态由 `CoverageVerifier` 根据分页和详情抓取证据判定，Agent 无权自行声明“已抓完”。

**Tech Stack:** Python、Pydantic/dataclass、Playwright、LangChain/DeepAgent、YAML、MySQL（沿用现有策略与轨迹存储）、pytest、SHA-256、标准库文本规范化与可选的现有相似度组件。

## Global Constraints

- 单次任务只能选择一个 `recruitment_type`：`campus`、`internship`、`social`；默认 `campus`。
- `campus` 和 `internship` 使用 `graduation_year`，默认 `2027`；`social` 强制将 `graduation_year` 归一化为 `None`。
- 必须先完整抓取目标招聘范围内的全部岗位，再进行筛选；不再使用 Top-K。
- 招聘类型、届别、学历属于硬门槛，不允许被技术方向、关键词、专业或地点分数补偿。
- 学历通过规则为“用户学历等级大于等于岗位最低学历等级”；硕士用户可投本科及硕士要求岗位，不可投博士最低要求岗位。
- “博士优先”属于偏好，不等同于“博士及以上”的硬要求。
- 专业和地点不作为硬淘汰项；不匹配时严重扣分。
- 匹配评分优先级：技术方向 = 关键词 > 专业 = 地点；招聘类型、届别、学历只在硬门槛结果中展示。
- 同一公司下，职责和任职要求主体相同的 JD 合并；允许地点、岗位编号、发布时间、招聘人数、招聘批次、页面模板不同。
- 详情页抓取前去重、详情资源去重、最终业务岗位去重必须是三个独立阶段。
- 只有 `coverage_complete=True` 且所有唯一详情页成功处理后，任务才可进入 `succeeded`。
- 完整抓取失败时保存中间数据和续跑游标，状态为 `partial_success` 或 `needs_manual_review`，不得向用户展示为完整结果。
- 完整抓取后匹配岗位数为 0 仍可判定为 `succeeded`。
- 所有招聘范围判定、硬过滤、合并和评分结论必须携带 `evidence_refs`。
- 不在本次重构中新增第四条路由；PATH B 内部通过 `plan_type` 区分 SnapshotPlan 与 CrawlPlan。
- 不在第一版新增数据库列；`plan_type`、计划版本和验证信息先写入 `plan_yaml` 顶层元数据，继续复用现有策略状态和成功/失败计数。
- 不使用截断后的 `PageEvidence.text_excerpt` 作为最终 JD 去重或评分输入；必须保留完整正文或完整正文的可重建结构化结果。
- 不绕过登录、验证码、滑块、扫码和反机器人页面；命中安全门禁时进入人工复核。
- 每个任务遵循 TDD：先写失败测试，再实现最小代码，再运行相关测试和回归测试。
- 每个任务独立提交，提交信息使用 Conventional Commits 风格。

---

## 1. 目标数据流

```text
DiscoveryTaskInput
    ↓
StrategyRouter
    ├─ PATH A: Certified Adapter
    ├─ PATH B: Deterministic Plan Executor
    └─ PATH C: Crawl Planning / Repair Agent
                         ↓
                    CrawlPlan
                         ↓
                     PATH B
    ↓
RawJobListing[]
    ↓
列表记录与详情资源预去重
    ↓
RawJobDetail[]
    ↓
RecruitmentScopeClassifier
    ↓
NormalizedJD[]
    ↓
CanonicalJobDeduplicator
    ↓
EligibilityGate
    ↓
MatchScorer
    ↓
MatchedJob[]
    ↓
DiscoveryRunResult + CrawlCoverage
```

## 2. 文件结构变更

### 新建文件

```text
job_discovery/
├── crawling/
│   ├── __init__.py
│   ├── crawl_plan.py                 # CrawlPlan、分页、字段和完成规则的 schema
│   ├── crawl_executor.py             # 通用全量抓取编排器
│   ├── pagination.py                 # 六类分页执行器与页面指纹
│   ├── coverage.py                   # CrawlCoverage 构建与完整性验证
│   └── checkpoint.py                 # 可序列化检查点与续跑游标
│
├── scope/
│   ├── __init__.py
│   ├── recruitment_classifier.py     # campus/internship/social + 届别判定
│   └── education_gate.py             # 学历解析和硬门槛
│
├── normalization/
│   ├── __init__.py
│   ├── jd_parser.py                  # 职责/要求/学历/专业/技能结构化
│   └── jd_normalizer.py              # 去模板、非业务字段和核心哈希
│
├── deduplication/
│   ├── __init__.py
│   ├── source_deduplicator.py        # 原始记录和详情 URL 去重
│   └── canonical_job_deduplicator.py # 基于 JD 主体的业务岗位合并
│
├── matching/
│   ├── __init__.py
│   ├── eligibility.py                # 招聘类型、届别、学历硬门槛
│   └── scorer.py                     # 技术方向、关键词、专业、地点评分
│
└── prompts/
    └── crawl_plan_agent.txt           # PATH C 新职责提示词
```

### 修改文件

```text
job_discovery/schemas.py
job_discovery/result_contract.py
job_discovery/deepagents_runner.py
job_discovery/worker.py
job_discovery/adapters/base.py
job_discovery/adapters/alibaba_spa.py
job_discovery/strategy/snapshot_executor.py
job_discovery/strategy/strategy_router.py
job_discovery/strategy/trajectory_buffer.py
job_discovery/strategy/error_classifier.py
job_discovery/tools/jd_extraction.py
job_discovery/tools/evidence_verifier.py
job_discovery/tools/candidate_packager.py
job_discovery/prompts/supervisor_base.txt
job_discovery/prompts/supervisor_clean_start.txt
job_discovery/prompts/supervisor_snapshot_fallback.txt
CLAUDE.md
```

### 新建测试文件

```text
tests/unit/job_discovery/test_crawl_schemas.py
tests/unit/job_discovery/test_coverage.py
tests/unit/job_discovery/test_pagination.py
tests/unit/job_discovery/test_crawl_executor.py
tests/unit/job_discovery/test_recruitment_classifier.py
tests/unit/job_discovery/test_education_gate.py
tests/unit/job_discovery/test_jd_normalizer.py
tests/unit/job_discovery/test_canonical_job_deduplicator.py
tests/unit/job_discovery/test_eligibility.py
tests/unit/job_discovery/test_match_scorer.py
tests/unit/job_discovery/test_result_contract_v2.py
tests/unit/job_discovery/test_path_routing_v2.py
tests/integration/job_discovery/test_complete_crawl_pipeline.py
```

---

### Task 1: 扩展任务输入、抓取结果和岗位实体契约

**Files:**
- Modify: `job_discovery/schemas.py`
- Test: `tests/unit/job_discovery/test_crawl_schemas.py`

**Interfaces:**
- Consumes: 现有 `DiscoveryTaskInput`、`DiscoveryRunResult`、`PageEvidence`。
- Produces:
  - `RecruitmentScope`
  - `PaginationType`
  - `CrawlCoverage`
  - `RawJobListing`
  - `RawJobDetail`
  - `RecruitmentScopeDecision`
  - `NormalizedJD`
  - `CanonicalJob`
  - 扩展后的 `DiscoveryTaskInput` 和 `DiscoveryRunResult`

- [ ] **Step 1: 写招聘范围默认值和校验的失败测试**

```python
from job_discovery.schemas import RecruitmentScope

def test_recruitment_scope_defaults_to_2027_campus():
    scope = RecruitmentScope()
    assert scope.recruitment_type == "campus"
    assert scope.graduation_year == 2027

def test_social_scope_forces_graduation_year_to_none():
    scope = RecruitmentScope(
        recruitment_type="social",
        graduation_year=2027,
    )
    assert scope.graduation_year is None

def test_internship_requires_graduation_year():
    try:
        RecruitmentScope(
            recruitment_type="internship",
            graduation_year=None,
        )
    except ValueError as exc:
        assert "graduation_year" in str(exc)
    else:
        raise AssertionError("expected ValueError")
```

- [ ] **Step 2: 运行测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/job_discovery/test_crawl_schemas.py -v
```

Expected: FAIL，`RecruitmentScope` 尚未定义。

- [ ] **Step 3: 在 `schemas.py` 中加入核心类型**

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

RecruitmentType = Literal["campus", "internship", "social"]
DecisionStatus = Literal["PASS", "FAIL", "REVIEW"]


@dataclass
class RecruitmentScope:
    recruitment_type: RecruitmentType = "campus"
    graduation_year: int | None = 2027

    def __post_init__(self) -> None:
        if self.recruitment_type == "social":
            self.graduation_year = None
            return
        if self.graduation_year is None:
            raise ValueError(
                "graduation_year is required for campus and internship"
            )


class PaginationType(str, Enum):
    PAGE_NUMBER = "page_number"
    NEXT_BUTTON = "next_button"
    LOAD_MORE = "load_more"
    INFINITE_SCROLL = "infinite_scroll"
    API_CURSOR = "api_cursor"
    API_OFFSET = "api_offset"
    SINGLE_PAGE = "single_page"
    UNKNOWN = "unknown"


@dataclass
class CrawlCoverage:
    pagination_type: PaginationType
    expected_page_count: int | None = None
    visited_page_count: int = 0
    visited_page_keys: list[str] = field(default_factory=list)
    expected_listing_count: int | None = None
    raw_listing_count: int = 0
    unique_listing_count: int = 0
    total_detail_count: int = 0
    fetched_detail_count: int = 0
    failed_detail_count: int = 0
    coverage_complete: bool = False
    completion_evidence: list[str] = field(default_factory=list)
    incomplete_reason: str | None = None
    resumable: bool = False
    resume_cursor: dict | None = None


@dataclass
class RawJobListing:
    source_url: str
    detail_url: str | None
    company: str | None
    title: str
    locations: list[str] = field(default_factory=list)
    job_code: str | None = None
    recruitment_type_hint: str | None = None
    graduation_year_hints: list[int] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    source_record_key: str | None = None


@dataclass
class RawJobDetail:
    detail_url: str
    full_text: str
    title: str | None = None
    company: str | None = None
    locations: list[str] = field(default_factory=list)
    job_code: str | None = None
    structured_fields: dict = field(default_factory=dict)
    channel_text: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    detail_resource_key: str | None = None


@dataclass
class RecruitmentScopeDecision:
    detected_type: Literal["campus", "internship", "social", "unknown"]
    detected_years: list[int]
    type_status: DecisionStatus
    year_status: DecisionStatus
    confidence: float
    evidence_refs: list[str]
    reason: str


@dataclass
class NormalizedJD:
    responsibilities: list[str]
    requirements: list[str]
    education_requirement: str | None
    major_requirements: list[str]
    technical_skills: list[str]
    normalized_responsibilities: str
    normalized_requirements: str
    normalized_core_text: str
    core_hash: str
    removable_metadata: dict = field(default_factory=dict)


@dataclass
class CanonicalJob:
    canonical_job_id: str
    company: str
    canonical_title: str
    alternative_titles: list[str]
    locations: list[str]
    recruitment_type: str
    graduation_years: list[int]
    jd: NormalizedJD
    detail_urls: list[str]
    apply_urls: list[str]
    source_listing_urls: list[str]
    source_record_ids: list[str]
    source_job_codes: list[str]
    evidence_refs: list[str]
    merged_record_count: int
```

扩展 `DiscoveryTaskInput`：

```python
recruitment_scope: RecruitmentScope = field(
    default_factory=RecruitmentScope
)
minimum_match_score: int = 60
technical_directions: list[str] = field(default_factory=list)
positive_keywords: list[str] = field(default_factory=list)
negative_keywords: list[str] = field(default_factory=list)
preferred_majors: list[str] = field(default_factory=list)
preferred_locations: list[str] = field(default_factory=list)
user_education: str | None = None
```

扩展 `DiscoveryRunResult`：

```python
coverage: CrawlCoverage | None = None
raw_listing_count: int = 0
canonical_job_count: int = 0
eligible_job_count: int = 0
matched_job_count: int = 0
scope_filter_completed: bool = False
dedup_completed: bool = False
scoring_completed: bool = False
review_candidates: list = field(default_factory=list)
```

- [ ] **Step 4: 增加序列化往返测试**

```python
def test_crawl_coverage_keeps_resume_cursor():
    coverage = CrawlCoverage(
        pagination_type=PaginationType.API_CURSOR,
        visited_page_count=3,
        resumable=True,
        resume_cursor={"cursor": "next-3"},
    )
    assert coverage.resume_cursor == {"cursor": "next-3"}
    assert coverage.coverage_complete is False
```

- [ ] **Step 5: 运行测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/job_discovery/test_crawl_schemas.py -v
```

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add job_discovery/schemas.py tests/unit/job_discovery/test_crawl_schemas.py
git commit -m "feat(job-discovery): add full-crawl domain contracts"
```

---

### Task 2: 建立抓取完整性验证器并修改结果不变量

**Files:**
- Create: `job_discovery/crawling/__init__.py`
- Create: `job_discovery/crawling/coverage.py`
- Modify: `job_discovery/result_contract.py`
- Modify: `job_discovery/worker.py`
- Test: `tests/unit/job_discovery/test_coverage.py`
- Test: `tests/unit/job_discovery/test_result_contract_v2.py`

**Interfaces:**
- Consumes: `CrawlCoverage`、扩展后的 `DiscoveryRunResult`。
- Produces:
  - `verify_coverage(coverage: CrawlCoverage) -> CoverageDecision`
  - `enforce_result_invariants(result: DiscoveryRunResult) -> DiscoveryRunResult`

- [ ] **Step 1: 写“有岗位但分页不完整不得成功”的失败测试**

```python
from job_discovery.crawling.coverage import verify_coverage
from job_discovery.schemas import CrawlCoverage, PaginationType

def test_jobs_found_does_not_mean_complete():
    coverage = CrawlCoverage(
        pagination_type=PaginationType.PAGE_NUMBER,
        expected_page_count=10,
        visited_page_count=4,
        raw_listing_count=80,
        unique_listing_count=70,
    )
    decision = verify_coverage(coverage)
    assert decision.complete is False
    assert "page" in decision.reason.lower()
```

- [ ] **Step 2: 写“零匹配岗位也可成功”的结果契约测试**

```python
def test_succeeded_result_allows_zero_matched_jobs():
    result = make_result(
        status="succeeded",
        candidates=[],
        coverage=complete_coverage(),
        scope_filter_completed=True,
        dedup_completed=True,
        scoring_completed=True,
    )
    enforced = enforce_result_invariants(result)
    assert enforced.status == "succeeded"
```

- [ ] **Step 3: 运行测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_coverage.py `
  tests/unit/job_discovery/test_result_contract_v2.py -v
```

Expected: FAIL，覆盖验证器不存在，旧不变量仍要求候选岗位非空。

- [ ] **Step 4: 实现覆盖验证结果**

```python
from dataclasses import dataclass
from job_discovery.schemas import CrawlCoverage, PaginationType


@dataclass(frozen=True)
class CoverageDecision:
    complete: bool
    status: str
    reason: str


def verify_coverage(coverage: CrawlCoverage) -> CoverageDecision:
    if coverage.failed_detail_count > 0:
        return CoverageDecision(
            complete=False,
            status="partial_success" if coverage.resumable else "needs_manual_review",
            reason=f"{coverage.failed_detail_count} detail pages failed",
        )

    if (
        coverage.expected_page_count is not None
        and coverage.visited_page_count != coverage.expected_page_count
    ):
        return CoverageDecision(
            complete=False,
            status="partial_success" if coverage.resumable else "needs_manual_review",
            reason=(
                f"visited {coverage.visited_page_count}/"
                f"{coverage.expected_page_count} pages"
            ),
        )

    if (
        coverage.expected_listing_count is not None
        and coverage.raw_listing_count < coverage.expected_listing_count
    ):
        return CoverageDecision(
            complete=False,
            status="partial_success" if coverage.resumable else "needs_manual_review",
            reason=(
                f"collected {coverage.raw_listing_count}/"
                f"{coverage.expected_listing_count} listings"
            ),
        )

    if coverage.pagination_type == PaginationType.UNKNOWN:
        return CoverageDecision(
            complete=False,
            status="needs_manual_review",
            reason="pagination completion cannot be proven",
        )

    if not coverage.completion_evidence:
        return CoverageDecision(
            complete=False,
            status="needs_manual_review",
            reason="missing positive completion evidence",
        )

    return CoverageDecision(
        complete=True,
        status="succeeded",
        reason="all pages and detail resources completed",
    )
```

- [ ] **Step 5: 修改 `enforce_result_invariants`**

移除：

```python
if result.status == "succeeded" and not result.candidates:
    ...
```

替换为：

```python
if result.status == "succeeded":
    if result.coverage is None:
        result.status = "needs_manual_review"
        result.error = "succeeded result missing crawl coverage"
        return result

    coverage_decision = verify_coverage(result.coverage)
    if not coverage_decision.complete:
        result.status = coverage_decision.status
        result.error = coverage_decision.reason
        return result

    required_stages = (
        result.scope_filter_completed,
        result.dedup_completed,
        result.scoring_completed,
    )
    if not all(required_stages):
        result.status = "partial_success"
        result.error = "post-crawl pipeline is incomplete"
```

- [ ] **Step 6: 运行测试和现有契约回归**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_coverage.py `
  tests/unit/job_discovery/test_result_contract_v2.py `
  tests/unit/ -k "result_contract or job_discovery" -v
```

Expected: 新测试 PASS；旧的“成功必须有候选”测试需要改为“成功必须有完整覆盖”。

- [ ] **Step 7: 提交**

```powershell
git add job_discovery/crawling job_discovery/result_contract.py `
  job_discovery/worker.py tests/unit/job_discovery/test_coverage.py `
  tests/unit/job_discovery/test_result_contract_v2.py
git commit -m "feat(job-discovery): enforce crawl coverage invariants"
```

---

### Task 3: 定义和验证 CrawlPlan

**Files:**
- Create: `job_discovery/crawling/crawl_plan.py`
- Modify: `job_discovery/strategy/snapshot_executor.py`
- Test: `tests/unit/job_discovery/test_crawl_schemas.py`

**Interfaces:**
- Consumes: YAML 字符串、`RecruitmentScope`。
- Produces:
  - `CrawlPlan.from_yaml(plan_yaml: str) -> CrawlPlan`
  - `CrawlPlan.validate_security() -> None`
  - 顶层 `plan_type: "crawl_plan"` 与 `version: 1`

- [ ] **Step 1: 写有效页码 CrawlPlan 的解析测试**

```python
def test_parse_page_number_crawl_plan():
    plan = CrawlPlan.from_yaml("""
plan_type: crawl_plan
version: 1
listing:
  item_selector: ".job-card"
  title_selector: ".job-title"
  detail_link_selector: "a@href"
pagination:
  type: page_number
  page_selector: ".pagination-item"
  next_selector: ".pagination-next"
detail:
  title_selector: "h1"
  body_selector: ".job-description"
completion:
  require_all_pages: true
  require_all_details: true
""")
    assert plan.pagination.type == PaginationType.PAGE_NUMBER
    assert plan.completion.require_all_pages is True
```

- [ ] **Step 2: 写无终止证据的无限滚动计划拒绝测试**

```python
def test_infinite_scroll_requires_positive_terminal_signal():
    with pytest.raises(ValueError, match="terminal"):
        CrawlPlan.from_yaml("""
plan_type: crawl_plan
version: 1
listing:
  item_selector: ".job"
  title_selector: ".title"
pagination:
  type: infinite_scroll
detail:
  body_selector: ".jd"
completion:
  require_all_pages: true
  require_all_details: true
""")
```

- [ ] **Step 3: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_crawl_schemas.py -v
```

- [ ] **Step 4: 实现 Plan schema**

```python
from dataclasses import dataclass, field
from typing import Any
import yaml

from job_discovery.schemas import PaginationType


@dataclass
class ListingSchema:
    item_selector: str
    title_selector: str
    detail_link_selector: str | None = None
    location_selector: str | None = None
    job_code_selector: str | None = None


@dataclass
class PaginationSchema:
    type: PaginationType
    page_selector: str | None = None
    next_selector: str | None = None
    disabled_selector: str | None = None
    terminal_selector: str | None = None
    endpoint_pattern: str | None = None
    items_path: str | None = None
    total_count_path: str | None = None
    has_more_path: str | None = None
    next_cursor_path: str | None = None
    offset_path: str | None = None


@dataclass
class DetailSchema:
    title_selector: str | None = None
    body_selector: str | None = None
    responsibility_selector: str | None = None
    requirement_selector: str | None = None
    education_selector: str | None = None
    major_selector: str | None = None
    location_selector: str | None = None


@dataclass
class CompletionRules:
    require_all_pages: bool = True
    require_all_details: bool = True


@dataclass
class CrawlPlan:
    version: int
    listing: ListingSchema
    pagination: PaginationSchema
    detail: DetailSchema
    completion: CompletionRules
    scope_actions: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, plan_yaml: str) -> "CrawlPlan":
        raw = yaml.safe_load(plan_yaml)
        if raw.get("plan_type") != "crawl_plan":
            raise ValueError("plan_type must be crawl_plan")
        pagination = PaginationSchema(
            **{
                **raw["pagination"],
                "type": PaginationType(raw["pagination"]["type"]),
            }
        )
        plan = cls(
            version=int(raw["version"]),
            listing=ListingSchema(**raw["listing"]),
            pagination=pagination,
            detail=DetailSchema(**raw["detail"]),
            completion=CompletionRules(**raw["completion"]),
            scope_actions=raw.get("scope_actions", []),
        )
        plan.validate_security()
        return plan

    def validate_security(self) -> None:
        if self.version != 1:
            raise ValueError("unsupported crawl plan version")
        if self.pagination.type == PaginationType.INFINITE_SCROLL:
            terminal_fields = (
                self.pagination.terminal_selector,
                self.pagination.has_more_path,
                self.pagination.total_count_path,
            )
            if not any(terminal_fields):
                raise ValueError(
                    "infinite_scroll requires a positive terminal signal"
                )
```

- [ ] **Step 5: 在 SnapshotExecutor 中识别计划类型**

```python
raw_plan = yaml.safe_load(strategy.plan_yaml)

if raw_plan.get("plan_type", "snapshot_plan") == "crawl_plan":
    return CrawlPlanExecutionRequest(
        plan=CrawlPlan.from_yaml(strategy.plan_yaml),
        snapshot_context=snapshot_context,
    )
```

此任务只完成分派，不在 SnapshotExecutor 中直接实现循环。

- [ ] **Step 6: 运行测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_crawl_schemas.py -v
```

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add job_discovery/crawling/crawl_plan.py `
  job_discovery/strategy/snapshot_executor.py `
  tests/unit/job_discovery/test_crawl_schemas.py
git commit -m "feat(job-discovery): add validated crawl plan schema"
```

---

### Task 4: 实现分页状态机和检查点

**Files:**
- Create: `job_discovery/crawling/checkpoint.py`
- Create: `job_discovery/crawling/pagination.py`
- Modify: `job_discovery/strategy/trajectory_buffer.py`
- Test: `tests/unit/job_discovery/test_pagination.py`

**Interfaces:**
- Consumes: `CrawlPlan.pagination`、浏览器/API 访问抽象。
- Produces:
  - `PaginationCheckpoint`
  - `PaginationPage`
  - `iterate_pages(...) -> Iterator[PaginationPage]`
  - `page_fingerprint(content: str, item_keys: list[str]) -> str`

- [ ] **Step 1: 写页码必须遍历 1..N 的失败测试**

```python
def test_page_number_iterator_visits_every_page():
    driver = FakePageNumberDriver(total_pages=10)
    pages = list(iterate_pages(page_number_plan(), driver))
    assert [page.sequence for page in pages] == list(range(1, 11))
    assert pages[-1].terminal_evidence == "visited_all_numbered_pages"
```

- [ ] **Step 2: 写循环页面检测测试**

```python
def test_next_button_stops_with_error_on_repeated_fingerprint():
    driver = FakeNextDriver(
        pages=["page-a", "page-b", "page-b", "page-b"],
        next_disabled=False,
    )
    with pytest.raises(PaginationLoopError):
        list(iterate_pages(next_button_plan(), driver))
```

- [ ] **Step 3: 写 API cursor 完成测试**

```python
def test_cursor_iterator_requires_has_more_false():
    driver = FakeCursorDriver([
        {"items": [1, 2], "next": "c2", "has_more": True},
        {"items": [3], "next": None, "has_more": False},
    ])
    pages = list(iterate_pages(cursor_plan(), driver))
    assert len(pages) == 2
    assert pages[-1].terminal_evidence == "has_more=false"
```

- [ ] **Step 4: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_pagination.py -v
```

- [ ] **Step 5: 实现检查点对象**

```python
from dataclasses import dataclass, field


@dataclass
class PaginationCheckpoint:
    pagination_type: str
    next_page_number: int | None = None
    next_cursor: str | None = None
    next_offset: int | None = None
    visited_page_keys: list[str] = field(default_factory=list)
    collected_source_record_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pagination_type": self.pagination_type,
            "next_page_number": self.next_page_number,
            "next_cursor": self.next_cursor,
            "next_offset": self.next_offset,
            "visited_page_keys": list(self.visited_page_keys),
            "collected_source_record_keys": list(
                self.collected_source_record_keys
            ),
        }
```

- [ ] **Step 6: 实现页面指纹和分页分派**

```python
import hashlib
from dataclasses import dataclass
from collections.abc import Iterator

from job_discovery.schemas import PaginationType


class PaginationLoopError(RuntimeError):
    pass


@dataclass
class PaginationPage:
    sequence: int
    page_key: str
    payload: object
    item_count: int
    terminal_evidence: str | None = None


def page_fingerprint(content: str, item_keys: list[str]) -> str:
    normalized = " ".join(content.split())
    material = normalized + "\n" + "\n".join(sorted(item_keys))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def iterate_pages(plan, driver, checkpoint=None) -> Iterator[PaginationPage]:
    if plan.pagination.type == PaginationType.PAGE_NUMBER:
        yield from _iterate_page_numbers(plan, driver, checkpoint)
        return
    if plan.pagination.type == PaginationType.NEXT_BUTTON:
        yield from _iterate_next_button(plan, driver, checkpoint)
        return
    if plan.pagination.type == PaginationType.API_CURSOR:
        yield from _iterate_api_cursor(plan, driver, checkpoint)
        return
    if plan.pagination.type == PaginationType.API_OFFSET:
        yield from _iterate_api_offset(plan, driver, checkpoint)
        return
    if plan.pagination.type == PaginationType.LOAD_MORE:
        yield from _iterate_load_more(plan, driver, checkpoint)
        return
    if plan.pagination.type == PaginationType.INFINITE_SCROLL:
        yield from _iterate_infinite_scroll(plan, driver, checkpoint)
        return
    if plan.pagination.type == PaginationType.SINGLE_PAGE:
        yield driver.read_single_page()
        return
    raise ValueError("unsupported or unknown pagination type")
```

每个私有迭代器必须：

- 将页面指纹加入 `visited_page_keys`；
- 指纹重复且没有合法终止信号时抛出 `PaginationLoopError`；
- 只接受正向终止证据；
- 每成功一页更新可序列化检查点；
- 记录 `trajectory.record_step(tool="crawl_page", ...)`。

- [ ] **Step 7: 运行测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_pagination.py -v
```

Expected: PASS。

- [ ] **Step 8: 提交**

```powershell
git add job_discovery/crawling/checkpoint.py `
  job_discovery/crawling/pagination.py `
  job_discovery/strategy/trajectory_buffer.py `
  tests/unit/job_discovery/test_pagination.py
git commit -m "feat(job-discovery): add deterministic pagination state machine"
```

---

### Task 5: 实现列表收集、详情抓取与 CrawlExecutor

**Files:**
- Create: `job_discovery/crawling/crawl_executor.py`
- Create: `job_discovery/deduplication/__init__.py`
- Create: `job_discovery/deduplication/source_deduplicator.py`
- Modify: `job_discovery/deepagents_runner.py`
- Test: `tests/unit/job_discovery/test_crawl_executor.py`

**Interfaces:**
- Consumes: `CrawlPlan`、分页迭代器、现有 Playwright 会话和 XHR 捕获能力。
- Produces:
  - `normalize_detail_url(url: str) -> str`
  - `make_source_record_key(record: RawJobListing) -> str`
  - `make_detail_resource_key(record: RawJobListing) -> str`
  - `CrawlExecutor.execute(...) -> CrawlExecutionResult`

- [ ] **Step 1: 写跟踪参数归一化测试**

```python
def test_normalize_detail_url_removes_tracking_parameters():
    left = normalize_detail_url(
        "https://jobs.example.com/job/123?city=beijing&utm_source=campus"
    )
    right = normalize_detail_url(
        "https://jobs.example.com/job/123?city=beijing&source=wechat"
    )
    assert left == right
```

保留可能改变实际详情内容的业务参数；第一版只移除明确的跟踪参数：

```python
TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "source",
    "channel",
    "ref",
    "timestamp",
    "session",
}
```

- [ ] **Step 2: 写“同一详情页只抓一次但合并地区”的测试**

```python
def test_executor_fetches_shared_detail_once():
    driver = FakeCrawlDriver(
        listings=[
            listing(detail_url="/job/123", location="北京"),
            listing(detail_url="/job/123", location="上海"),
        ],
        details={"/job/123": "岗位职责...任职要求..."},
    )
    result = CrawlExecutor(driver).execute(crawl_plan(), task())
    assert driver.detail_fetch_count["/job/123"] == 1
    assert result.raw_listings[0].locations == ["北京", "上海"]
```

- [ ] **Step 3: 写详情失败产生可续跑 partial 状态测试**

```python
def test_executor_records_failed_detail_and_resume_cursor():
    driver = FakeCrawlDriver(
        listings=[
            listing(detail_url="/job/1"),
            listing(detail_url="/job/2"),
        ],
        details={
            "/job/1": "success",
            "/job/2": ConnectionError("temporary"),
        },
    )
    result = CrawlExecutor(driver).execute(crawl_plan(), task())
    assert result.coverage.failed_detail_count == 1
    assert result.coverage.coverage_complete is False
    assert result.coverage.resumable is True
```

- [ ] **Step 4: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_crawl_executor.py -v
```

- [ ] **Step 5: 实现三种预去重键**

```python
import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_discovery.schemas import RawJobListing


TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "source",
    "channel",
    "ref",
    "timestamp",
    "session",
}


def normalize_detail_url(url: str) -> str:
    parts = urlsplit(url)
    filtered = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            urlencode(sorted(filtered)),
            "",
        )
    )


def make_source_record_key(record: RawJobListing) -> str:
    material = "|".join(
        [
            (record.company or "").strip().lower(),
            record.title.strip().lower(),
            ",".join(sorted(record.locations)),
            record.job_code or "",
            normalize_detail_url(record.detail_url or record.source_url),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def make_detail_resource_key(record: RawJobListing) -> str:
    if record.job_code:
        material = f"{record.company or ''}|job_code|{record.job_code}"
    else:
        material = normalize_detail_url(
            record.detail_url or record.source_url
        )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
```

- [ ] **Step 6: 实现 CrawlExecutor 编排**

```python
from dataclasses import dataclass

from job_discovery.crawling.coverage import verify_coverage
from job_discovery.crawling.pagination import iterate_pages
from job_discovery.schemas import CrawlCoverage, RawJobDetail


@dataclass
class CrawlExecutionResult:
    raw_listings: list
    raw_details: list[RawJobDetail]
    coverage: CrawlCoverage
    checkpoint: dict | None
    error: str | None = None


class CrawlExecutor:
    def __init__(self, driver, trajectory=None):
        self.driver = driver
        self.trajectory = trajectory

    def execute(self, plan, task, checkpoint=None) -> CrawlExecutionResult:
        listings = []
        page_keys = []
        completion_evidence = []

        for page in iterate_pages(plan, self.driver, checkpoint):
            page_keys.append(page.page_key)
            listings.extend(
                self.driver.extract_listings(page, plan.listing)
            )
            if page.terminal_evidence:
                completion_evidence.append(page.terminal_evidence)

        merged_listings = merge_duplicate_source_records(listings)
        unique_resources = group_by_detail_resource(merged_listings)

        details = []
        failed_resources = []
        for resource_key, records in unique_resources.items():
            try:
                details.append(
                    self.driver.fetch_detail(
                        records[0],
                        plan.detail,
                        resource_key=resource_key,
                    )
                )
            except Exception as exc:
                failed_resources.append((resource_key, str(exc)))

        coverage = CrawlCoverage(
            pagination_type=plan.pagination.type,
            expected_page_count=self.driver.expected_page_count,
            visited_page_count=len(page_keys),
            visited_page_keys=page_keys,
            expected_listing_count=self.driver.expected_listing_count,
            raw_listing_count=len(listings),
            unique_listing_count=len(merged_listings),
            total_detail_count=len(unique_resources),
            fetched_detail_count=len(details),
            failed_detail_count=len(failed_resources),
            completion_evidence=completion_evidence,
            resumable=bool(failed_resources),
            resume_cursor=self.driver.current_checkpoint(),
        )
        coverage.coverage_complete = verify_coverage(coverage).complete

        return CrawlExecutionResult(
            raw_listings=merged_listings,
            raw_details=details,
            coverage=coverage,
            checkpoint=coverage.resume_cursor,
            error=failed_resources[0][1] if failed_resources else None,
        )
```

`merge_duplicate_source_records` 合并标题相同、详情资源相同的地点和来源证据；不得丢弃来源 URL。

- [ ] **Step 7: 运行测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_crawl_executor.py `
  tests/unit/job_discovery/test_pagination.py -v
```

Expected: PASS。

- [ ] **Step 8: 提交**

```powershell
git add job_discovery/crawling/crawl_executor.py `
  job_discovery/deduplication `
  job_discovery/deepagents_runner.py `
  tests/unit/job_discovery/test_crawl_executor.py
git commit -m "feat(job-discovery): execute complete list and detail crawls"
```

---

### Task 6: 实现招聘类型、届别组合判定

**Files:**
- Create: `job_discovery/scope/__init__.py`
- Create: `job_discovery/scope/recruitment_classifier.py`
- Modify: `job_discovery/tools/evidence_verifier.py`
- Test: `tests/unit/job_discovery/test_recruitment_classifier.py`

**Interfaces:**
- Consumes: 频道、筛选条件、结构化字段、详情正文、URL 和标题证据。
- Produces:
  - `classify_recruitment_scope(...) -> RecruitmentScopeDecision`

- [ ] **Step 1: 写证据优先级测试**

```python
def test_structured_social_field_overrides_campus_channel():
    decision = classify_recruitment_scope(
        target=RecruitmentScope("campus", 2027),
        structured_fields={"recruitType": "SOCIAL"},
        channel_text="2027届校园招聘",
        detail_text="三年以上工作经验",
        url="https://example.com/campus/job/1",
        title="算法工程师",
    )
    assert decision.detected_type == "social"
    assert decision.type_status == "FAIL"
```

- [ ] **Step 2: 写 2026—2027 届范围匹配测试**

```python
def test_year_range_includes_target_year():
    decision = classify_recruitment_scope(
        target=RecruitmentScope("campus", 2027),
        structured_fields={},
        channel_text="校园招聘",
        detail_text="面向2026至2027届毕业生",
        url="https://example.com/campus/job/1",
        title="AI应用工程师",
    )
    assert decision.year_status == "PASS"
    assert 2027 in decision.detected_years
```

- [ ] **Step 3: 写无届别时 REVIEW 测试**

```python
def test_campus_without_year_is_review():
    decision = classify_recruitment_scope(
        target=RecruitmentScope("campus", 2027),
        structured_fields={},
        channel_text="校园招聘",
        detail_text="面向应届毕业生",
        url="https://example.com/campus/job/1",
        title="研发工程师",
    )
    assert decision.type_status == "PASS"
    assert decision.year_status == "REVIEW"
```

- [ ] **Step 4: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_recruitment_classifier.py -v
```

- [ ] **Step 5: 实现分层证据判定**

```python
TYPE_ALIASES = {
    "campus": {"campus", "校园招聘", "校招", "应届生招聘", "graduate"},
    "internship": {"internship", "实习", "实习生", "日常实习"},
    "social": {"social", "社会招聘", "社招", "experienced"},
}


def classify_recruitment_scope(
    target,
    structured_fields,
    channel_text,
    detail_text,
    url,
    title,
):
    evidence_refs = []
    detected_type = _type_from_structured_fields(structured_fields)
    if detected_type:
        evidence_refs.append(
            f"structured:recruitment_type={detected_type}"
        )
        type_confidence = 0.98
    else:
        detected_type = _type_from_text(
            " ".join([channel_text, detail_text, url, title])
        )
        type_confidence = 0.75 if detected_type else 0.0

    if detected_type is None:
        detected_type = "unknown"
        type_status = "REVIEW"
    elif detected_type == target.recruitment_type:
        type_status = "PASS"
    else:
        type_status = "FAIL"

    years = sorted(
        set(
            _extract_graduation_years(channel_text)
            + _extract_graduation_years(detail_text)
            + _extract_graduation_years(
                str(structured_fields)
            )
        )
    )

    if target.recruitment_type == "social":
        year_status = "PASS"
    elif target.graduation_year in years:
        year_status = "PASS"
    elif years:
        year_status = "FAIL"
    else:
        year_status = "REVIEW"

    return RecruitmentScopeDecision(
        detected_type=detected_type,
        detected_years=years,
        type_status=type_status,
        year_status=year_status,
        confidence=type_confidence,
        evidence_refs=evidence_refs,
        reason=_build_scope_reason(
            detected_type,
            years,
            type_status,
            year_status,
        ),
    )
```

实现年份解析时覆盖：

```text
2027届
2026-2027届
2026 至 2027 届
2026/2027届
```

不能仅通过“应届生”推断具体年份。

- [ ] **Step 6: 将判定证据接入 `verify_evidence`**

`verify_evidence` 保留现有字段归一化，同时附加：

```python
candidate.scope_decision = classify_recruitment_scope(...)
candidate.evidence_refs.extend(
    candidate.scope_decision.evidence_refs
)
```

- [ ] **Step 7: 运行测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_recruitment_classifier.py `
  tests/unit/ -k evidence_verifier -v
```

Expected: PASS。

- [ ] **Step 8: 提交**

```powershell
git add job_discovery/scope/recruitment_classifier.py `
  job_discovery/scope/__init__.py `
  job_discovery/tools/evidence_verifier.py `
  tests/unit/job_discovery/test_recruitment_classifier.py
git commit -m "feat(job-discovery): classify recruitment type and cohort"
```

---

### Task 7: 实现学历解析和硬门槛

**Files:**
- Create: `job_discovery/scope/education_gate.py`
- Create: `job_discovery/matching/__init__.py`
- Create: `job_discovery/matching/eligibility.py`
- Test: `tests/unit/job_discovery/test_education_gate.py`
- Test: `tests/unit/job_discovery/test_eligibility.py`

**Interfaces:**
- Consumes: 用户学历、JD 学历文本、`RecruitmentScopeDecision`。
- Produces:
  - `parse_minimum_education(text: str) -> EducationRequirement`
  - `evaluate_education(user_education, requirement) -> GateDecision`
  - `evaluate_eligibility(...) -> EligibilityResult`

- [ ] **Step 1: 写硕士用户学历门槛测试**

```python
@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("本科及以上", "PASS"),
        ("硕士及以上", "PASS"),
        ("博士及以上", "FAIL"),
        ("博士优先，本科及以上", "PASS"),
        ("博士优先，硕士及以上", "PASS"),
        ("学历不限", "PASS"),
    ],
)
def test_master_user_education_gate(requirement, expected):
    parsed = parse_minimum_education(requirement)
    decision = evaluate_education("硕士", parsed)
    assert decision.status == expected
```

- [ ] **Step 2: 写未知学历 REVIEW 测试**

```python
def test_unknown_education_requirement_is_review():
    parsed = parse_minimum_education("具备良好学习能力")
    decision = evaluate_education("硕士", parsed)
    assert decision.status == "REVIEW"
```

- [ ] **Step 3: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_education_gate.py `
  tests/unit/job_discovery/test_eligibility.py -v
```

- [ ] **Step 4: 实现学历解析**

```python
from dataclasses import dataclass

EDUCATION_RANK = {
    "不限": 0,
    "大专": 1,
    "本科": 2,
    "硕士": 3,
    "博士": 4,
}


@dataclass(frozen=True)
class EducationRequirement:
    minimum: str | None
    preferred: list[str]
    source_text: str


@dataclass(frozen=True)
class GateDecision:
    status: str
    reason: str
    evidence_refs: list[str]


def parse_minimum_education(text: str) -> EducationRequirement:
    preferred = []
    if "博士优先" in text:
        preferred.append("博士")

    minimum = None
    for degree in ("博士", "硕士", "本科", "大专"):
        if f"{degree}及以上" in text or f"{degree}以上" in text:
            minimum = degree
            break

    if minimum is None and "学历不限" in text:
        minimum = "不限"

    if minimum is None:
        exact_candidates = [
            degree
            for degree in ("博士", "硕士", "本科", "大专")
            if degree in text and f"{degree}优先" not in text
        ]
        if len(exact_candidates) == 1:
            minimum = exact_candidates[0]

    return EducationRequirement(
        minimum=minimum,
        preferred=preferred,
        source_text=text,
    )


def evaluate_education(
    user_education: str | None,
    requirement: EducationRequirement,
) -> GateDecision:
    if user_education not in EDUCATION_RANK:
        return GateDecision(
            status="REVIEW",
            reason="user education is missing or unsupported",
            evidence_refs=[],
        )
    if requirement.minimum is None:
        return GateDecision(
            status="REVIEW",
            reason="minimum job education cannot be determined",
            evidence_refs=["jd:education_unknown"],
        )

    passed = (
        EDUCATION_RANK[user_education]
        >= EDUCATION_RANK[requirement.minimum]
    )
    return GateDecision(
        status="PASS" if passed else "FAIL",
        reason=(
            f"user={user_education}, "
            f"minimum={requirement.minimum}"
        ),
        evidence_refs=[
            f"jd:minimum_education={requirement.minimum}"
        ],
    )
```

- [ ] **Step 5: 实现统一 Eligibility Gate**

```python
@dataclass
class EligibilityResult:
    status: str
    recruitment_type: GateDecision
    graduation_year: GateDecision
    education: GateDecision
    evidence_refs: list[str]


def evaluate_eligibility(scope_decision, education_decision):
    type_gate = _decision_status_to_gate(
        scope_decision.type_status,
        scope_decision.reason,
        scope_decision.evidence_refs,
    )
    year_gate = _decision_status_to_gate(
        scope_decision.year_status,
        scope_decision.reason,
        scope_decision.evidence_refs,
    )

    statuses = {
        type_gate.status,
        year_gate.status,
        education_decision.status,
    }
    if "FAIL" in statuses:
        overall = "FAIL"
    elif "REVIEW" in statuses:
        overall = "REVIEW"
    else:
        overall = "PASS"

    return EligibilityResult(
        status=overall,
        recruitment_type=type_gate,
        graduation_year=year_gate,
        education=education_decision,
        evidence_refs=list(
            dict.fromkeys(
                type_gate.evidence_refs
                + year_gate.evidence_refs
                + education_decision.evidence_refs
            )
        ),
    )
```

- [ ] **Step 6: 运行测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_education_gate.py `
  tests/unit/job_discovery/test_eligibility.py -v
```

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add job_discovery/scope/education_gate.py `
  job_discovery/matching `
  tests/unit/job_discovery/test_education_gate.py `
  tests/unit/job_discovery/test_eligibility.py
git commit -m "feat(job-discovery): enforce recruitment and education gates"
```

---

### Task 8: 结构化并规范化完整 JD

**Files:**
- Create: `job_discovery/normalization/__init__.py`
- Create: `job_discovery/normalization/jd_parser.py`
- Create: `job_discovery/normalization/jd_normalizer.py`
- Modify: `job_discovery/tools/jd_extraction.py`
- Test: `tests/unit/job_discovery/test_jd_normalizer.py`

**Interfaces:**
- Consumes: `RawJobDetail.full_text`。
- Produces:
  - `parse_jd_sections(full_text: str) -> ParsedJDSections`
  - `normalize_jd(detail: RawJobDetail) -> NormalizedJD`

- [ ] **Step 1: 写非业务字段差异不改变核心哈希测试**

```python
def test_location_and_job_code_do_not_change_core_hash():
    first = normalize_text("""
岗位编号：A100
工作地点：北京
岗位职责：
1. 负责大模型应用开发
任职要求：
1. 熟悉Python和RAG
""")
    second = normalize_text("""
岗位编号：B900
工作地点：上海
岗位职责：
负责大模型应用开发。
任职要求：
熟悉 Python、RAG。
""")
    assert first.core_hash == second.core_hash
```

- [ ] **Step 2: 写职责不同必须产生不同哈希测试**

```python
def test_different_responsibilities_produce_different_hashes():
    ai_job = normalize_text("""
岗位职责：负责大模型应用开发
任职要求：熟悉Python
""")
    test_job = normalize_text("""
岗位职责：负责软件测试和质量保障
任职要求：熟悉Python
""")
    assert ai_job.core_hash != test_job.core_hash
```

- [ ] **Step 3: 写完整正文不受 `text_excerpt` 截断影响测试**

```python
def test_normalizer_uses_full_detail_text():
    detail = RawJobDetail(
        detail_url="https://example.com/job/1",
        full_text=("公司介绍" * 1000)
        + "\n岗位职责：负责AI Agent开发"
        + "\n任职要求：熟悉Python和RAG",
    )
    normalized = normalize_jd(detail)
    assert "AI Agent" in normalized.normalized_core_text
```

- [ ] **Step 4: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_jd_normalizer.py -v
```

- [ ] **Step 5: 实现职责和要求段解析**

```python
RESPONSIBILITY_HEADERS = (
    "岗位职责",
    "工作职责",
    "职位描述",
    "主要职责",
)
REQUIREMENT_HEADERS = (
    "任职要求",
    "岗位要求",
    "职位要求",
    "任职资格",
)


@dataclass
class ParsedJDSections:
    responsibilities: list[str]
    requirements: list[str]
    remaining_text: str


def parse_jd_sections(full_text: str) -> ParsedJDSections:
    cleaned = _normalize_line_breaks(full_text)
    responsibility_text = _extract_section(
        cleaned,
        RESPONSIBILITY_HEADERS,
        REQUIREMENT_HEADERS,
    )
    requirement_text = _extract_section(
        cleaned,
        REQUIREMENT_HEADERS,
        (),
    )
    return ParsedJDSections(
        responsibilities=_split_bullets(responsibility_text),
        requirements=_split_bullets(requirement_text),
        remaining_text=cleaned,
    )
```

- [ ] **Step 6: 实现核心规范化**

```python
import hashlib
import re

REMOVABLE_LINE_PATTERNS = (
    r"^岗位编号[:：].+$",
    r"^职位编号[:：].+$",
    r"^工作地点[:：].+$",
    r"^招聘人数[:：].+$",
    r"^发布时间[:：].+$",
    r"^招聘批次[:：].+$",
)


def _normalize_core_lines(lines: list[str]) -> str:
    normalized = []
    for line in lines:
        value = re.sub(r"\s+", "", line)
        value = re.sub(r"[，。；、,:：;]", "", value)
        if value:
            normalized.append(value.lower())
    return "\n".join(normalized)


def normalize_jd(detail: RawJobDetail) -> NormalizedJD:
    parsed = parse_jd_sections(detail.full_text)
    responsibilities = _remove_metadata_lines(
        parsed.responsibilities,
        REMOVABLE_LINE_PATTERNS,
    )
    requirements = _remove_metadata_lines(
        parsed.requirements,
        REMOVABLE_LINE_PATTERNS,
    )
    normalized_responsibilities = _normalize_core_lines(
        responsibilities
    )
    normalized_requirements = _normalize_core_lines(requirements)
    normalized_core_text = (
        normalized_responsibilities
        + "\n---requirements---\n"
        + normalized_requirements
    )
    core_hash = hashlib.sha256(
        normalized_core_text.encode("utf-8")
    ).hexdigest()

    return NormalizedJD(
        responsibilities=responsibilities,
        requirements=requirements,
        education_requirement=_extract_education(
            requirements
        ),
        major_requirements=_extract_majors(requirements),
        technical_skills=_extract_skills(
            responsibilities + requirements
        ),
        normalized_responsibilities=normalized_responsibilities,
        normalized_requirements=normalized_requirements,
        normalized_core_text=normalized_core_text,
        core_hash=core_hash,
        removable_metadata=_extract_removable_metadata(
            detail.full_text
        ),
    )
```

- [ ] **Step 7: 修改 `jd_extraction.py`**

- 列表页不再调用 `_split_multi_job_page` 作为主路径；
- DOM/API 列表记录必须逐对象提取；
- `_split_multi_job_page` 只保留为静态公告兜底；
- 删除“最多 2 个岗位段”的固定限制，改用可配置安全上限：

```python
MAX_FALLBACK_JOB_SEGMENTS = 200
```

- [ ] **Step 8: 运行测试和抽取回归**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_jd_normalizer.py `
  tests/unit/ -k "jd_extraction" -v
```

Expected: PASS。

- [ ] **Step 9: 提交**

```powershell
git add job_discovery/normalization `
  job_discovery/tools/jd_extraction.py `
  tests/unit/job_discovery/test_jd_normalizer.py
git commit -m "feat(job-discovery): normalize full JD core content"
```

---

### Task 9: 实现基于 JD 主体的业务岗位去重

**Files:**
- Create: `job_discovery/deduplication/canonical_job_deduplicator.py`
- Modify: `job_discovery/tools/candidate_packager.py`
- Test: `tests/unit/job_discovery/test_canonical_job_deduplicator.py`

**Interfaces:**
- Consumes: 同一公司下的 `NormalizedJD`、原始 listing/detail 记录。
- Produces:
  - `deduplicate_canonical_jobs(...) -> list[CanonicalJob]`
  - 新的 `canonical_job_key`
  - 保留现有 `idempotency_key` 作为原始记录幂等键

- [ ] **Step 1: 写相同 JD 不同地点合并测试**

```python
def test_same_jd_different_locations_are_merged():
    jobs = build_jobs(
        company="示例公司",
        entries=[
            ("AI应用工程师", ["北京"], jd_one()),
            ("AI应用工程师", ["上海"], jd_one()),
        ],
    )
    merged = deduplicate_canonical_jobs(jobs)
    assert len(merged) == 1
    assert merged[0].locations == ["上海", "北京"]
    assert merged[0].merged_record_count == 2
```

- [ ] **Step 2: 写标题相同但 JD 不同不合并测试**

```python
def test_same_title_different_jd_remains_separate():
    jobs = build_jobs(
        company="示例公司",
        entries=[
            ("算法工程师", ["北京"], ai_jd()),
            ("算法工程师", ["北京"], control_jd()),
        ],
    )
    merged = deduplicate_canonical_jobs(jobs)
    assert len(merged) == 2
```

- [ ] **Step 3: 写标题不同但 JD 相同合并测试**

```python
def test_different_titles_same_jd_are_merged():
    jobs = build_jobs(
        company="示例公司",
        entries=[
            ("大模型应用工程师", ["北京"], jd_one()),
            ("AI Agent研发工程师", ["上海"], jd_one()),
        ],
    )
    merged = deduplicate_canonical_jobs(jobs)
    assert len(merged) == 1
    assert set(merged[0].alternative_titles) == {
        "大模型应用工程师",
        "AI Agent研发工程师",
    }
```

- [ ] **Step 4: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_canonical_job_deduplicator.py -v
```

- [ ] **Step 5: 实现一级严格哈希合并**

```python
def canonical_job_key(company: str, jd: NormalizedJD) -> str:
    material = (
        _normalize_company(company)
        + "\n"
        + jd.normalized_responsibilities
        + "\n"
        + jd.normalized_requirements
    )
    return hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()
```

按 `normalized_company + core_hash` 分组，合并：

- `locations`
- `alternative_titles`
- `detail_urls`
- `apply_urls`
- `source_listing_urls`
- `source_record_ids`
- `source_job_codes`
- `evidence_refs`

所有列表必须稳定排序并去重。

- [ ] **Step 6: 实现二级高相似度合并**

第一版采用可解释的字符 n-gram Jaccard，不引入新模型依赖：

```python
def _char_ngrams(text: str, size: int = 3) -> set[str]:
    return {
        text[index:index + size]
        for index in range(max(0, len(text) - size + 1))
    }


def _jaccard(left: str, right: str) -> float:
    left_set = _char_ngrams(left)
    right_set = _char_ngrams(right)
    if not left_set and not right_set:
        return 1.0
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)
```

合并条件固定为：

```python
same_company
and responsibility_similarity >= 0.94
and requirement_similarity >= 0.94
and not has_critical_conflict(left, right)
```

`has_critical_conflict` 第一版检查：

- 最低学历冲突；
- 技术方向关键词集合完全不相交且双方都非空；
- 职责中分别出现互斥方向，例如“软件测试”与“大模型应用开发”。

相似度阈值作为模块常量，不放进 LLM Prompt。

- [ ] **Step 7: 修改 CandidatePackager 键语义**

保留：

```python
idempotency_key = sha256(
    company + title + location + apply_url + evidence_hash
)
```

但将其明确重命名或注释为：

```text
source_record_idempotency_key
```

新增：

```python
canonical_job_key = canonical_job_key(
    candidate.company,
    candidate.normalized_jd,
)
```

不得将地点、URL 或 `evidence_hash` 纳入 `canonical_job_key`。

- [ ] **Step 8: 运行测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_canonical_job_deduplicator.py `
  tests/unit/ -k candidate_packager -v
```

Expected: PASS。

- [ ] **Step 9: 提交**

```powershell
git add job_discovery/deduplication/canonical_job_deduplicator.py `
  job_discovery/tools/candidate_packager.py `
  tests/unit/job_discovery/test_canonical_job_deduplicator.py
git commit -m "feat(job-discovery): deduplicate jobs by normalized JD"
```

---

### Task 10: 实现相关岗位评分与阈值筛选

**Files:**
- Create: `job_discovery/matching/scorer.py`
- Test: `tests/unit/job_discovery/test_match_scorer.py`

**Interfaces:**
- Consumes: 通过硬门槛的 `CanonicalJob` 和用户偏好。
- Produces:
  - `score_job(job, preferences) -> JobMatchScore`
  - `filter_matched_jobs(jobs, minimum_score) -> list[ScoredJob]`

- [ ] **Step 1: 写权重和总分测试**

```python
def test_match_score_uses_required_weights():
    score = score_job(
        job=ai_application_job(),
        preferences=preferences(
            technical_directions=["AI应用开发"],
            positive_keywords=["RAG", "Python"],
            preferred_majors=["控制科学与工程"],
            preferred_locations=["沈阳"],
        ),
    )
    assert score.dimensions["technical_direction"].max_score == 35
    assert score.dimensions["keywords"].max_score == 35
    assert score.dimensions["major"].max_score == 15
    assert score.dimensions["location"].max_score == 15
    assert score.total_score <= 100
```

- [ ] **Step 2: 写专业和地点不匹配不硬淘汰测试**

```python
def test_major_and_location_mismatch_only_reduce_score():
    score = score_job(
        job=job(major="计算机", location="上海"),
        preferences=preferences(
            preferred_majors=["控制科学与工程"],
            preferred_locations=["沈阳"],
        ),
    )
    assert score.eligible is True
    assert score.dimensions["major"].score == 0
    assert score.dimensions["location"].score == 0
```

- [ ] **Step 3: 写负向关键词扣分测试**

```python
def test_negative_keywords_reduce_keyword_score():
    score = score_job(
        job=job(text="负责纯前端页面开发，熟悉Python"),
        preferences=preferences(
            positive_keywords=["Python"],
            negative_keywords=["纯前端"],
        ),
    )
    assert score.dimensions["keywords"].score < 20
```

- [ ] **Step 4: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_match_scorer.py -v
```

- [ ] **Step 5: 实现可解释评分结构**

```python
from dataclasses import dataclass


@dataclass
class MatchDimensionScore:
    score: float
    max_score: float
    evidence_refs: list[str]
    reason: str


@dataclass
class JobMatchScore:
    eligible: bool
    total_score: float
    dimensions: dict[str, MatchDimensionScore]
    evidence_refs: list[str]


WEIGHTS = {
    "technical_direction": 35.0,
    "keywords": 35.0,
    "major": 15.0,
    "location": 15.0,
}
```

实现规则：

- 技术方向：将用户方向与标题、职责、要求中的方向词匹配；命中率映射到 0–35。
- 关键词：正向关键词命中覆盖率映射到 0–35；每个负向关键词命中扣 8 分，最低为 0。
- 专业：明确包含目标专业或其可配置同义词得 15；“相关专业”得 8；明确不相关得 0；未知得 5。
- 地点：精确地点得 15；同一用户配置区域内的可接受城市得 10；远程得 8；不匹配得 0；未知得 5。
- 评分原因必须列出命中和未命中的具体词。
- 仅 `EligibilityResult.status == "PASS"` 的岗位进入自动返回列表；`REVIEW` 单独进入复核列表，不与正式匹配结果混合。

- [ ] **Step 6: 实现阈值过滤**

```python
def filter_matched_jobs(scored_jobs, minimum_score):
    return sorted(
        [
            item
            for item in scored_jobs
            if item.match_score.eligible
            and item.match_score.total_score >= minimum_score
        ],
        key=lambda item: (
            -item.match_score.total_score,
            item.job.company,
            item.job.canonical_title,
        ),
    )
```

- [ ] **Step 7: 运行测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_match_scorer.py -v
```

Expected: PASS。

- [ ] **Step 8: 提交**

```powershell
git add job_discovery/matching/scorer.py `
  tests/unit/job_discovery/test_match_scorer.py
git commit -m "feat(job-discovery): score all eligible jobs"
```

---

### Task 11: 将 PATH A 升级为可证明完整的 Certified Adapter

**Files:**
- Modify: `job_discovery/adapters/base.py`
- Modify: `job_discovery/adapters/alibaba_spa.py`
- Modify: `job_discovery/deepagents_runner.py`
- Test: `tests/unit/job_discovery/test_path_routing_v2.py`
- Modify: `tests/manual/test_strategy_router_live_smoke.py`

**Interfaces:**
- Consumes: `DiscoveryTaskInput`、`StrategyRecord`、`TrajectoryBuffer`。
- Produces:
  - `DomainAdapter.execute(...) -> CrawlExecutionResult`
  - Adapter 成功必须包含 `CrawlCoverage`

- [ ] **Step 1: 写 Adapter 无覆盖信息不得成功测试**

```python
def test_adapter_without_coverage_is_not_success():
    adapter = FakeAdapterWithoutCoverage()
    result = execute_strategy(adapter_strategy(), task(), adapter=adapter)
    assert result.status == "needs_manual_review"
    assert "coverage" in result.error
```

- [ ] **Step 2: 写 Alibaba API 分页完整计数测试**

```python
def test_alibaba_adapter_counts_every_api_page():
    adapter = AlibabaSPAAdapter(fetcher=fake_alibaba_fetcher(
        pages=[
            {"items": [1, 2], "total": 5},
            {"items": [3, 4], "total": 5},
            {"items": [5], "total": 5},
        ]
    ))
    result = adapter.execute(task(), strategy(), trajectory())
    assert result.coverage.raw_listing_count == 5
    assert result.coverage.expected_listing_count == 5
    assert result.coverage.coverage_complete is True
```

- [ ] **Step 3: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_path_routing_v2.py -v
```

- [ ] **Step 4: 修改 Adapter 抽象接口**

```python
from abc import ABC, abstractmethod
from job_discovery.crawling.crawl_executor import CrawlExecutionResult


class DomainAdapter(ABC):
    @abstractmethod
    def execute(
        self,
        task,
        strategy,
        trajectory,
    ) -> CrawlExecutionResult:
        raise NotImplementedError

    def validate(self, result: CrawlExecutionResult) -> None:
        if result.coverage is None:
            raise ValueError("adapter result missing coverage")
        decision = verify_coverage(result.coverage)
        if not decision.complete:
            raise IncompleteCrawlError(decision.reason)
```

- [ ] **Step 5: 修改 Alibaba Adapter**

- 按 API 的 `total`、`pageSize`、`pageNo`、`hasMore` 或真实字段遍历全部页；
- 禁止使用固定最大页数作为正常停止条件；
- 每页记录 XHR URL、页码/cursor 和 item count；
- 详情资源去重后抓取所有唯一详情；
- 输出 `CrawlCoverage`；
- 若 API 未提供总数，必须有 `hasMore=false` 或空页等正向完成证据；
- 继续使用现有中文编码修复、证据注入和安全门禁。

- [ ] **Step 6: 让 PATH A 暂时返回统一 CrawlExecutionResult**

在 Task 14 接入统一后处理前，PATH A 的执行分支只负责返回并校验：

```python
crawl_result = adapter.execute(
    task,
    strategy,
    trajectory,
)
adapter.validate(crawl_result)
return crawl_result
```

相应测试断言 PATH A 返回值包含完整的 `raw_listings`、`raw_details` 和 `coverage`；不得在本任务中重复执行旧的 extract/verify/package 后处理。

- [ ] **Step 7: 运行单元测试和 Alibaba 手工烟测**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_path_routing_v2.py `
  tests/unit/ -k "alibaba_spa" -v

.\.venv\Scripts\python.exe -u `
  tests/manual/test_strategy_router_live_smoke.py
```

Expected: 单元测试 PASS；烟测日志显示每页计数、总列表数、详情数和 `coverage_complete`。

- [ ] **Step 8: 提交**

```powershell
git add job_discovery/adapters/base.py `
  job_discovery/adapters/alibaba_spa.py `
  job_discovery/deepagents_runner.py `
  tests/unit/job_discovery/test_path_routing_v2.py `
  tests/manual/test_strategy_router_live_smoke.py
git commit -m "refactor(job-discovery): certify adapter crawl coverage"
```

---

### Task 12: 将 PATH B 升级为 SnapshotPlan + CrawlPlan 双执行器

**Files:**
- Modify: `job_discovery/strategy/snapshot_executor.py`
- Modify: `job_discovery/strategy/strategy_router.py`
- Test: `tests/unit/job_discovery/test_path_routing_v2.py`
- Test: `tests/unit/job_discovery/test_crawl_executor.py`

**Interfaces:**
- Consumes: `plan_yaml` 顶层 `plan_type`。
- Produces:
  - SnapshotPlan 继续走原顺序回放；
  - CrawlPlan 调用 `CrawlExecutor`；
  - 两类计划返回统一的 `DiscoveryRunResult`。

- [ ] **Step 1: 写计划类型路由测试**

```python
def test_snapshot_plan_uses_snapshot_executor():
    result = execute_plan(snapshot_plan_yaml(), task())
    assert result.execution_path == "snapshot_plan"

def test_crawl_plan_uses_crawl_executor():
    result = execute_plan(crawl_plan_yaml(), task())
    assert result.execution_path == "crawl_plan"
```

- [ ] **Step 2: 写 CrawlPlan 不完整时触发 fallback 上下文测试**

```python
def test_incomplete_crawl_plan_returns_repair_context():
    result = execute_plan(
        crawl_plan_yaml(),
        task(),
        driver=driver_with_selector_failure(),
    )
    assert result.needs_supervisor_fallback is True
    assert result.snapshot_context["failed_step"]["tool"] == "crawl_executor"
    assert result.snapshot_context["checkpoint"] is not None
```

- [ ] **Step 3: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_path_routing_v2.py `
  tests/unit/job_discovery/test_crawl_executor.py -v
```

- [ ] **Step 4: 修改 SnapshotExecutor 分派**

```python
raw_plan = yaml.safe_load(strategy.plan_yaml)
plan_type = raw_plan.get("plan_type", "snapshot_plan")

if plan_type == "snapshot_plan":
    return self._execute_snapshot_plan(
        task,
        strategy,
        trajectory,
    )

if plan_type == "crawl_plan":
    plan = CrawlPlan.from_yaml(strategy.plan_yaml)
    crawl_result = CrawlExecutor(
        driver=self.browser_driver,
        trajectory=trajectory,
    ).execute(
        plan=plan,
        task=task,
        checkpoint=_checkpoint_from_snapshot_context(
            snapshot_context
        ),
    )
    if not crawl_result.coverage.coverage_complete:
        return SnapshotExecutionResult(
            needs_supervisor_fallback=True,
            snapshot_context=_build_crawl_repair_context(
                strategy=strategy,
                crawl_result=crawl_result,
                trajectory=trajectory,
            ),
        )
    return run_post_crawl_pipeline(task, crawl_result)

raise ValueError(f"unsupported plan_type: {plan_type}")
```

- [ ] **Step 5: 保持微信 SnapshotPlan 兼容**

确认以下行为不变：

- `triage_link → fetch_wechat_article → extract_jd_candidates`；
- 被验证墙拦截时跳过 Supervisor 直接人工复核；
- 单页公告可以成功且不要求分页页数；
- Snapshot 自动证据仍保留。

- [ ] **Step 6: 运行测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_path_routing_v2.py `
  tests/unit/ -k "snapshot_executor or wechat" -v
```

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add job_discovery/strategy/snapshot_executor.py `
  job_discovery/strategy/strategy_router.py `
  tests/unit/job_discovery/test_path_routing_v2.py `
  tests/unit/job_discovery/test_crawl_executor.py
git commit -m "feat(job-discovery): execute snapshot and crawl plans"
```

---

### Task 13: 将 PATH C 改成 CrawlPlan 生成与修复 Agent

**Files:**
- Create: `job_discovery/prompts/crawl_plan_agent.txt`
- Modify: `job_discovery/deepagents_runner.py`
- Modify: `job_discovery/prompts/supervisor_base.txt`
- Modify: `job_discovery/prompts/supervisor_clean_start.txt`
- Modify: `job_discovery/prompts/supervisor_snapshot_fallback.txt`
- Modify: `job_discovery/strategy/error_classifier.py`
- Test: `tests/unit/job_discovery/test_path_routing_v2.py`

**Interfaces:**
- Consumes: 未知网站起始 URL 或 PATH A/B 的失败轨迹和 checkpoint。
- Produces:
  - `build_crawl_plan_agent(...)`
  - `generate_crawl_plan(...) -> CrawlPlan`
  - `repair_crawl_plan(...) -> CrawlPlan`
  - PATH C 不直接返回最终岗位集合

- [ ] **Step 1: 写 PATH C 不能直接成功返回岗位的测试**

```python
def test_path_c_returns_plan_not_final_jobs():
    agent = FakePlanningAgent(
        crawl_plan=crawl_plan_yaml()
    )
    result = run_unknown_site(task(), planning_agent=agent)
    assert result.generated_plan is not None
    assert result.candidates == []
    assert result.next_execution_path == "crawl_plan"
```

- [ ] **Step 2: 写结构错误才进入 PATH C 的测试**

```python
@pytest.mark.parametrize(
    ("error_type", "expected_next"),
    [
        ("structure_error", "planning_agent"),
        ("blocked", "manual_review"),
        ("transient", "resume_same_executor"),
        ("data_error", "partial_success"),
    ],
)
def test_failure_routing(error_type, expected_next):
    assert classify_next_action(error_type) == expected_next
```

- [ ] **Step 3: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_path_routing_v2.py -v
```

- [ ] **Step 4: 编写 PATH C 系统提示词**

`crawl_plan_agent.txt` 必须包含以下不可违背的规则：

```text
Your only deliverable is a valid CrawlPlan.

You may inspect a small number of pages to identify:
- recruitment entry and selected scope
- listing item structure
- pagination mechanism and positive terminal condition
- detail URL and JD field structure

You must not:
- enumerate every page
- return discovered jobs as a completed result
- declare crawl completion
- bypass login, captcha, slider, QR login, or anti-bot controls
- use tool-call budget as a pagination limit

For infinite scroll, emit a plan only when a positive terminal signal exists:
hasMore=false, totalCount reached, nextCursor=null, or an explicit terminal DOM marker.
Otherwise request manual review.
```

- [ ] **Step 5: 在 `deepagents_runner.py` 中新增计划 Agent 构建器**

```python
def build_crawl_plan_agent(
    settings,
    model,
    snapshot_context=None,
):
    tools = [
        open_rendered_url,
        read_dom,
        extract_links,
        extract_rendered_job_evidence,
    ]
    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=_render_crawl_plan_prompt(
            snapshot_context
        ),
        response_format=CrawlPlanPydantic,
    )
```

计划 Agent 不暴露：

- `package_candidates`
- `verify_evidence`
- `extract_jd_candidates`
- `finish_with_manual_review` 之外的最终结果工具

- [ ] **Step 6: 修改 Supervisor**

Supervisor 只负责编排：

```python
if route.requires_plan_generation:
    crawl_plan = generate_crawl_plan(...)
    return execute_crawl_plan_via_path_b(
        crawl_plan,
        checkpoint=snapshot_context.get("checkpoint"),
    )
```

删除或停止使用“PATH C 通过 `run_web_navigation` 直接产出最终岗位”的主流程。保留 `run_web_navigation` 作为：

- 计划识别辅助；
- SnapshotPlan 单页读取兼容；
- 调试工具。

- [ ] **Step 7: 扩展错误分类**

新增或明确：

```python
STRUCTURE_ERRORS = {
    "selector_not_found",
    "pagination_shape_changed",
    "detail_schema_changed",
    "unexpected_iframe",
    "api_payload_changed",
}

BLOCKED_ERRORS = {
    "captcha",
    "slider",
    "login_required",
    "qr_login_required",
    "anti_bot",
}
```

路由规则：

```python
structure_error -> PATH C repair -> PATH B
transient -> same path resume
blocked -> needs_manual_review
completion_unverified -> needs_manual_review
data_error -> partial_success
```

- [ ] **Step 8: 运行测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_path_routing_v2.py `
  tests/unit/ -k "error_classifier or loop_guardian" -v
```

Expected: PASS。

- [ ] **Step 9: 提交**

```powershell
git add job_discovery/prompts/crawl_plan_agent.txt `
  job_discovery/deepagents_runner.py `
  job_discovery/prompts/supervisor_base.txt `
  job_discovery/prompts/supervisor_clean_start.txt `
  job_discovery/prompts/supervisor_snapshot_fallback.txt `
  job_discovery/strategy/error_classifier.py `
  tests/unit/job_discovery/test_path_routing_v2.py
git commit -m "refactor(job-discovery): make path c a crawl planning agent"
```

---

### Task 14: 集成统一的 Post-Crawl Pipeline

**Files:**
- Create: `job_discovery/post_crawl_pipeline.py`
- Modify: `job_discovery/deepagents_runner.py`
- Modify: `job_discovery/worker.py`
- Modify: `job_discovery/result_contract.py`
- Test: `tests/integration/job_discovery/test_complete_crawl_pipeline.py`

**Interfaces:**
- Consumes: `CrawlExecutionResult` 和 `DiscoveryTaskInput`。
- Produces:
  - `run_post_crawl_pipeline(task, crawl_result) -> DiscoveryRunResult`

- [ ] **Step 1: 写完整端到端成功测试**

```python
def test_complete_pipeline_filters_deduplicates_and_scores():
    task = task_input(
        recruitment_type="campus",
        graduation_year=2027,
        user_education="硕士",
        technical_directions=["AI应用开发"],
        positive_keywords=["RAG", "Python"],
        preferred_majors=["控制科学与工程"],
        preferred_locations=["沈阳"],
        minimum_match_score=60,
    )
    crawl_result = fixture_complete_crawl_result(
        details=[
            campus_2027_ai_job_beijing(),
            campus_2027_ai_job_shanghai_same_jd(),
            social_ai_job(),
            campus_2028_ai_job(),
            campus_2027_phd_only_job(),
            campus_2027_sales_job(),
        ]
    )

    result = run_post_crawl_pipeline(task, crawl_result)

    assert result.status == "succeeded"
    assert result.raw_listing_count == 6
    assert result.canonical_job_count == 5
    assert result.eligible_job_count == 2
    assert result.matched_job_count == 1
    assert len(result.candidates) == 1
    assert set(result.candidates[0].locations) == {"北京", "上海"}
```

- [ ] **Step 2: 写分页不完整时不得运行正式评分结果测试**

```python
def test_incomplete_crawl_is_saved_but_not_presented_complete():
    crawl_result = fixture_incomplete_crawl_result()
    result = run_post_crawl_pipeline(task_input(), crawl_result)
    assert result.status in {
        "partial_success",
        "needs_manual_review",
    }
    assert result.coverage.coverage_complete is False
    assert result.scoring_completed is False
```

- [ ] **Step 3: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/integration/job_discovery/test_complete_crawl_pipeline.py -v
```

- [ ] **Step 4: 实现统一后处理**

```python
def run_post_crawl_pipeline(task, crawl_result):
    coverage_decision = verify_coverage(crawl_result.coverage)
    if not coverage_decision.complete:
        return DiscoveryRunResult(
            status=coverage_decision.status,
            candidates=[],
            evidence=_collect_evidence(crawl_result),
            coverage=crawl_result.coverage,
            raw_listing_count=len(
                crawl_result.raw_listings
            ),
            error=coverage_decision.reason,
        )

    classified = []
    for detail in crawl_result.raw_details:
        scope_decision = classify_recruitment_scope(
            target=task.recruitment_scope,
            structured_fields=detail.structured_fields,
            channel_text=detail.channel_text,
            detail_text=detail.full_text,
            url=detail.detail_url,
            title=detail.title or "",
        )
        normalized_jd = normalize_jd(detail)
        education = evaluate_education(
            task.user_education,
            parse_minimum_education(
                normalized_jd.education_requirement or ""
            ),
        )
        eligibility = evaluate_eligibility(
            scope_decision,
            education,
        )
        classified.append(
            ClassifiedJob(
                detail=detail,
                scope_decision=scope_decision,
                normalized_jd=normalized_jd,
                eligibility=eligibility,
            )
        )

    canonical_jobs = deduplicate_canonical_jobs(
        classified
    )
    eligible_jobs = [
        job for job in canonical_jobs
        if job.eligibility.status == "PASS"
    ]
    review_jobs = [
        job for job in canonical_jobs
        if job.eligibility.status == "REVIEW"
    ]

    scored_jobs = [
        score_job(job, task)
        for job in eligible_jobs
    ]
    matched_jobs = filter_matched_jobs(
        scored_jobs,
        task.minimum_match_score,
    )

    return DiscoveryRunResult(
        status="succeeded",
        candidates=[
            package_scored_job(item)
            for item in matched_jobs
        ],
        review_candidates=[
            package_review_job(item)
            for item in review_jobs
        ],
        evidence=_collect_evidence(crawl_result),
        coverage=crawl_result.coverage,
        raw_listing_count=len(
            crawl_result.raw_listings
        ),
        canonical_job_count=len(canonical_jobs),
        eligible_job_count=len(eligible_jobs),
        matched_job_count=len(matched_jobs),
        scope_filter_completed=True,
        dedup_completed=True,
        scoring_completed=True,
    )
```

- [ ] **Step 5: 修改 Worker 状态落库**

```python
if result.status == "partial_success":
    persist_partial_records(
        task_id=task.id,
        coverage=result.coverage,
        evidence=result.evidence,
    )

if result.status == "needs_manual_review":
    persist_manual_review_context(
        task_id=task.id,
        coverage=result.coverage,
        error=result.error,
        trajectory=trajectory.to_dict(),
    )
```

继续使用现有任务状态机，不新增状态。

- [ ] **Step 6: 运行集成测试与全量单元测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/integration/job_discovery/test_complete_crawl_pipeline.py `
  tests/unit/ -k job_discovery -v
```

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add job_discovery/post_crawl_pipeline.py `
  job_discovery/deepagents_runner.py `
  job_discovery/worker.py `
  job_discovery/result_contract.py `
  tests/integration/job_discovery/test_complete_crawl_pipeline.py
git commit -m "feat(job-discovery): integrate full post-crawl pipeline"
```

---

### Task 15: 增加恢复、审计指标和防回归门禁

**Files:**
- Modify: `job_discovery/strategy/trajectory_buffer.py`
- Modify: `job_discovery/strategy/trajectory_store.py`
- Modify: `job_discovery/strategy/trajectory_annotator.py`
- Modify: `job_discovery/worker.py`
- Test: `tests/unit/job_discovery/test_crawl_executor.py`
- Test: `tests/integration/job_discovery/test_complete_crawl_pipeline.py`

**Interfaces:**
- Consumes: `CrawlCoverage`、checkpoint、各阶段计数。
- Produces:
  - 可续跑任务上下文；
  - 抓取完整性审计指标；
  - 失败后不重复抓取已成功详情页。

- [ ] **Step 1: 写 checkpoint 恢复测试**

```python
def test_resume_skips_completed_pages_and_details():
    first = execute_until_failure(
        failure_page=6,
        total_pages=10,
    )
    resumed = resume_from_checkpoint(first.checkpoint)
    assert resumed.driver.visited_pages == [6, 7, 8, 9, 10]
    assert resumed.driver.refetched_detail_keys == []
    assert resumed.coverage.visited_page_count == 10
```

- [ ] **Step 2: 写审计指标存在测试**

```python
def test_result_exposes_crawl_audit_counts():
    result = complete_pipeline_result()
    assert result.coverage.raw_listing_count > 0
    assert result.coverage.unique_listing_count > 0
    assert result.coverage.total_detail_count > 0
    assert result.canonical_job_count >= result.matched_job_count
```

- [ ] **Step 3: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_crawl_executor.py `
  tests/integration/job_discovery/test_complete_crawl_pipeline.py -v
```

- [ ] **Step 4: 扩展轨迹记录字段**

每次分页记录：

```python
trajectory.record_step(
    tool="crawl_page",
    status="success",
    params={
        "pagination_type": pagination_type,
        "page_key": page_key,
        "cursor": cursor,
    },
    result={
        "item_count": item_count,
        "raw_listing_count": raw_listing_count,
        "unique_listing_count": unique_listing_count,
        "terminal_evidence": terminal_evidence,
    },
)
```

每次详情记录：

```python
trajectory.record_step(
    tool="fetch_job_detail",
    status="success",
    params={
        "detail_resource_key": resource_key,
        "url": detail_url,
    },
    result={
        "content_hash": content_hash,
        "text_length": len(full_text),
    },
)
```

不得把完整 JD 文本写入轨迹；完整正文继续写入现有证据存储或结果对象。

- [ ] **Step 5: 扩展 SnapshotContext**

```python
{
    "source": "...",
    "strategy_id": 1,
    "completed_steps": [...],
    "failed_step": {...},
    "checkpoint": {
        "pagination_type": "api_cursor",
        "next_cursor": "c6",
        "visited_page_keys": [...],
        "collected_source_record_keys": [...],
        "fetched_detail_resource_keys": [...]
    },
    "coverage": {...}
}
```

- [ ] **Step 6: 运行测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_crawl_executor.py `
  tests/integration/job_discovery/test_complete_crawl_pipeline.py `
  tests/unit/ -k trajectory -v
```

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add job_discovery/strategy/trajectory_buffer.py `
  job_discovery/strategy/trajectory_store.py `
  job_discovery/strategy/trajectory_annotator.py `
  job_discovery/worker.py `
  tests/unit/job_discovery/test_crawl_executor.py `
  tests/integration/job_discovery/test_complete_crawl_pipeline.py
git commit -m "feat(job-discovery): persist crawl checkpoints and audit metrics"
```

---

### Task 16: 更新文档、手工烟测和发布门禁

**Files:**
- Modify: `CLAUDE.md`
- Modify: `tests/manual/test_strategy_router_live_smoke.py`
- Create: `tests/manual/test_complete_crawl_live.py`
- Modify: `tests/manual/test_adapter_failure_takeover.py`

**Interfaces:**
- Consumes: 完成后的 A/B/C 路由和统一结果契约。
- Produces: 可审计的模块文档和发布检查清单。

- [ ] **Step 1: 重写 CLAUDE.md 中的架构图**

替换为：

```text
PATH A — Certified Adapter
    完整执行专用网站抓取，输出 CrawlCoverage

PATH B — Deterministic Plan Executor
    SnapshotPlan: 单页或固定步骤内容
    CrawlPlan: 多页列表、全部详情、checkpoint、完整性证明

PATH C — Crawl Planning and Repair Agent
    识别网站结构、生成或修复 CrawlPlan
    不直接输出完整岗位结果
```

- [ ] **Step 2: 在 CLAUDE.md 中增加系统不变量**

写入以下十条：

1. 发现岗位不等于完整抓取。
2. 只有 `coverage_complete=true` 才能返回完整结果。
3. Agent 无权自行声明抓取结束。
4. 招聘类型、届别和学历不允许被评分补偿。
5. 列表记录、详情资源和业务岗位使用不同去重键。
6. `canonical_job_key` 不包含地点、跟踪参数或 `evidence_hash`。
7. 相同 JD 多地区合并后保留所有地区和来源。
8. 部分抓取结果保存但不得伪装为完整结果。
9. 所有判定、过滤、合并和评分结论携带证据引用。
10. 无限滚动缺少正向终止证据时必须人工复核。

- [ ] **Step 3: 新增全量抓取手工烟测**

`test_complete_crawl_live.py` 接受一个测试 URL，通过环境变量选择招聘范围：

```powershell
$env:JOB_DISCOVERY_TEST_URL="https://example.com/campus"
$env:JOB_DISCOVERY_RECRUITMENT_TYPE="campus"
$env:JOB_DISCOVERY_GRADUATION_YEAR="2027"

.\.venv\Scripts\python.exe -u `
  tests/manual/test_complete_crawl_live.py
```

输出必须包含：

```text
execution_path
pagination_type
expected_page_count
visited_page_count
expected_listing_count
raw_listing_count
unique_listing_count
total_detail_count
fetched_detail_count
failed_detail_count
coverage_complete
canonical_job_count
eligible_job_count
matched_job_count
```

- [ ] **Step 4: 执行完整测试矩阵**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/ -k job_discovery -v

.\.venv\Scripts\python.exe -m pytest `
  tests/integration/job_discovery/ -v

.\.venv\Scripts\python.exe tests/unit/test_loop_guardian.py

.\.venv\Scripts\python.exe -u `
  tests/manual/test_strategy_router_live_smoke.py

.\.venv\Scripts\python.exe -u `
  tests/manual/test_adapter_failure_takeover.py

.\.venv\Scripts\python.exe -u `
  tests/manual/test_complete_crawl_live.py
```

Expected:

- 所有自动化测试 PASS；
- PATH A 输出完整覆盖信息；
- PATH B 可执行 SnapshotPlan 与 CrawlPlan；
- PATH C 输出 CrawlPlan 后回到 PATH B；
- 任何不完整任务均不是 `succeeded`；
- 零匹配岗位但抓取完整时为 `succeeded`；
- 验证码或登录墙进入 `needs_manual_review`。

- [ ] **Step 5: 执行静态检查**

使用项目已经配置的静态检查命令；若项目未配置专用命令，至少执行：

```powershell
.\.venv\Scripts\python.exe -m compileall job_discovery
```

Expected: 无语法错误。

- [ ] **Step 6: 最终提交**

```powershell
git add CLAUDE.md tests/manual
git commit -m "docs(job-discovery): document complete crawl architecture"
```

---

## 3. 推荐实施顺序和阶段门禁

### 阶段一：契约与完整性基础

包含 Task 1–4。

完成门禁：

- 新类型可稳定序列化；
- 结果不变量不再依赖“候选数大于 0”；
- 六类分页均有明确完成条件；
- 检查点可序列化；
- 尚未切换生产路由。

### 阶段二：确定性全量抓取

包含 Task 5、11、12。

完成门禁：

- 一个 10 页测试站点可以抓完全部页；
- 相同详情资源只请求一次；
- PATH A/B 均输出 `CrawlCoverage`；
- SnapshotPlan 的微信和单页公告行为不回归。

### 阶段三：范围过滤、JD 去重和评分

包含 Task 6–10、14。

完成门禁：

- 2027 校招不会混入社招或 2028 校招；
- 硕士用户不会收到博士最低要求岗位；
- 相同 JD 多地区只返回一个岗位；
- 专业、地点不匹配只扣分；
- 返回所有达到阈值的岗位，不截断 Top-K。

### 阶段四：PATH C 改造与恢复能力

包含 Task 13、15。

完成门禁：

- PATH C 不再直接输出最终岗位；
- 未知网站流程为 C 生成计划 → B 完整执行；
- 结构错误由 C 修复计划；
- 临时错误从 checkpoint 恢复；
- blocked 错误直接人工复核。

### 阶段五：文档和发布

包含 Task 16。

完成门禁：

- 全量自动化测试通过；
- 至少一个 PATH A 网站、一个 CrawlPlan 网站和一个 SnapshotPlan 来源完成烟测；
- 所有成功结果都能展示完整性证据；
- 所有不完整结果都不会被标成完整。

---

## 4. 发布与回滚策略

采用逐路径切换，不一次性替换所有网站：

1. 先在测试环境启用新的结果契约和覆盖验证，但旧 PATH C 仍不进入正式成功结果。
2. 选择一个已有 Adapter 的网站完成 PATH A 改造。
3. 选择一个页码型校招网站完成 CrawlPlan/PATH B 验证。
4. 选择一个未知网站验证 PATH C 生成计划后回到 PATH B。
5. 保持微信 SnapshotPlan 不变，确认没有回归。
6. 每个站点只有在三次连续全量烟测中，列表总数、详情总数和完成证据一致后，才将其策略视为稳定。
7. 路径出现结构错误时，只将对应策略状态降级；不回滚已经稳定的其他网站。
8. 回滚时恢复原策略记录或旧 Adapter，但结果契约继续阻止不完整结果进入 `succeeded`，避免回滚后重新暴露“部分结果伪装完整”的问题。

---

## 5. 实施完成定义

只有同时满足以下条件，才可声明本次重构完成：

- PATH A、PATH B、PATH C 的新职责与文档一致。
- 所有多页岗位站点由确定性代码完成全量遍历。
- 完整性验证包含正向终止证据，而不是仅依赖“没有新增岗位”。
- 招聘类型、届别、学历硬门槛全部生效。
- 专业和地点仅影响分数。
- 相同公司、相同 JD 主体的多地区岗位已合并。
- 详情、来源、地区和证据没有在合并过程中丢失。
- 不再执行 Top-K 截断。
- 任务可保存部分结果和 checkpoint，但不会将其展示为完整成功。
- 完整抓取后没有匹配岗位时，结果仍然是成功且 `matched_job_count=0`。
- 自动化测试、集成测试和三类现场烟测全部通过。
