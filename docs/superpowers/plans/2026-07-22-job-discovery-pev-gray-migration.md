# Job Discovery PEV Gray Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Step1 已完成的核心契约之上，把 PATH B 升级为可验证、可续跑的确定性 CrawlExecutor，把 PATH C 缩减为 CrawlPlan 生成/修复 Agent，并按 Moka → 飞书 → 汇川 → 小红书的顺序灰度迁移；微信保留 SnapshotPlan，北森鉴权墙继续人工复核，旧 PATH C 仅作为 `coverage-unverified` 兜底。

**Architecture:** Planner 只识别页面结构并产生 `CrawlPlan`；PATH B 的确定性 Executor 负责完整分页、列表去重、详情队列、checkpoint 和原始证据；`CoverageVerifier` 是唯一可以证明完整性的组件。已知站点通过 PATH A 的 adapter/driver 向同一个 Executor 提供确定性站点能力，未知站点由 PATH C 生成或修复计划后回到 PATH B 执行。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、dataclass、PyYAML、Playwright Sync API、LangGraph/Deep Agents、pytest、Ruff、SHA-256。

## 已完成基线

- Step1 已由提交 `e65e2c6 feat(job-discovery): add full-crawl domain contracts and CoverageVerifier` 完成。
- 已存在：
  - `backend/app/services/job_discovery/schemas.py`
    - `PaginationType`
    - `RecruitmentScope`
    - `CrawlCoverage`
    - `RawJobListing`
    - `RawJobDetail`
    - `DiscoveryRunResult.coverage`
  - `backend/app/services/job_discovery/crawling/crawl_plan.py`
    - `CrawlPlan.from_yaml`
    - `CrawlPlan.validate_security`
  - `backend/app/services/job_discovery/crawling/coverage.py`
    - `CoverageDecision`
    - `verify_coverage`
  - `tests/unit/job_discovery/test_crawl_schemas.py`
  - `tests/unit/job_discovery/test_coverage.py`
- 本计划不得修改 `result_contract.enforce_result_invariants` 的全局语义。
- 当前工作树另有未提交的用户改动；执行每个任务前必须运行 `git status --short`，只暂存任务文件，不能覆盖或顺带提交无关改动。

## Global Constraints

- `tools/jd_extraction.py` 和 `tools/evidence_verifier.py` 保持冻结；可以调用，不能修改。
- Planner 不返回岗位，不枚举全部页，不宣称 crawl complete。
- Executor 和 Verifier 不调用 LLM。
- 只有 `verify_coverage(result.coverage).complete is True` 的 PEV 结果可以标记为 coverage-verified `succeeded`。
- `coverage is None` 明确表示 legacy `coverage-unverified`，不能伪装为完整抓取。
- 固定页数、工具调用预算、递归上限和“连续无新增”都不能作为正常完成证据。
- 正向完成证据只接受：已访问全部编号页、next disabled/不存在且页面计数一致、`hasMore=false`、`nextCursor=null`、`offset >= totalCount`、明确 terminal DOM marker、合法 single-page 证明。
- 列表记录、详情资源、业务岗位分别去重；三种 key 不得复用。
- `normalize_detail_url` 只移除明确跟踪参数，必须保留 hash route 和可能改变岗位内容的业务参数。
- 每个唯一详情资源只请求一次；多地区 listing 指向同一详情时合并 locations/evidence，但不能丢失来源 URL。
- 登录、验证码、滑块、扫码、反爬和需要鉴权的内部 API 一律停止并返回 `needs_manual_review`。
- 学生仍只能读取 `verified` JobPosting；PEV 改造不能绕过现有管理员审核和安全门。
- 所有新增行为使用 TDD：先写失败测试，确认失败，再最小实现，再跑回归。
- 每个 Task 独立提交；禁止 `git add .`。

## 文件结构锁定

### 新建

```text
backend/app/services/job_discovery/
├── crawling/
│   ├── checkpoint.py              # 可序列化 CrawlCheckpoint
│   ├── driver.py                  # CrawlDriver Protocol + ListingPage
│   ├── pagination.py              # 指纹、循环检测、正向终止状态机
│   ├── playwright_driver.py       # 执行 Planner 产出的通用 DOM/API CrawlPlan
│   └── crawl_executor.py          # URL/key 归一化、列表/详情编排
├── adapters/
│   ├── complete_crawl_base.py     # PATH A V2 adapter 基类
│   ├── moka.py                    # Moka driver/adapter
│   ├── feishu.py                  # 飞书招聘 driver/adapter
│   ├── inovance.py                # 汇川 driver/adapter
│   └── xiaohongshu.py             # 小红书 API-cursor driver/adapter
├── planning/
│   ├── __init__.py
│   └── crawl_plan_agent.py        # PATH C 计划生成/修复
├── prompts/
│   └── crawl_plan_agent.txt
├── strategy/
│   └── deadline.py                # Snapshot 工具硬 deadline
└── post_crawl_pipeline.py         # raw crawl → verified candidates/result
```

### 修改

```text
backend/app/config.py
backend/app/services/job_discovery/schemas.py
backend/app/services/job_discovery/crawling/crawl_plan.py
backend/app/services/job_discovery/crawling/coverage.py
backend/app/services/job_discovery/adapters/__init__.py
backend/app/services/job_discovery/strategy/snapshot_executor.py
backend/app/services/job_discovery/strategy/strategy_store.py
backend/app/services/job_discovery/strategy/error_classifier.py
backend/app/services/job_discovery/worker.py
backend/app/services/job_discovery/deepagents_runner.py
scripts/seed_strategies.py
backend/app/services/job_discovery/README.md
backend/app/services/job_discovery/CLAUDE.md
docs/job-discovery-agent-workflow.md
docs/job-discovery-agent-operations.md
```

### 新建测试

```text
tests/unit/job_discovery/test_checkpoint.py
tests/unit/job_discovery/test_pagination.py
tests/unit/job_discovery/test_crawl_executor.py
tests/unit/job_discovery/test_playwright_crawl_driver.py
tests/unit/job_discovery/test_post_crawl_pipeline.py
tests/unit/job_discovery/test_path_b_routing.py
tests/unit/job_discovery/test_crawl_plan_agent.py
tests/unit/job_discovery/test_moka_adapter.py
tests/unit/job_discovery/test_feishu_adapter.py
tests/unit/job_discovery/test_inovance_adapter.py
tests/unit/job_discovery/test_xiaohongshu_adapter.py
tests/unit/job_discovery/test_wechat_deadline.py
tests/unit/job_discovery/test_blocked_site_policy.py
tests/integration/job_discovery/test_pev_worker_routing.py
tests/integration/job_discovery/test_pev_site_fixtures.py
tests/manual/probe_pev_site.py
tests/manual/test_pev_live_smoke.py
```

---

## Task 2.1: Crawl checkpoint、driver 协议与分页状态机

**Files:**
- Create: `backend/app/services/job_discovery/crawling/checkpoint.py`
- Create: `backend/app/services/job_discovery/crawling/driver.py`
- Create: `backend/app/services/job_discovery/crawling/pagination.py`
- Test: `tests/unit/job_discovery/test_checkpoint.py`
- Test: `tests/unit/job_discovery/test_pagination.py`

**Interfaces:**
- Consumes:
  - `CrawlPlan`
  - `DiscoveryTaskInput`
  - `RawJobListing`
  - `PaginationType`
- Produces:
  - `CrawlCheckpoint.to_dict() -> dict[str, Any]`
  - `CrawlCheckpoint.from_dict(data) -> CrawlCheckpoint`
  - `ListingPage`
  - `CrawlDriver.fetch_listing_page(plan=CrawlPlan, task=DiscoveryTaskInput, cursor=dict|None) -> ListingPage`
  - `CrawlDriver.fetch_detail(plan=CrawlPlan, listing=RawJobListing, resource_key=str) -> RawJobDetail`
  - `page_fingerprint(page: ListingPage) -> str`
  - `iterate_pages(plan, task, driver, checkpoint=None, trajectory=None) -> Iterator[ListingPage]`

- [ ] **Step 1: 写 checkpoint 失败测试**

```python
def test_checkpoint_roundtrip_preserves_pending_details() -> None:
    checkpoint = CrawlCheckpoint(
        plan_version=1,
        source_url="https://jobs.example.com/campus",
        pagination_cursor={"page": 3},
        visited_page_keys=["p1", "p2"],
        pending_detail_keys=["d2"],
        completed_detail_keys=["d1"],
        failed_detail_keys=[],
    )
    assert CrawlCheckpoint.from_dict(checkpoint.to_dict()) == checkpoint


def test_checkpoint_rejects_other_source_url() -> None:
    checkpoint = CrawlCheckpoint(
        plan_version=1,
        source_url="https://a.example/jobs",
    )
    with pytest.raises(ValueError, match="source_url"):
        checkpoint.validate_for(
            plan_version=1,
            source_url="https://b.example/jobs",
        )
```

- [ ] **Step 2: 写分页失败测试**

```python
def test_page_number_requires_positive_terminal_evidence() -> None:
    driver = FakeDriver(
        pages=[
            ListingPage("p1", [listing("1")], {"page": 2}),
            ListingPage("p2", [listing("2")], None),
        ],
    )
    with pytest.raises(CompletionUnverifiedError):
        list(iterate_pages(page_number_plan(), task(), driver))


def test_repeated_page_fingerprint_raises_loop_error() -> None:
    repeated = ListingPage("same", [listing("1")], {"page": 2})
    driver = FakeDriver(pages=[repeated, repeated])
    with pytest.raises(PaginationLoopError):
        list(iterate_pages(page_number_plan(), task(), driver))


def test_api_cursor_accepts_next_cursor_null() -> None:
    driver = FakeDriver(
        pages=[
            ListingPage("c1", [listing("1")], {"cursor": "c2"}),
            ListingPage(
                "c2",
                [listing("2")],
                None,
                terminal_evidence="next_cursor_null",
            ),
        ],
    )
    pages = list(iterate_pages(api_cursor_plan(), task(), driver))
    assert [page.page_key for page in pages] == ["c1", "c2"]
    assert pages[-1].terminal_evidence == "next_cursor_null"
```

- [ ] **Step 3: 运行测试确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_checkpoint.py `
  tests/unit/job_discovery/test_pagination.py -q
```

Expected: collection/import failure，因为三个新模块尚不存在。

- [ ] **Step 4: 实现精确契约**

`checkpoint.py`：

```python
@dataclass
class CrawlCheckpoint:
    plan_version: int
    source_url: str
    pagination_cursor: dict[str, Any] | None = None
    visited_page_keys: list[str] = field(default_factory=list)
    pending_detail_keys: list[str] = field(default_factory=list)
    completed_detail_keys: list[str] = field(default_factory=list)
    failed_detail_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CrawlCheckpoint":
        return cls(**data)

    def validate_for(self, *, plan_version: int, source_url: str) -> None:
        if self.plan_version != plan_version:
            raise ValueError("checkpoint plan_version mismatch")
        if self.source_url != source_url:
            raise ValueError("checkpoint source_url mismatch")
```

`driver.py`：

```python
@dataclass
class ListingPage:
    page_key: str
    listings: list[RawJobListing]
    next_cursor: dict[str, Any] | None
    terminal_evidence: str | None = None
    expected_page_count: int | None = None
    expected_listing_count: int | None = None


class CrawlDriver(Protocol):
    def fetch_listing_page(
        self,
        *,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        cursor: dict[str, Any] | None,
    ) -> ListingPage:
        raise NotImplementedError

    def fetch_detail(
        self,
        *,
        plan: CrawlPlan,
        listing: RawJobListing,
        resource_key: str,
    ) -> RawJobDetail:
        raise NotImplementedError
```

`pagination.py` 必须支持现有 Step1 枚举中的全部类型，但只实现三种可执行类型：

```python
SUPPORTED_PAGINATION = {
    PaginationType.SINGLE_PAGE,
    PaginationType.PAGE_NUMBER,
    PaginationType.API_CURSOR,
}
```

其他类型直接抛 `UnsupportedPaginationError`，不得悄悄用固定循环模拟。

同时在模块中定义：

```python
class PaginationLoopError(RuntimeError):
    pass


class CompletionUnverifiedError(RuntimeError):
    pass


class UnsupportedPaginationError(RuntimeError):
    pass


class CrawlBudgetExhausted(RuntimeError):
    def __init__(self, checkpoint: CrawlCheckpoint) -> None:
        super().__init__("crawl page budget exhausted")
        self.checkpoint = checkpoint
```

- [ ] **Step 5: 记录轨迹和 checkpoint**

`iterate_pages` 每页必须：

1. 调用 `driver.fetch_listing_page`；
2. 计算/检查 `page_fingerprint`；
3. 立即更新 checkpoint；
4. 调用 `trajectory.record_step("crawl_page", "ok", {"cursor": cursor}, {"page_key": page.page_key, "listing_count": len(page.listings)})`；
5. 只有收到 `terminal_evidence` 才正常停止。

页面预算只能抛 `CrawlBudgetExhausted` 并保留 checkpoint，不能写入 completion evidence。

- [ ] **Step 6: 跑测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_checkpoint.py `
  tests/unit/job_discovery/test_pagination.py `
  tests/unit/job_discovery/test_crawl_schemas.py `
  tests/unit/job_discovery/test_coverage.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add `
  backend/app/services/job_discovery/crawling/checkpoint.py `
  backend/app/services/job_discovery/crawling/driver.py `
  backend/app/services/job_discovery/crawling/pagination.py `
  tests/unit/job_discovery/test_checkpoint.py `
  tests/unit/job_discovery/test_pagination.py
git commit -m "feat(job-discovery): add crawl checkpoint and pagination state machine"
```

---

## Task 2.2: CrawlExecutor、URL 归一化与详情队列

**Files:**
- Create: `backend/app/services/job_discovery/crawling/crawl_executor.py`
- Modify: `backend/app/services/job_discovery/schemas.py`
- Modify: `backend/app/services/job_discovery/crawling/coverage.py`
- Test: `tests/unit/job_discovery/test_crawl_executor.py`
- Modify Test: `tests/unit/job_discovery/test_coverage.py`

**Interfaces:**
- Produces:
  - `normalize_detail_url(url: str) -> str`
  - `make_source_record_key(listing: RawJobListing) -> str`
  - `make_detail_resource_key(listing: RawJobListing) -> str`
  - `CrawlExecutionResult`
  - `CrawlExecutor.execute(plan=CrawlPlan, task=DiscoveryTaskInput, checkpoint=CrawlCheckpoint|None) -> CrawlExecutionResult`

- [ ] **Step 1: 扩展 RawJobListing 的岗位级 URL 契约**

先写测试：

```python
def test_listing_keeps_apply_url_separate_from_source_url() -> None:
    listing = RawJobListing(
        source_url="https://jobs.example.com/campus",
        detail_url="https://jobs.example.com/job/1",
        apply_url="https://jobs.example.com/job/1",
        company="Example",
        title="算法工程师",
    )
    assert listing.apply_url.endswith("/job/1")
    assert listing.apply_url != listing.source_url
```

再向 `RawJobListing` 添加：

```python
apply_url: str | None = None
```

这是向后兼容的默认字段，不修改旧构造调用。

- [ ] **Step 2: 写 URL/key 失败测试**

```python
def test_normalize_detail_url_strips_tracking_but_keeps_hash_route() -> None:
    left = normalize_detail_url(
        "https://app.mokahr.com/campus/deeproute/1?utm_source=x#/job/abc"
    )
    right = normalize_detail_url(
        "https://app.mokahr.com/campus/deeproute/1?source=y#/job/abc"
    )
    assert left == right
    assert "#/job/abc" in left


def test_shared_detail_is_fetched_once_and_locations_are_merged() -> None:
    driver = FakeDriver.shared_detail(["北京", "上海"])
    result = CrawlExecutor(driver).execute(
        plan=single_page_plan(),
        task=task(),
    )
    assert driver.detail_fetch_count == 1
    assert result.raw_listings[0].locations == ["上海", "北京"]


def test_missing_detail_url_is_not_success() -> None:
    driver = FakeDriver.with_listing(detail_url=None)
    result = CrawlExecutor(driver).execute(
        plan=single_page_plan(require_all_details=True),
        task=task(),
    )
    assert result.coverage.failed_detail_count == 1
    assert result.coverage.coverage_complete is False
```

- [ ] **Step 3: 运行并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_crawl_executor.py -q
```

Expected: FAIL，因为 `crawl_executor.py` 尚不存在。

- [ ] **Step 4: 实现 URL 归一化**

必须使用以下精确跟踪参数 allowlist：

```python
TRACKING_QUERY_KEYS = frozenset({
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "source",
    "channel",
    "ref",
    "referer_code",
    "timestamp",
    "session",
})
```

`recommendCode`、`external_referral_code`、`share_token` 不在列表中，因为它们可能影响合法投递渠道。fragment 必须保留。

- [ ] **Step 5: 实现执行结果和编排**

```python
@dataclass
class CrawlExecutionResult:
    raw_listings: list[RawJobListing]
    raw_details: list[RawJobDetail]
    coverage: CrawlCoverage
    checkpoint: CrawlCheckpoint | None
    error: str | None = None


class CrawlExecutor:
    def __init__(
        self,
        driver: CrawlDriver,
        trajectory: TrajectoryBuffer | None = None,
    ) -> None:
        self.driver = driver
        self.trajectory = trajectory

```

`execute` 的精确签名为：

```python
def execute(
    self,
    *,
    plan: CrawlPlan,
    task: DiscoveryTaskInput,
    checkpoint: CrawlCheckpoint | None = None,
) -> CrawlExecutionResult:
```

函数体按以下顺序实现：

1. `iterate_pages` 完整收集 listing；
2. 按 `source_record_key` 合并重复列表行；
3. 按 `detail_resource_key` 生成唯一详情队列；
4. 从 checkpoint 跳过已完成详情；
5. 抓取每个唯一详情并设置 `detail_resource_key`；
6. 记录失败资源和可续跑 checkpoint；
7. 构造 `CrawlCoverage`；
8. 最后调用一次 `verify_coverage` 并写入 `coverage.coverage_complete`。

异常字符串只保存异常类型和脱敏原因码，不保存响应 body、token 或完整 URL query。

- [ ] **Step 6: 加强 CoverageVerifier 的详情计数不变量**

补失败测试：

```python
def test_fetched_detail_count_must_equal_total_when_required() -> None:
    coverage = _complete_coverage()
    coverage.total_detail_count = 3
    coverage.fetched_detail_count = 2
    coverage.failed_detail_count = 0
    decision = verify_coverage(coverage)
    assert decision.complete is False
    assert "detail" in decision.reason
```

在 `verify_coverage` 中于 pagination 检查之前加入：

```python
if coverage.fetched_detail_count != coverage.total_detail_count:
    return CoverageDecision(
        complete=False,
        status=incomplete_status,
        reason=(
            f"fetched {coverage.fetched_detail_count}/"
            f"{coverage.total_detail_count} detail resources"
        ),
    )
```

- [ ] **Step 7: 跑测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_crawl_executor.py `
  tests/unit/job_discovery/test_pagination.py `
  tests/unit/job_discovery/test_coverage.py `
  tests/unit/job_discovery/test_crawl_schemas.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交**

```powershell
git add `
  backend/app/services/job_discovery/crawling/crawl_executor.py `
  backend/app/services/job_discovery/crawling/coverage.py `
  backend/app/services/job_discovery/schemas.py `
  tests/unit/job_discovery/test_crawl_executor.py `
  tests/unit/job_discovery/test_coverage.py
git commit -m "feat(job-discovery): execute complete listing and detail crawls"
```

---

## Task 2.3: Post-crawl pipeline 与 PATH B 双计划执行

**Files:**
- Create: `backend/app/services/job_discovery/post_crawl_pipeline.py`
- Create: `backend/app/services/job_discovery/adapters/complete_crawl_base.py`
- Create: `backend/app/services/job_discovery/crawling/playwright_driver.py`
- Modify: `backend/app/services/job_discovery/strategy/snapshot_executor.py`
- Modify: `backend/app/services/job_discovery/strategy/strategy_store.py`
- Modify: `backend/app/services/job_discovery/worker.py`
- Modify: `backend/app/config.py`
- Test: `tests/unit/job_discovery/test_post_crawl_pipeline.py`
- Test: `tests/unit/job_discovery/test_playwright_crawl_driver.py`
- Test: `tests/unit/job_discovery/test_path_b_routing.py`
- Modify Test: `tests/unit/test_snapshot_executor.py`
- Modify Test: `tests/integration/test_job_discovery_worker_strategy.py`

**Interfaces:**
- `run_post_crawl_pipeline(task, crawl_result) -> DiscoveryRunResult`
- `CompleteCrawlAdapter.build_driver(plan=CrawlPlan, task=DiscoveryTaskInput, trajectory=TrajectoryBuffer) -> CrawlDriver`
- `PlaywrightCrawlDriver` 执行 Planner 生成的通用 DOM/API plan
- `SnapshotExecutor` 支持 `plan_type: snapshot_plan|crawl_plan`

- [ ] **Step 1: 写 post-crawl 失败测试**

```python
def test_incomplete_crawl_never_returns_verified_success() -> None:
    crawl_result = incomplete_result(status="partial_success")
    result = run_post_crawl_pipeline(task(), crawl_result)
    assert result.status == "partial_success"
    assert result.coverage is crawl_result.coverage


def test_complete_detail_uses_detail_url_as_apply_url() -> None:
    crawl_result = complete_result(
        listing=listing(
            source_url="https://x/jobs",
            detail_url="https://x/job/1",
            apply_url="https://x/job/1",
        ),
        detail=detail("https://x/job/1", FULL_JD_TEXT),
    )
    result = run_post_crawl_pipeline(task(), crawl_result)
    assert result.status == "succeeded"
    assert result.candidates[0].apply_url == "https://x/job/1"


def test_complete_zero_listing_crawl_stays_succeeded_in_worker() -> None:
    result = complete_zero_listing_result()
    saved = finalize_worker_result(result)
    assert saved.status == "succeeded"
    assert saved.candidates == []
    assert saved.coverage.coverage_complete is True
```

- [ ] **Step 2: 实现最小 post-crawl pipeline**

Pipeline 必须：

1. 先调用 `verify_coverage`；
2. 每个 `RawJobDetail.full_text` 单独调用现有冻结 `_extract_jd_candidates`；
3. 用关联 listing 的 company/title/locations/apply_url 修正候选元数据；
4. 生成与 detail 对应的 `PageEvidence`；
5. 调用现有冻结 `_verify_evidence`；
6. 调用现有 canonical dedup 和 candidate packager；
7. 不完整时保留已成功详情产生的候选，但 status 使用 Verifier 决策；
8. 完整抓取且零岗位允许 `succeeded`。

不得调用 `enforce_result_invariants` 来判断 PEV 完整性。

- [ ] **Step 3: 写 PATH B 路由失败测试**

```python
def test_snapshot_plan_keeps_legacy_step_replay() -> None:
    result = SnapshotExecutor(
        snapshot_strategy(),
        task(),
        trajectory(),
    ).execute()
    assert result.coverage is None


def test_crawl_plan_uses_crawl_executor() -> None:
    result = SnapshotExecutor(
        crawl_strategy(),
        task(),
        trajectory(),
        crawl_driver_factory=lambda *_: FakeDriver.complete(),
    ).execute()
    assert result.coverage is not None
    assert result.coverage.coverage_complete is True


def test_crawl_structure_failure_returns_repair_context() -> None:
    result = SnapshotExecutor(
        crawl_strategy(),
        task(),
        trajectory(),
        crawl_driver_factory=lambda *_: FakeDriver.selector_failure(),
    ).execute()
    assert result.needs_supervisor_fallback is True
    assert result.snapshot_context["checkpoint"] is not None
    assert result.snapshot_context["failed_step"]["error_type"] == "structure_error"


def test_complete_adapter_uses_shared_crawl_executor() -> None:
    adapter = FakeCompleteCrawlAdapter(FakeDriver.complete())
    result = execute_matched_adapter(adapter, task())
    assert result.coverage is not None
    assert result.coverage.coverage_complete is True
    assert adapter.legacy_execute_called is False
```

- [ ] **Step 4: 修改 Strategy plan 校验**

`strategy_store.validate_plan_yaml`：

- 顶层有 `plan_type: crawl_plan` 时调用 `CrawlPlan.from_yaml`；
- 旧 `plan:` list 继续按 SnapshotPlan 校验；
- `plan_type: snapshot_plan` 允许显式写法；
- 其他 plan type 返回固定错误 `unsupported plan_type`。

- [ ] **Step 5: 修改 SnapshotExecutor**

构造函数新增：

```python
crawl_driver_factory: Callable[[CrawlPlan, DiscoveryTaskInput], CrawlDriver] | None = None
checkpoint: CrawlCheckpoint | None = None
```

`execute()` 顶部解析 plan type：

```python
plan_type = self._parse_plan_type()
if plan_type == "crawl_plan":
    return self._execute_crawl_plan()
return self._execute_snapshot_plan()
```

SnapshotPlan 现有测试和行为保持不变。

- [ ] **Step 6: 实现通用 Playwright plan driver**

`PlaywrightCrawlDriver` 只能解释 `CrawlPlan` 中已声明的 selector/JSON path：

- DOM listing：`item_selector` 下读取 title/location/job code/detail href；
- page number：根据 cursor 中的 page number 点击明确页码或 next selector；
- API cursor：只重放当前公开页面已经发出的同源 endpoint pattern，按 `items_path/next_cursor_path/has_more_path/total_count_path` 读取；
- detail：只打开同源 `http/https` detail URL，使用 detail selectors 读取正文；
- selector 缺失抛 `SelectorNotFoundError`；
- JSON path 缺失抛 `ApiPayloadChangedError`；
- 登录/验证码 marker 抛 `BlockedCrawlError`；
- 不执行任意 Planner 生成的 JavaScript，不允许 `javascript:` URL，不跨站发送 cookie/header。

异常类型定义在同一模块：

```python
class SelectorNotFoundError(RuntimeError):
    pass


class ApiPayloadChangedError(RuntimeError):
    pass


class BlockedCrawlError(RuntimeError):
    pass


class UnsafePlanExecutionError(RuntimeError):
    pass
```

测试使用 fake Playwright page/response，不访问网络：

```python
def test_generic_driver_rejects_cross_origin_detail_url() -> None:
    driver = driver_with_listing_href("https://evil.example/job/1")
    with pytest.raises(UnsafePlanExecutionError):
        driver.fetch_listing_page(
            plan=single_page_plan(),
            task=task(source_url="https://jobs.example.com"),
            cursor=None,
        )


def test_generic_driver_reads_declared_json_paths_only() -> None:
    driver = driver_with_api_payload({
        "data": {
            "items": [{"id": "1", "title": "工程师"}],
            "nextCursor": None,
            "total": 1,
        }
    })
    page = driver.fetch_listing_page(
        plan=api_cursor_plan(),
        task=task(),
        cursor=None,
    )
    assert len(page.listings) == 1
    assert page.terminal_evidence == "next_cursor_null"
```

- [ ] **Step 7: 添加灰度配置**

`backend/app/config.py`：

```python
job_discovery_pev_enabled: bool = False
job_discovery_planner_enabled: bool = False
job_discovery_legacy_path_c_enabled: bool = True
job_discovery_planner_max_inspection_pages: int = Field(default=3, ge=1, le=5)
```

PEV 默认关闭，现有部署不会自动切换。

- [ ] **Step 8: 修改 worker 注入**

Worker 只在匹配 `crawl_plan` 且 `job_discovery_pev_enabled` 时注入 crawl driver factory。PEV 关闭时遇到 crawl plan 必须返回固定错误 `pev_disabled` 并走 legacy fallback，不得半执行。

Worker 结果不变量分流必须为：

```python
if result.coverage is None:
    result = enforce_result_invariants(result)
else:
    coverage_decision = verify_coverage(result.coverage)
    result.status = coverage_decision.status
    result.block_reason = (
        None if coverage_decision.complete else coverage_decision.reason
    )
```

这只改变 PEV 调用点，不修改 `enforce_result_invariants` 函数本身；legacy PATH C 仍保持“succeeded 必须有 candidates”的原行为。

Adapter 分支增加版本化分发：

```python
if isinstance(adapter_instance, CompleteCrawlAdapter):
    crawl_result = adapter_instance.execute_crawl(
        task_input,
        strategy_record,
        trajectory,
    )
    result = run_post_crawl_pipeline(task_input, crawl_result)
else:
    result = adapter_instance.execute(
        task_input,
        strategy_record,
        trajectory,
    )
```

legacy `AlibabaSPAAdapter` 继续调用原 `execute()`。

Task summary 增加：

```python
"execution_path": executor_type,
"coverage_verified": bool(
    result.coverage is not None
    and verify_coverage(result.coverage).complete
),
"coverage": asdict(result.coverage) if result.coverage else None,
```

- [ ] **Step 9: 跑回归**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_post_crawl_pipeline.py `
  tests/unit/job_discovery/test_playwright_crawl_driver.py `
  tests/unit/job_discovery/test_path_b_routing.py `
  tests/unit/test_snapshot_executor.py `
  tests/integration/test_job_discovery_worker_strategy.py -q
```

Expected: PASS。

- [ ] **Step 10: 提交**

```powershell
git add `
  backend/app/config.py `
  backend/app/services/job_discovery/adapters/complete_crawl_base.py `
  backend/app/services/job_discovery/crawling/playwright_driver.py `
  backend/app/services/job_discovery/post_crawl_pipeline.py `
  backend/app/services/job_discovery/strategy/snapshot_executor.py `
  backend/app/services/job_discovery/strategy/strategy_store.py `
  backend/app/services/job_discovery/worker.py `
  tests/unit/job_discovery/test_post_crawl_pipeline.py `
  tests/unit/job_discovery/test_playwright_crawl_driver.py `
  tests/unit/job_discovery/test_path_b_routing.py `
  tests/unit/test_snapshot_executor.py `
  tests/integration/test_job_discovery_worker_strategy.py
git commit -m "feat(job-discovery): execute CrawlPlan through path B"
```

---

## Task 3: PATH C 缩减为 CrawlPlan 生成/修复 Agent

**Files:**
- Create: `backend/app/services/job_discovery/planning/__init__.py`
- Create: `backend/app/services/job_discovery/planning/crawl_plan_agent.py`
- Create: `backend/app/services/job_discovery/prompts/crawl_plan_agent.txt`
- Modify: `backend/app/services/job_discovery/deepagents_runner.py`
- Modify: `backend/app/services/job_discovery/strategy/error_classifier.py`
- Modify: `backend/app/services/job_discovery/worker.py`
- Test: `tests/unit/job_discovery/test_crawl_plan_agent.py`
- Modify Test: `tests/unit/job_discovery/test_path_b_routing.py`
- Test: `tests/integration/job_discovery/test_pev_worker_routing.py`

**Interfaces:**
- `build_crawl_plan_agent(settings, model, snapshot_context=None)`
- `generate_crawl_plan(task, agent, max_inspection_pages=3) -> CrawlPlan`
- `repair_crawl_plan(task, failed_plan, snapshot_context, agent, max_inspection_pages=3) -> CrawlPlan`
- `classify_next_action(error_type) -> Literal["planner_repair_then_path_b", "resume_path_b", "needs_manual_review", "partial_success"]`

Planner 模块同时定义：

```python
class PlanningContractError(RuntimeError):
    pass


class PlanningBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionErrorClassification:
    error_type: str
    reason: str
```

`classify_execution_error(message: str) -> ExecutionErrorClassification` 实现在现有 `strategy/error_classifier.py`，并复用现有 `classify_error` 的稳定 reason code。

- [ ] **Step 1: 写 Planner 输出边界失败测试**

```python
def test_planner_returns_plan_without_candidates() -> None:
    plan = generate_crawl_plan(
        task=task(),
        agent=FakePlanningAgent(valid_plan_yaml()),
    )
    assert isinstance(plan, CrawlPlan)


def test_planner_rejects_candidate_payload() -> None:
    agent = FakePlanningAgent({
        "plan_type": "crawl_plan",
        "version": 1,
        "candidates": [{"title": "invented"}],
    })
    with pytest.raises(PlanningContractError, match="candidates"):
        generate_crawl_plan(task=task(), agent=agent)


def test_planner_cannot_inspect_more_than_configured_pages() -> None:
    agent = FakePlanningAgent(tool_calls=["open"] * 4)
    with pytest.raises(PlanningBudgetExceeded):
        generate_crawl_plan(
            task=task(),
            agent=agent,
            max_inspection_pages=3,
        )
```

- [ ] **Step 2: 编写 Planner prompt**

Prompt 必须逐字包含以下约束：

```text
Your only deliverable is a valid CrawlPlan.
Do not return candidates or discovered jobs.
Do not enumerate every listing page.
Do not declare crawl completion.
Inspect at most {max_inspection_pages} pages.
Do not bypass login, captcha, slider, QR login, authentication, or anti-bot controls.
Every pagination plan must include a positive terminal signal.
If a positive terminal signal cannot be identified, return needs_manual_review.
```

Planner 可用工具限制为：

- `open_rendered_url`
- `read_dom`
- `extract_links`
- 一个只返回脱敏 XHR schema/字段名、不返回完整岗位数据的 `inspect_network_schema`
- `finish_with_manual_review`

Planner 不注册：

- `extract_jd_candidates`
- `verify_evidence`
- `package_candidates`
- `run_web_navigation`

- [ ] **Step 3: 实现结构化结果校验**

Agent 输出先拒绝 `candidates/evidence/coverage_complete` 等最终结果字段，再序列化成 YAML/映射交给 `CrawlPlan.from_yaml`。所有 Planner 输出仍要经过 `validate_security`。

- [ ] **Step 4: 扩展错误分类和下一步路由**

精确集合：

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
    "authentication_required",
}
TRANSIENT_ERRORS = {
    "network_timeout",
    "connection_reset",
    "upstream_5xx",
}
```

路由：

```text
structure_error       -> planner_repair_then_path_b
transient             -> resume_path_b
blocked               -> needs_manual_review
completion_unverified -> needs_manual_review
data_error            -> partial_success
```

- [ ] **Step 5: Worker 灰度接线**

PEV planner 只在以下条件同时成立时启用：

```python
self.settings.job_discovery_pev_enabled
and self.settings.job_discovery_planner_enabled
```

新流转：

```text
no strategy
  -> PATH C generate CrawlPlan
  -> PATH B execute
  -> CoverageVerifier

PATH A/B structure_error
  -> PATH C repair CrawlPlan(checkpoint + failed_step)
  -> PATH B resume
```

若 Planner 失败且 `job_discovery_legacy_path_c_enabled=True`，允许调用旧 Supervisor，但必须标记为 coverage-unverified；若该 flag 为 False，则进入 `needs_manual_review`。

- [ ] **Step 6: 集成测试**

```python
def test_unknown_site_plans_then_executes_without_direct_candidates() -> None:
    result = run_worker_with(
        planner=FakePlanner(valid_plan()),
        driver=FakeDriver.complete(),
    )
    assert result.coverage is not None
    assert result.coverage.coverage_complete is True
    assert planner_returned_candidates is False


def test_blocked_error_never_falls_back_to_legacy_navigation() -> None:
    result = run_worker_with(driver=FakeDriver.blocked("captcha"))
    assert result.status == "needs_manual_review"
    legacy_supervisor.assert_not_called()
```

- [ ] **Step 7: 跑测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_crawl_plan_agent.py `
  tests/unit/job_discovery/test_path_b_routing.py `
  tests/integration/job_discovery/test_pev_worker_routing.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交**

```powershell
git add `
  backend/app/services/job_discovery/planning `
  backend/app/services/job_discovery/prompts/crawl_plan_agent.txt `
  backend/app/services/job_discovery/deepagents_runner.py `
  backend/app/services/job_discovery/strategy/error_classifier.py `
  backend/app/services/job_discovery/worker.py `
  tests/unit/job_discovery/test_crawl_plan_agent.py `
  tests/unit/job_discovery/test_path_b_routing.py `
  tests/integration/job_discovery/test_pev_worker_routing.py
git commit -m "refactor(job-discovery): make path C plan and repair crawls"
```

---

## Task 4.1: 站点证据探针与脱敏 fixture 门禁

**Files:**
- Create: `tests/manual/probe_pev_site.py`
- Create: `tests/fixtures/job_discovery/moka/`
- Create: `tests/fixtures/job_discovery/feishu/`
- Create: `tests/fixtures/job_discovery/inovance/`
- Create: `tests/fixtures/job_discovery/xiaohongshu/`
- Test: `tests/integration/job_discovery/test_pev_site_fixtures.py`

**Purpose:** 在编写真实 adapter 前，先固定每个平台的 listing schema、详情 URL 规则、分页正向终止字段和鉴权边界。fixture 是 adapter 的输入契约，不把 live 响应直接塞进单元测试。

- [ ] **Step 1: 实现通用探针**

CLI：

```powershell
.\.venv\Scripts\python.exe tests/manual/probe_pev_site.py `
  --site moka `
  --url "https://app.mokahr.com/campus-recruitment/deeproute/145894#/home" `
  --output tests/fixtures/job_discovery/moka
```

探针必须输出：

```text
page_url
response_url_pattern
response_content_type
top_level_keys
items_path
item_field_names
total_count_path
has_more_path
next_cursor_path
detail_url_examples
terminal_evidence
blocked_markers
```

输出前必须：

- 删除 Cookie、Authorization 和 request body；
- query 中的 token/referral/share code 替换成 `<redacted>`；
- 正文只保留字段名和最多 3 个脱敏样本；
- 不保存邮箱、手机号、姓名、简历或设备标识。

- [ ] **Step 2: 写 fixture 契约测试**

```python
@pytest.mark.parametrize("site", ["moka", "feishu", "inovance", "xiaohongshu"])
def test_fixture_declares_positive_terminal_signal(site: str) -> None:
    fixture = load_fixture(site)
    assert any(
        fixture.get(key)
        for key in (
            "total_count_path",
            "has_more_path",
            "next_cursor_path",
            "terminal_selector",
            "single_page_proof",
        )
    )


def test_fixture_never_contains_secret_headers() -> None:
    text = all_fixture_text().lower()
    assert "authorization" not in text
    assert "cookie" not in text
    assert "bearer " not in text
```

- [ ] **Step 3: 对四个平台执行探针**

执行 URL：

- Moka：元戎和 DJI 各一次；
- 飞书：小鹏；
- 汇川：`https://recruit.inovance.com/#/jobs?ref=AHPNGR5`；
- 小红书：Ace Intern landing。

如果探针遇到登录/验证码，只保存 blocked marker fixture，并停止该平台 adapter 实施；不得绕过。

- [ ] **Step 4: 跑 fixture 门禁**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/integration/job_discovery/test_pev_site_fixtures.py -q
```

Expected: PASS；每个平台都有至少一个可证明终止字段或合法 blocked fixture。

- [ ] **Step 5: 提交**

```powershell
git add `
  tests/manual/probe_pev_site.py `
  tests/fixtures/job_discovery `
  tests/integration/job_discovery/test_pev_site_fixtures.py
git commit -m "test(job-discovery): capture sanitized PEV site contracts"
```

---

## Task 4.2: 迁移 Moka 到 PATH A driver + PATH B executor

**Files:**
- Create: `backend/app/services/job_discovery/adapters/moka.py`
- Modify: `backend/app/services/job_discovery/adapters/__init__.py`
- Modify: `scripts/seed_strategies.py`
- Test: `tests/unit/job_discovery/test_moka_adapter.py`
- Modify Test: `tests/integration/job_discovery/test_pev_site_fixtures.py`

**Interfaces:**
- `MokaCrawlAdapter(CompleteCrawlAdapter)`
- `MokaCrawlDriver(CrawlDriver)`

- [ ] **Step 1: 写 fixture 驱动失败测试**

```python
def test_moka_emits_every_listing_with_job_level_url() -> None:
    driver = MokaCrawlDriver.from_fixture("tests/fixtures/job_discovery/moka")
    result = CrawlExecutor(driver).execute(
        plan=moka_plan(),
        task=deeproute_task(),
    )
    assert result.coverage.raw_listing_count == result.coverage.expected_listing_count
    assert result.coverage.failed_detail_count == 0
    assert all(item.detail_url for item in result.raw_listings)
    assert all(item.apply_url != item.source_url for item in result.raw_listings)


def test_moka_hash_routes_survive_normalization() -> None:
    listing = first_moka_listing()
    assert "#/job/" in normalize_detail_url(listing.detail_url)
```

- [ ] **Step 2: 实现 Moka driver**

规则：

- 从 landing 点击/解析“全部职位”入口进入 job list；
- listing 由公开 XHR 或 DOM 确定性产生，LLM 不参与；
- 单页站点使用 `PaginationType.SINGLE_PAGE`；
- terminal evidence 必须是 fixture 证明的 `expected_listing_count reached` 或 `single_page_mokahr_hash_jobs`；
- detail URL 使用岗位 id 构造的 hash route；
- 所有唯一详情都抓取，不能使用 `_MAX_DETAIL_PAGES`；
- 鉴权墙返回 blocked error。

- [ ] **Step 3: 种灰度策略**

在 `scripts/seed_strategies.py` 新增默认 `enabled=False`：

```python
JobDiscoveryStrategy(
    url_pattern="app.mokahr.com/*",
    site_type="career_site",
    adapter=(
        "backend.app.services.job_discovery.adapters.moka."
        "MokaCrawlAdapter"
    ),
    plan_yaml=MOKA_CRAWL_PLAN,
    priority=40,
    enabled=False,
)
```

只在测试环境手动启用；连续 3 次 coverage-verified live smoke 后才能默认启用。

- [ ] **Step 4: 跑测试与 live smoke**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_moka_adapter.py `
  tests/unit/job_discovery/test_crawl_executor.py -q

$env:JOB_DISCOVERY_PEV_SITE='moka'
.\.venv\Scripts\python.exe -u tests/manual/test_pev_live_smoke.py
```

Live smoke 门禁：

- 元戎：21/21、详情 21/21、岗位级 URL 21/21；
- DJI：列表数等于站点公开 total、所有唯一详情成功；
- 三次运行 listing/detail/coverage 计数一致。

- [ ] **Step 5: 提交**

```powershell
git add `
  backend/app/services/job_discovery/adapters/moka.py `
  backend/app/services/job_discovery/adapters/__init__.py `
  scripts/seed_strategies.py `
  tests/unit/job_discovery/test_moka_adapter.py `
  tests/integration/job_discovery/test_pev_site_fixtures.py
git commit -m "feat(job-discovery): migrate Moka to verified crawl execution"
```

---

## Task 4.3: 迁移飞书招聘平台

**Files:**
- Create: `backend/app/services/job_discovery/adapters/feishu.py`
- Modify: `backend/app/services/job_discovery/adapters/__init__.py`
- Modify: `scripts/seed_strategies.py`
- Test: `tests/unit/job_discovery/test_feishu_adapter.py`

- [ ] **Step 1: 写失败测试**

```python
def test_feishu_uses_position_detail_route() -> None:
    listing = FeishuCrawlDriver.from_fixture(FIXTURE).first_listing()
    assert re.search(r"/campus/position/\d+/detail", listing.detail_url)
    assert "/campus/position/list" not in listing.apply_url


def test_feishu_follows_total_until_last_page() -> None:
    result = execute_fixture_crawl("feishu")
    assert result.coverage.raw_listing_count == result.coverage.expected_listing_count
    assert result.coverage.completion_evidence == ["total_count_reached"]
```

- [ ] **Step 2: 实现 driver**

必须：

- 从分享 landing 解析真实 `/campus/position/list` 入口；
- 使用 fixture 中公开的 page-number/total 字段；
- 保留合法 `share_token` 于 apply URL，但 detail resource key 归一化时不依赖 token；
- detail URL 必须为 `/campus/position/{position_id}/detail`；
- 每页和每详情保存 evidence ref；
- 详情返回登录/扫码 marker 时停止，不尝试登录。

- [ ] **Step 3: 种 disabled 灰度策略**

Pattern：

```text
*.jobs.feishu.cn/*
```

Adapter：

```text
backend.app.services.job_discovery.adapters.feishu.FeishuCrawlAdapter
```

- [ ] **Step 4: 验证**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_feishu_adapter.py `
  tests/integration/job_discovery/test_pev_site_fixtures.py -q

$env:JOB_DISCOVERY_PEV_SITE='feishu'
.\.venv\Scripts\python.exe -u tests/manual/test_pev_live_smoke.py
```

门禁：`listpage_apply_url=0`、详情成功数等于唯一 listing 数、total 正向完成。

- [ ] **Step 5: 提交**

```powershell
git add `
  backend/app/services/job_discovery/adapters/feishu.py `
  backend/app/services/job_discovery/adapters/__init__.py `
  scripts/seed_strategies.py `
  tests/unit/job_discovery/test_feishu_adapter.py
git commit -m "feat(job-discovery): migrate Feishu careers to verified crawl execution"
```

---

## Task 4.4: 迁移汇川自建 SPA

**Files:**
- Create: `backend/app/services/job_discovery/adapters/inovance.py`
- Modify: `backend/app/services/job_discovery/adapters/__init__.py`
- Modify: `scripts/seed_strategies.py`
- Test: `tests/unit/job_discovery/test_inovance_adapter.py`

- [ ] **Step 1: 写失败测试**

```python
def test_inovance_fetches_body_for_every_unique_detail() -> None:
    result = execute_fixture_crawl("inovance")
    assert result.coverage.fetched_detail_count == result.coverage.total_detail_count
    assert all(detail.full_text.strip() for detail in result.raw_details)


def test_inovance_never_uses_jobs_hash_as_apply_url() -> None:
    result = execute_fixture_crawl("inovance")
    assert all(
        "#/jobs" not in (listing.apply_url or "").rstrip("/")
        for listing in result.raw_listings
    )
```

- [ ] **Step 2: 实现 driver**

使用 Task 4.1 fixture 中确认的公开 API 或 hash detail route。若 fixture 只能确认 DOM click 而不能构造 URL，driver 必须从 listing 元素点击后读取稳定 detail route，并把 route 保存到 `RawJobListing.detail_url`；不能把列表页作为 fallback apply URL。

如果某 listing 无法生成稳定详情 URL：

- 将其计入 `failed_detail_count`；
- checkpoint 保存其 source record key；
- 结果为 partial/manual，不得 `succeeded`。

- [ ] **Step 3: 种 disabled 策略并验证**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_inovance_adapter.py `
  tests/unit/job_discovery/test_crawl_executor.py -q

$env:JOB_DISCOVERY_PEV_SITE='inovance'
.\.venv\Scripts\python.exe -u tests/manual/test_pev_live_smoke.py
```

门禁：正文数等于唯一详情数、list page URL 数为 0、CoverageVerifier complete。

- [ ] **Step 4: 提交**

```powershell
git add `
  backend/app/services/job_discovery/adapters/inovance.py `
  backend/app/services/job_discovery/adapters/__init__.py `
  scripts/seed_strategies.py `
  tests/unit/job_discovery/test_inovance_adapter.py
git commit -m "feat(job-discovery): migrate Inovance to verified detail crawls"
```

---

## Task 5: 迁移小红书 API cursor

**Files:**
- Create: `backend/app/services/job_discovery/adapters/xiaohongshu.py`
- Modify: `backend/app/services/job_discovery/adapters/__init__.py`
- Modify: `scripts/seed_strategies.py`
- Test: `tests/unit/job_discovery/test_xiaohongshu_adapter.py`

- [ ] **Step 1: 写 cursor 完成失败测试**

```python
def test_xhs_cursor_requires_null_next_cursor() -> None:
    driver = XhsFixtureDriver(last_cursor="still-more")
    result = CrawlExecutor(driver).execute(
        plan=xhs_plan(),
        task=xhs_task(),
    )
    assert result.coverage.coverage_complete is False


def test_xhs_collects_total_before_success() -> None:
    result = execute_fixture_crawl("xiaohongshu")
    assert result.coverage.raw_listing_count == result.coverage.expected_listing_count
    assert "next_cursor_null" in result.coverage.completion_evidence
```

- [ ] **Step 2: 实现 API cursor driver**

必须从 fixture 的真实 JSON path 读取：

- items；
- total count；
- next cursor 或 hasMore；
- position id；
- detail/apply URL。

停止条件只能是：

```text
nextCursor == null
```

或：

```text
hasMore == false AND collected_count == totalCount
```

“连续两次无新增”“页面预算到达”都作为不完整结果。

- [ ] **Step 3: 回归 43→1 假阴性**

Fixture 测试至少包含 43 个列表记录的脱敏结构，断言 executor 产出 43 个 raw listings，并逐个排队详情；不得把整个 landing blob 交给 `_extract_jd_candidates` 一次性切分。

- [ ] **Step 4: 种 disabled 策略并验证**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_xiaohongshu_adapter.py `
  tests/unit/job_discovery/test_pagination.py -q

$env:JOB_DISCOVERY_PEV_SITE='xiaohongshu'
.\.venv\Scripts\python.exe -u tests/manual/test_pev_live_smoke.py
```

门禁：raw listing 等于公开 total、无 cursor 未终止成功、所有详情有正文或明确 partial。

- [ ] **Step 5: 提交**

```powershell
git add `
  backend/app/services/job_discovery/adapters/xiaohongshu.py `
  backend/app/services/job_discovery/adapters/__init__.py `
  scripts/seed_strategies.py `
  tests/unit/job_discovery/test_xiaohongshu_adapter.py
git commit -m "feat(job-discovery): migrate Xiaohongshu cursor crawls"
```

---

## Task 6: 微信 SnapshotPlan 整体 deadline

**Files:**
- Create: `backend/app/services/job_discovery/strategy/deadline.py`
- Modify: `backend/app/services/job_discovery/strategy/snapshot_executor.py`
- Modify: `backend/app/services/job_discovery/deepagents_runner.py`
- Modify: `backend/app/services/job_discovery/worker.py`
- Test: `tests/unit/job_discovery/test_wechat_deadline.py`
- Modify Test: `tests/unit/test_snapshot_executor.py`
- Modify Test: `tests/integration/test_job_discovery_readgzh_smoke.py`

- [ ] **Step 1: 写硬超时失败测试**

```python
def test_hung_wechat_fetch_is_terminated() -> None:
    started = time.monotonic()
    result = run_with_hard_timeout(
        _hang_forever,
        timeout_seconds=0.2,
    )
    assert result.timed_out is True
    assert time.monotonic() - started < 2.0


def test_snapshot_deadline_stops_before_next_step() -> None:
    executor = SnapshotExecutor(
        wechat_strategy(),
        task(),
        trajectory(),
        deadline_seconds=0.1,
        hard_timeout_tools={"fetch_wechat_article"},
    )
    result = executor.execute()
    assert result.status == "needs_manual_review"
    assert result.block_reason == "task_deadline_exceeded"
```

- [ ] **Step 2: 实现 Windows 可终止子进程**

`deadline.py` 使用：

```python
ctx = multiprocessing.get_context("spawn")
```

顶层 worker 函数通过 Queue 返回成功值或脱敏异常。父进程 `join(timeout)` 后仍存活则：

```python
process.terminate()
process.join(5)
```

不能用仅抛 `FutureTimeout` 但后台线程继续运行的实现。

- [ ] **Step 3: SnapshotExecutor 使用单一绝对 deadline**

构造时：

```python
self._deadline_at = time.monotonic() + deadline_seconds
```

每一步计算 `remaining_seconds`。`fetch_wechat_article` 在子进程中使用 remaining time；OCR 每张图片前检查 remaining time，网络 timeout 使用：

```python
min(configured_timeout, max(1.0, remaining_seconds))
```

超时直接 `needs_manual_review/task_deadline_exceeded`，不进入 WebNavigationAgent。

- [ ] **Step 4: 确认微信仍走 SnapshotPlan**

Worker/Router 测试断言：

- `mp.weixin.qq.com/s/*` 命中 SnapshotPlan；
- 不构建 CrawlPlan Agent；
- 不调用 `run_web_navigation`；
- ReadGZH 失败返回合法 manual review；
- OCR 有结果时仍调用现有冻结 JD extraction。

- [ ] **Step 5: 测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_wechat_deadline.py `
  tests/unit/test_snapshot_executor.py `
  tests/integration/test_job_discovery_readgzh_smoke.py -q
```

Expected: PASS，hang fixture 在 2 秒内结束。

- [ ] **Step 6: 提交**

```powershell
git add `
  backend/app/services/job_discovery/strategy/deadline.py `
  backend/app/services/job_discovery/strategy/snapshot_executor.py `
  backend/app/services/job_discovery/deepagents_runner.py `
  backend/app/services/job_discovery/worker.py `
  tests/unit/job_discovery/test_wechat_deadline.py `
  tests/unit/test_snapshot_executor.py `
  tests/integration/test_job_discovery_readgzh_smoke.py
git commit -m "fix(job-discovery): enforce hard deadline for WeChat snapshots"
```

---

## Task 7: 北森鉴权墙固定为 needs_manual_review

**Files:**
- Create: `tests/unit/job_discovery/test_blocked_site_policy.py`
- Modify: `backend/app/services/job_discovery/strategy/error_classifier.py`
- Modify: `backend/app/services/job_discovery/worker.py`
- Modify: `backend/app/services/job_discovery/prompts/crawl_plan_agent.txt`

- [ ] **Step 1: 写失败测试**

```python
def test_beisen_authenticated_api_is_blocked_not_structure_error() -> None:
    error = classify_execution_error(
        "detail API returned 401 and requires SPA session authentication"
    )
    assert error.error_type == "blocked"
    assert error.reason == "authentication_required"


def test_blocked_result_does_not_enter_planner_repair_loop() -> None:
    result = run_worker_with(
        source_url=IFLYTEK_URL,
        driver=FakeDriver.authentication_required(),
    )
    assert result.status == "needs_manual_review"
    planner.repair.assert_not_called()
    legacy_supervisor.assert_not_called()
```

- [ ] **Step 2: 实现 blocked 优先级**

错误分类顺序必须保证：

1. authentication/login/captcha/anti-bot；
2. transient network；
3. structure；
4. data；
5. unknown。

401/403 如果页面/API 明确要求会话鉴权，分类为 blocked；公开 API 的临时 403 也不得自动尝试绕过。

- [ ] **Step 3: 测试和提交**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery/test_blocked_site_policy.py `
  tests/unit/job_discovery/test_crawl_plan_agent.py -q

git add `
  backend/app/services/job_discovery/strategy/error_classifier.py `
  backend/app/services/job_discovery/worker.py `
  backend/app/services/job_discovery/prompts/crawl_plan_agent.txt `
  tests/unit/job_discovery/test_blocked_site_policy.py
git commit -m "fix(job-discovery): keep authenticated career walls manual"
```

---

## Task 8: Legacy PATH C 标记、灰度发布和回滚

**Files:**
- Modify: `backend/app/services/job_discovery/worker.py`
- Modify: `scripts/seed_strategies.py`
- Create: `tests/manual/test_pev_live_smoke.py`
- Modify: `tests/integration/job_discovery/test_supervisor_ten_url_eval.py`
- Modify: `backend/app/services/job_discovery/README.md`
- Modify: `backend/app/services/job_discovery/CLAUDE.md`
- Modify: `docs/job-discovery-agent-workflow.md`
- Modify: `docs/job-discovery-agent-operations.md`
- Test: `tests/integration/job_discovery/test_pev_worker_routing.py`

- [ ] **Step 1: 写 legacy 标签失败测试**

```python
def test_legacy_path_c_is_explicitly_coverage_unverified() -> None:
    result, summary = run_legacy_supervisor_success()
    assert result.coverage is None
    assert summary["execution_path"] == "legacy_path_c"
    assert summary["coverage_verified"] is False


def test_pev_success_is_coverage_verified() -> None:
    result, summary = run_pev_success()
    assert result.coverage is not None
    assert summary["execution_path"] in {"path_a_adapter", "path_b_crawl_plan"}
    assert summary["coverage_verified"] is True
```

- [ ] **Step 2: 保持全局 result invariant 不变**

`result_contract.enforce_result_invariants` 不增加 “succeeded 必须有 coverage” 规则，也不需要在本任务中修改该文件。

PEV 完整性仍只由 `run_post_crawl_pipeline -> verify_coverage` 强制。

- [ ] **Step 3: 实现灰度状态**

Worker summary 固定写：

```python
execution_path: str
coverage_verified: bool
coverage: dict | None
legacy_fallback_reason: str | None
```

Legacy 成功可以继续保存候选，但管理端/评估输出必须显示 `coverage-unverified`，不能与 PEV PASS 混合统计。

- [ ] **Step 4: 更新 10 URL 评估门禁**

PEV PASS 定义：

```text
coverage_verified=true
coverage_complete=true
failed_detail_count=0
candidate_count == unique_listing_count（允许 canonical 多地区合并时单独报告）
count_apply_url_is_listpage=0
正文覆盖率=100%，合法鉴权墙除外
```

Legacy 结果单列，不计入 PEV pass rate。

- [ ] **Step 5: 灰度启用顺序**

每个平台满足三次连续 live smoke 后，按顺序启用：

1. Moka；
2. 飞书；
3. 汇川；
4. 小红书。

每次只把对应 `JobDiscoveryStrategy.enabled` 切为 true。其他站点仍走 legacy。

- [ ] **Step 6: 回滚规则**

单站出现以下任一条件时，只禁用该站策略：

- expected/raw listing count 漂移；
- 正向终止字段消失；
- detail failure > 0；
- listpage apply URL > 0；
- blocked marker 新出现；
- 三次运行计数不一致。

回滚不删除新契约、不修改全局 result invariant、不影响已稳定站点。

- [ ] **Step 7: 更新文档**

文档必须明确：

```text
PATH A = Certified site driver/adapter
PATH B = SnapshotPlan + CrawlPlan deterministic executor
PATH C = CrawlPlan generation/repair agent
Legacy PATH C = coverage-unverified compatibility fallback
CoverageVerifier = only completion authority
```

运维文档增加 flags、单站启停、checkpoint、manual review 和 live smoke 命令。

- [ ] **Step 8: 全量自动验证**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/job_discovery `
  tests/unit/test_job_discovery_repository.py `
  tests/unit/test_job_discovery_result_contract.py `
  tests/unit/test_job_discovery_tasks.py `
  tests/unit/test_job_discovery_tools.py `
  tests/unit/test_job_discovery_worker.py `
  tests/unit/test_snapshot_executor.py `
  tests/unit/test_strategy_router.py `
  tests/unit/test_strategy_store.py -q
.\.venv\Scripts\python.exe -m pytest `
  tests/integration/test_job_discovery_worker_strategy.py `
  tests/integration/job_discovery/test_pev_worker_routing.py `
  tests/integration/job_discovery/test_pev_site_fixtures.py `
  tests/integration/test_job_discovery_readgzh_smoke.py -q
.\.venv\Scripts\python.exe -m ruff check backend/app/services/job_discovery tests/unit/job_discovery tests/integration/job_discovery
```

Expected: 全部 PASS，Ruff 零错误。

- [ ] **Step 9: 运行 opt-in live 门禁**

```powershell
$env:RUN_TEN_URL_EVAL='1'
$env:JOB_DISCOVERY_PEV_ENABLED='1'
.\.venv\Scripts\python.exe `
  tests/integration/job_discovery/test_supervisor_ten_url_eval.py -v
```

不得在缺少所需 API key 时把跳过结果报告为成功。

- [ ] **Step 10: 最终提交**

```powershell
git add `
  backend/app/services/job_discovery/worker.py `
  backend/app/services/job_discovery/README.md `
  backend/app/services/job_discovery/CLAUDE.md `
  docs/job-discovery-agent-workflow.md `
  docs/job-discovery-agent-operations.md `
  scripts/seed_strategies.py `
  tests/manual/test_pev_live_smoke.py `
  tests/integration/job_discovery/test_supervisor_ten_url_eval.py `
  tests/integration/job_discovery/test_pev_worker_routing.py
git commit -m "docs(job-discovery): complete PEV gray migration rollout"
```

---

## 最终完成定义

只有同时满足以下条件，Step2–Step8 才算完成：

- PATH B 同时执行旧 SnapshotPlan 和新 CrawlPlan。
- CrawlExecutor 可以从 checkpoint 恢复，且不会重复抓已完成详情。
- 所有成功 PEV crawl 都携带 CoverageVerifier 可重算的正向完成证据。
- PATH C 只产生或修复 CrawlPlan，不直接产生 candidates。
- Moka、飞书、汇川、小红书各自连续三次 live smoke 计数稳定。
- 所有迁移站点 `count_apply_url_is_listpage == 0`。
- 所有唯一详情要么成功抓取，要么任务为 partial/manual；不存在“详情失败但 succeeded”。
- 小红书只在 cursor 正向终止且 collected count 达到 total 时成功。
- 微信不进入 CrawlPlan Agent，hang 会被硬 deadline 终止。
- 北森会话鉴权墙稳定返回 `needs_manual_review`，不进入 repair/legacy 循环。
- Legacy PATH C 仍可用，但所有结果明确标记 `coverage-unverified`。
- `enforce_result_invariants` 的 legacy 全局行为没有改变。
- job_discovery 单测、相关集成测试和 Ruff 全部通过。

## 推荐执行方式

按 Task 2.1 → 2.2 → 2.3 → 3 → 4.1 → 4.2 → 4.3 → 4.4 → 5 → 6 → 7 → 8 顺序执行。站点 adapter 任务必须在 Task 4.1 fixture 门禁通过后开始；任一站点无法取得公开、可证明终止的数据源时，停止该站迁移并保持 legacy/manual，不得用 LLM 或固定预算补齐。
