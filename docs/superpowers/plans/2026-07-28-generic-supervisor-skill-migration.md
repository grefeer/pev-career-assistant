# Generic Supervisor Skill Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make new job-discovery URLs run through a generic, coverage-verifiable Supervisor that implements the reusable `skill/job-discovery` workflow without loading an Adapter.

**Architecture:** Add a `generic_supervisor` service that owns a deterministic classify-plan-browse-extract-verify state machine. It emits the existing `DiscoveryRunResult` / `CrawlCoverage` contracts; page-level extraction is bounded and cacheable, while Worker remains the only persistence coordinator.

**Tech Stack:** Python 3.12, FastAPI service layer, Playwright sync API, LangGraph/LangChain structured extraction, pytest, existing `CrawlCoverage` and post-crawl pipeline.

## Global Constraints

- Never bypass login, CAPTCHA, anti-bot, permission, or paywall barriers.
- New-URL evaluation must set `job_discovery_disallow_adapters=true`; any Adapter import or `path_a_adapter` output is a test failure.
- Do not add domain names, corporate IDs, private API endpoints, or company-specific selectors to generic code.
- Use existing `NormalizedJobCandidate`, `PageEvidence`, `DiscoveryRunResult`, `CrawlCoverage`, `verify_coverage`, and post-crawl normalization contracts.
- A thin SPA receives exactly one `search_interact` retry after `parallel_fetch`; no unbounded retry loop.
- A PEV pass requires positive terminal evidence, all required details, no duplicates, and non-empty JD body for every candidate.

---

### Task 1: Add generic-supervisor contracts and feature controls

**Files:**
- Create: `backend/app/services/job_discovery/generic_supervisor/contracts.py`
- Create: `backend/app/services/job_discovery/generic_supervisor/__init__.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/services/job_discovery/schemas.py`
- Test: `tests/unit/job_discovery/test_generic_supervisor_contracts.py`

**Interfaces:**
- Produces `DiscoveryMode`, `DiscoveryPlan`, `PageArtifact`, and `GenericSupervisorResult`.
- Adds `Settings.job_discovery_generic_supervisor_enabled: bool = False`, `job_discovery_disallow_adapters: bool = False`, and bounded `job_discovery_generic_max_concurrency: int = 4`.

- [ ] **Step 1: Write failing contract tests**

```python
def test_discovery_plan_allows_one_retry_and_rejects_zero_page_budget() -> None:
    plan = DiscoveryPlan(mode=DiscoveryMode.PARALLEL_FETCH, max_pages=3)
    assert plan.retry_mode is DiscoveryMode.SEARCH_INTERACT
    with pytest.raises(ValueError, match="max_pages"):
        DiscoveryPlan(mode=DiscoveryMode.LIST, max_pages=0)

def test_generic_result_maps_to_existing_discovery_contract() -> None:
    result = GenericSupervisorResult.manual_review("captcha")
    assert result.to_discovery_result().status == "needs_manual_review"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/job_discovery/test_generic_supervisor_contracts.py -q`

Expected: FAIL because `generic_supervisor.contracts` does not exist.

- [ ] **Step 3: Implement contracts and settings**

```python
class DiscoveryMode(StrEnum):
    PARALLEL_FETCH = "parallel_fetch"
    SEARCH_INTERACT = "search_interact"
    LIST = "list"
    DETAIL = "detail"
    WECHAT = "wechat"
    BLOCKED = "blocked"

@dataclass(frozen=True)
class DiscoveryPlan:
    mode: DiscoveryMode
    max_pages: int
    max_concurrency: int = 4
    retry_mode: DiscoveryMode | None = DiscoveryMode.SEARCH_INTERACT
```

Validate positive page/concurrency values in `__post_init__`; make `GenericSupervisorResult.to_discovery_result()` produce only existing domain objects.

- [ ] **Step 4: Run the contract tests**

Run: `python -m pytest tests/unit/job_discovery/test_generic_supervisor_contracts.py -q`

Expected: PASS.

### Task 2: Build deterministic classification and bounded path selection

**Files:**
- Create: `backend/app/services/job_discovery/generic_supervisor/planner.py`
- Test: `tests/unit/job_discovery/test_generic_supervisor_planner.py`

**Interfaces:**
- Consumes `DiscoveryTaskInput`, `Settings`, and a probe record `{url, text, blocked_marker, next_url}`.
- Produces `DiscoveryPlan` through `plan_discovery(task, probe, settings) -> DiscoveryPlan`.

- [ ] **Step 1: Write failing planner tests**

```python
def test_thin_spa_uses_parallel_fetch_then_exactly_one_search_retry(settings) -> None:
    plan = plan_discovery(task, Probe(text="", next_url=None), settings)
    assert plan.mode is DiscoveryMode.PARALLEL_FETCH
    assert plan.retry_mode is DiscoveryMode.SEARCH_INTERACT
    assert plan.max_attempts == 2

def test_captcha_never_creates_a_browser_retry(settings) -> None:
    plan = plan_discovery(task, Probe(text="请完成验证", blocked_marker="captcha"), settings)
    assert plan.mode is DiscoveryMode.BLOCKED
    assert plan.retry_mode is None
```

- [ ] **Step 2: Run the planner tests to verify failure**

Run: `python -m pytest tests/unit/job_discovery/test_generic_supervisor_planner.py -q`

Expected: FAIL because `plan_discovery` is undefined.

- [ ] **Step 3: Implement pure classification rules**

```python
def plan_discovery(task: DiscoveryTaskInput, probe: Probe, settings: Settings) -> DiscoveryPlan:
    if probe.blocked_marker:
        return DiscoveryPlan(mode=DiscoveryMode.BLOCKED, max_pages=0, retry_mode=None)
    if "mp.weixin.qq.com" in task.source_url:
        return DiscoveryPlan(mode=DiscoveryMode.WECHAT, max_pages=1, retry_mode=None)
    return DiscoveryPlan(mode=DiscoveryMode.PARALLEL_FETCH,
                         max_pages=settings.job_discovery_max_pages_per_task,
                         max_concurrency=settings.job_discovery_generic_max_concurrency)
```

Use generic URL/text signals only; do not match individual employers or platforms.

- [ ] **Step 4: Run planner tests**

Run: `python -m pytest tests/unit/job_discovery/test_generic_supervisor_planner.py -q`

Expected: PASS.

### Task 3: Implement generic page discovery, URL pagination inference, and bounded concurrency

**Files:**
- Create: `backend/app/services/job_discovery/generic_supervisor/browser.py`
- Test: `tests/unit/job_discovery/test_generic_supervisor_browser.py`

**Interfaces:**
- Produces `infer_pagination_urls(first_url, next_url, current_page, total_pages) -> list[str] | None`.
- Produces `collect_page_artifacts(plan, task, browser_factory) -> list[PageArtifact]`.
- `PageArtifact` contains `url`, `text`, `title`, `content_hash`, `listing_terminal_evidence`, and `detail_links`.

- [ ] **Step 1: Write failing pagination and de-duplication tests**

```python
def test_infer_pagination_urls_from_changed_query_key() -> None:
    urls = infer_pagination_urls(
        "https://jobs.example.test/list?current=1&limit=10",
        "https://jobs.example.test/list?current=2&limit=10", 1, 3,
    )
    assert urls[-1].endswith("current=3&limit=10")

def test_collects_each_hash_once_when_parallel_pages_repeat_content() -> None:
    artifacts = collect_page_artifacts(plan, task, fake_browser_factory)
    assert [item.content_hash for item in artifacts] == ["a", "b"]
```

- [ ] **Step 2: Run browser tests to verify failure**

Run: `python -m pytest tests/unit/job_discovery/test_generic_supervisor_browser.py -q`

Expected: FAIL because browser helpers do not exist.

- [ ] **Step 3: Implement public-DOM-only browsing**

Use `urllib.parse` to compare query mappings and vary only one integer pagination key. Use a bounded `ThreadPoolExecutor(max_workers=plan.max_concurrency)` only after URL inference. If inference fails, use one browser session and generic enabled next-button controls, stopping at `max_pages` or a disabled/unchanged page. Hash rendered text with SHA-256 and preserve first-seen order.

- [ ] **Step 4: Add thin-SPA retry test and implement it**

```python
def test_thin_parallel_fetch_retries_search_interact_once() -> None:
    artifacts = collect_page_artifacts(plan, task, thin_then_interactive_browser)
    assert thin_then_interactive_browser.calls == ["parallel_fetch", "search_interact"]
```

Only retry when no artifact has meaningful text; a blocked marker returns a manual-review artifact instead.

- [ ] **Step 5: Run browser tests**

Run: `python -m pytest tests/unit/job_discovery/test_generic_supervisor_browser.py -q`

Expected: PASS.

### Task 4: Add page-level extraction cache and deterministic fan-in

**Files:**
- Create: `backend/app/services/job_discovery/generic_supervisor/extraction.py`
- Test: `tests/unit/job_discovery/test_generic_supervisor_extraction.py`

**Interfaces:**
- Consumes `list[PageArtifact]` and `extract_page_candidates(artifact) -> list[NormalizedJobCandidate]`.
- Produces `extract_pages(artifacts, cache) -> dict[str, list[NormalizedJobCandidate]]`.
- Cache protocol: `get(content_hash) -> list[NormalizedJobCandidate] | None`, `put(content_hash, candidates) -> None`.

- [ ] **Step 1: Write failing cache and page-isolation tests**

```python
def test_same_content_hash_is_extracted_once() -> None:
    cache = InMemoryPageExtractionCache()
    result = extract_pages([artifact("same"), artifact("same")], cache, extractor)
    assert extractor.call_count == 1
    assert result["same"][0].responsibilities

def test_empty_body_is_not_promoted_to_a_full_jd() -> None:
    candidates = extract_pages([artifact("a")], cache, title_only_extractor)["a"]
    assert candidates == []
```

- [ ] **Step 2: Run extraction tests to verify failure**

Run: `python -m pytest tests/unit/job_discovery/test_generic_supervisor_extraction.py -q`

Expected: FAIL because extraction cache and fan-in are undefined.

- [ ] **Step 3: Implement page-local extraction**

Invoke the existing structured LLM extractor only when `job_discovery_llm_extraction_enabled` is true; otherwise use the existing deterministic extraction. Reject title-only candidates in this PEV path. Attach `PageEvidence.content_hash` to every candidate before calling existing normalizers and `run_post_crawl_pipeline`.

- [ ] **Step 4: Run extraction tests**

Run: `python -m pytest tests/unit/job_discovery/test_generic_supervisor_extraction.py -q`

Expected: PASS.

### Task 5: Compose the generic supervisor and emit PEV-compatible coverage

**Files:**
- Create: `backend/app/services/job_discovery/generic_supervisor/service.py`
- Modify: `backend/app/services/job_discovery/post_crawl_pipeline.py`
- Test: `tests/unit/job_discovery/test_generic_supervisor_service.py`

**Interfaces:**
- Produces `run_generic_supervisor(task, settings, dependencies) -> DiscoveryRunResult`.
- Its successful result always contains `CrawlCoverage` with pagination type, visited pages, expected/observed listing counts where available, detail counts, and positive `completion_evidence`.

- [ ] **Step 1: Write failing PEV service tests**

```python
def test_complete_two_page_run_is_coverage_verified() -> None:
    result = run_generic_supervisor(task, settings, dependencies=two_page_dependencies)
    decision = verify_coverage(result.coverage)
    assert decision.complete is True
    assert result.status == "succeeded"

def test_missing_detail_body_keeps_pev_incomplete() -> None:
    result = run_generic_supervisor(task, settings, dependencies=missing_detail_dependencies)
    assert verify_coverage(result.coverage).complete is False
```

- [ ] **Step 2: Run service tests to verify failure**

Run: `python -m pytest tests/unit/job_discovery/test_generic_supervisor_service.py -q`

Expected: FAIL because the service does not exist.

- [ ] **Step 3: Implement state-machine composition**

Call planner, browser, extraction, then existing `run_post_crawl_pipeline`. Compute a `CrawlCoverage` only from observed page/detail facts. Mark `needs_manual_review` for wall artifacts and `partial_success`/`failed` for budget exhaustion or incomplete details; never set `coverage_complete=True` without terminal evidence.

- [ ] **Step 4: Run service tests**

Run: `python -m pytest tests/unit/job_discovery/test_generic_supervisor_service.py -q`

Expected: PASS.

### Task 6: Integrate Worker routing and enforce adapter prohibition

**Files:**
- Modify: `backend/app/services/job_discovery/worker.py`
- Modify: `backend/app/config.py`
- Test: `tests/unit/test_job_discovery_worker.py`
- Test: `tests/integration/test_generic_supervisor_worker.py`

**Interfaces:**
- Uses `run_generic_supervisor(...)` before strategy routing when both generic Supervisor and adapter prohibition flags are enabled.
- Emits `execution_path="path_generic_supervisor"` and `adapter_invocation_count=0` in the Worker result summary.

- [ ] **Step 1: Write failing worker tests**

```python
def test_disallow_adapters_never_imports_matched_adapter(worker, task, monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "_load_adapter", pytest.fail)
    result = worker._run_task(task)
    assert result.summary_json["execution_path"] == "path_generic_supervisor"
    assert result.summary_json["adapter_invocation_count"] == 0

def test_generic_supervisor_disabled_preserves_existing_route(worker, task) -> None:
    worker.settings.job_discovery_generic_supervisor_enabled = False
    assert worker._route_task(task) != "path_generic_supervisor"
```

- [ ] **Step 2: Run Worker tests to verify failure**

Run: `python -m pytest tests/unit/test_job_discovery_worker.py tests/integration/test_generic_supervisor_worker.py -q`

Expected: FAIL because no generic route or adapter-invocation metric exists.

- [ ] **Step 3: Implement routing**

Add the generic route before `StrategyRouter.match`. Do not call `StrategyRouter`, `_load_adapter`, `SnapshotExecutor`, or legacy Supervisor while `job_discovery_disallow_adapters` is true. Add the new execution label and summary metric while preserving existing feature-flag behavior when false.

- [ ] **Step 4: Run Worker tests**

Run: `python -m pytest tests/unit/test_job_discovery_worker.py tests/integration/test_generic_supervisor_worker.py -q`

Expected: PASS.

### Task 7: Add no-adapter blind-evaluation harness and documentation

**Files:**
- Create: `tests/manual/run_generic_supervisor_url_eval.py`
- Create: `tests/unit/test_generic_supervisor_url_eval.py`
- Modify: `docs/job-discovery-agent-operations.md`

**Interfaces:**
- CLI: `python tests/manual/run_generic_supervisor_url_eval.py --urls-file <json> --timeout-sec 360`.
- Every emitted row contains `execution_path`, `adapter_invocation_count`, `candidate_count`, `body_candidate_count`, `unique_listing_count`, `coverage_verified`, `elapsed_sec`, and `block_reason`.

- [ ] **Step 1: Write failing evaluator contract test**

```python
def test_evaluator_rejects_an_adapter_path() -> None:
    row = build_eval_row({"execution_path": "path_a_adapter", "adapter_invocation_count": 1})
    assert row["bucket"] == "invalid_adapter_usage"
```

- [ ] **Step 2: Run evaluator test to verify failure**

Run: `python -m pytest tests/unit/test_generic_supervisor_url_eval.py -q`

Expected: FAIL because the evaluator does not exist.

- [ ] **Step 3: Implement evaluator and operations instructions**

The harness must construct settings with `job_discovery_generic_supervisor_enabled=True`, `job_discovery_disallow_adapters=True`, and PEV enabled. It must report, not hide, blocked or incomplete sites. Document that new URL evaluation is invalid whenever Adapter use is nonzero.

- [ ] **Step 4: Run all migration tests and static checks**

Run: `python -m pytest tests/unit/job_discovery/test_generic_supervisor_contracts.py tests/unit/job_discovery/test_generic_supervisor_planner.py tests/unit/job_discovery/test_generic_supervisor_browser.py tests/unit/job_discovery/test_generic_supervisor_extraction.py tests/unit/job_discovery/test_generic_supervisor_service.py tests/unit/test_job_discovery_worker.py tests/integration/test_generic_supervisor_worker.py tests/unit/test_generic_supervisor_url_eval.py -q`

Expected: PASS.

Run: `python -m ruff check backend/app/services/job_discovery/generic_supervisor backend/app/services/job_discovery/worker.py`

Expected: PASS.

## Plan Self-Review

- Spec coverage: Tasks 1-2 cover controls and classification; Task 3 covers parallel fetch/retry; Task 4 covers page fan-out/cache; Task 5 covers PEV verification; Task 6 enforces no-Adapter routing; Task 7 covers blind evaluation and operations.
- Placeholder scan: no unresolved implementation stage remains.
- Type consistency: `DiscoveryPlan`, `PageArtifact`, `run_generic_supervisor`, and `adapter_invocation_count` are introduced before their consuming tasks.
