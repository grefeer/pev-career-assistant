# Supervisor Site-Wide JD Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PATH C reliably navigate from a public company/recruitment URL to every reachable job detail up to configured limits, preserve complete JD evidence, and return evidence-backed structured candidates without depending on the LLM's final-message format.

**Architecture:** Keep Strategy Router and PATH A/B/C selection unchanged. Inside PATH C, combine a persistent Playwright session with deterministic inventory, pagination, evidence, checkpoint, status, and serialization components; use the Supervisor only for ambiguous navigation decisions and unfamiliar schema/DOM interpretation.

**Tech Stack:** Python 3.12, FastAPI service conventions, SQLAlchemy 2, Alembic, Playwright sync API, Deep Agents/LangGraph, Pydantic, encrypted MinIO/S3 object storage, pytest.

## Global Constraints

- Do not modify Strategy Router matching, strategy state transitions, PATH A execution, or PATH B execution.
- Preserve PATH A/B fallback compatibility into PATH C.
- Do not bypass login, captcha, anti-bot, permission, or paywall barriers.
- Only `backend/app/repositories/*.py` may perform SQL; business rules remain in services/domain modules.
- Default PATH C limits: 200 jobs, 1,800 seconds total runtime, 45 seconds per page, and two retries per job.
- `job_discovery_page_timeout_seconds` must validate within 40-50 seconds.
- Complete JD bodies go to encrypted object storage; database evidence stores preview text, content hash, metadata, and `storage_uri`.
- Every populated structured field must resolve to a JSON path or text span in its own job-detail evidence.
- Missing fields remain missing and produce warnings; no source content may be invented.
- A list page cannot serve as the only evidence for a completed candidate.
- Silent job loss, cross-job evidence references, and final-message-only result loss are prohibited.
- Existing user changes and manual output files are out of scope.

---

## File Map

**Create**

- `backend/app/services/job_discovery/result_contract.py` — normalize transitional Agent outputs and enforce status invariants.
- `backend/app/services/job_discovery/runtime_state.py` — typed inventory, budgets, coverage, checkpoints, and final counters.
- `backend/app/services/job_discovery/browser_session.py` — persistent Playwright session and rendered interaction primitives.
- `backend/app/services/job_discovery/page_classifier.py` — deterministic page classification and recruitment-entry ranking.
- `backend/app/services/job_discovery/site_navigator.py` — category, pagination, scrolling, and job-detail traversal.
- `backend/app/services/job_discovery/evidence_artifacts.py` — encrypted complete-evidence artifact persistence.
- `backend/app/services/job_discovery/structured_extraction.py` — JSON/DOM/LLM extraction with field-level source references.
- `backend/app/services/job_discovery/supervisor_orchestrator.py` — PATH C runtime owner and deterministic result assembly.
- `alembic/versions/20260721_0012_supervisor_discovery_checkpoint.py` — checkpoint and field-evidence columns.
- Focused unit/integration tests named in each task.
- Sanitized fixtures under `tests/fixtures/job_discovery/site_wide/`.

**Modify**

- `backend/app/config.py` — PATH C budget settings.
- `backend/app/db/models.py` — checkpoint and field-evidence mappings.
- `backend/app/repositories/job_discovery.py` — checkpoint persistence only.
- `backend/app/services/job_discovery/schemas.py` — storage URI, field evidence, coverage/result summaries.
- `backend/app/services/job_discovery/deepagents_runner.py` — bind PATH C tools to a task runtime; retain compatibility wrappers.
- `backend/app/services/job_discovery/prompts/supervisor_base.txt` — reduce Supervisor role to decisions over observed actions.
- `backend/app/services/job_discovery/worker.py` — invoke the PATH C orchestrator and persist its typed result.
- `scripts/run_job_discovery_worker.py` — supply encrypted evidence artifact storage.
- Existing tests and operational documentation listed below.

---

### Task 1: Seal the result contract and status semantics

**Files:**
- Create: `backend/app/services/job_discovery/result_contract.py`
- Modify: `backend/app/services/job_discovery/worker.py:86-124`
- Modify: `tests/unit/test_job_discovery_worker.py:150-210`
- Create: `tests/unit/test_job_discovery_result_contract.py`

**Interfaces:**
- Consumes: raw Deep Agent invocation output `Any` and `DiscoveryRunResult`.
- Produces: `parse_agent_result(raw: Any) -> DiscoveryRunResult`, `enforce_result_invariants(result: DiscoveryRunResult) -> DiscoveryRunResult`, and `AgentResultParseError` diagnostic metadata.

- [ ] **Step 1: Add failing parser-shape tests**

```python
from langchain_core.messages import AIMessage, ToolMessage

from backend.app.services.job_discovery.result_contract import parse_agent_result


def test_parses_fenced_json_from_reverse_message_scan() -> None:
    raw = {
        "messages": [
            AIMessage(content='```json\n{"status":"succeeded","summary":"ok"}\n```'),
            ToolMessage(content="done", tool_call_id="call-1"),
        ]
    }
    assert parse_agent_result(raw).status == "succeeded"


def test_parses_text_content_block() -> None:
    raw = {
        "messages": [
            AIMessage(content=[{
                "type": "text",
                "text": '{"status":"needs_manual_review","block_reason":"captcha"}',
            }])
        ]
    }
    assert parse_agent_result(raw).block_reason == "captcha"
```

- [ ] **Step 2: Run the new parser tests and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_result_contract.py -q`

Expected: FAIL because `result_contract` does not exist.

- [ ] **Step 3: Implement a single shared parser**

```python
class AgentResultParseError(ValueError):
    def __init__(self, *, message_types: list[str], message_count: int) -> None:
        super().__init__("Could not parse structured output from agent result")
        self.message_types = message_types
        self.message_count = message_count


def parse_agent_result(raw: Any) -> DiscoveryRunResult:
    for candidate in _iter_result_candidates(raw):
        parsed = _coerce_result_dict(candidate)
        if parsed is not None:
            return DiscoveryRunResult(**_known_result_fields(parsed))
    messages = raw.get("messages", []) if isinstance(raw, dict) else []
    raise AgentResultParseError(
        message_types=[type(item).__name__ for item in messages],
        message_count=len(messages),
    )
```

`_iter_result_candidates` must yield `structured_response`, direct dictionaries, and message contents in reverse order. `_coerce_result_dict` must support Pydantic objects, dictionaries, plain JSON, fenced JSON, and LangChain text content blocks. It must not log message bodies.

- [ ] **Step 4: Add failing status-invariant tests**

```python
def test_zero_candidate_partial_success_becomes_manual_review() -> None:
    result = DiscoveryRunResult(
        status="partial_success",
        evidence=[PageEvidence(evidence_type="job_list", content_hash="h")],
        candidates=[],
    )
    normalized = enforce_result_invariants(result)
    assert normalized.status == "needs_manual_review"
    assert normalized.block_reason == "parse_failed"


def test_success_requires_at_least_one_candidate() -> None:
    result = DiscoveryRunResult(status="succeeded", candidates=[])
    assert enforce_result_invariants(result).status == "failed"
```

- [ ] **Step 5: Implement invariants and replace the worker-local parser**

```python
def enforce_result_invariants(result: DiscoveryRunResult) -> DiscoveryRunResult:
    if result.status == "succeeded" and not result.candidates:
        return replace(result, status="failed", block_reason="parse_failed")
    if result.status == "partial_success" and not result.candidates:
        return replace(
            result,
            status="needs_manual_review",
            block_reason=result.block_reason or "parse_failed",
        )
    return result
```

Make `worker._parse_agent_result` a compatibility alias that calls the shared parser. On `AgentResultParseError`, record only message types/count and the trajectory stop reason, then return `failed + parse_failed`.

- [ ] **Step 6: Run parser and worker regression tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_result_contract.py tests/unit/test_job_discovery_worker.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

```powershell
git add backend/app/services/job_discovery/result_contract.py backend/app/services/job_discovery/worker.py tests/unit/test_job_discovery_result_contract.py tests/unit/test_job_discovery_worker.py
git commit -m "fix: make discovery results lossless and status-safe"
```

---

### Task 2: Add PATH C budgets, typed inventory, coverage, and checkpoints

**Files:**
- Create: `backend/app/services/job_discovery/runtime_state.py`
- Modify: `backend/app/config.py:86-103`
- Modify: `backend/app/services/job_discovery/schemas.py`
- Create: `tests/unit/test_job_discovery_runtime_state.py`
- Modify: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: `DiscoveryTaskInput`, monotonic time, and optional checkpoint dictionary.
- Produces: `DiscoveryBudget`, `DiscoveredJob`, `CategoryCoverage`, `DiscoveryRunState`, `from_task()`, `to_checkpoint()`, `from_checkpoint()`, `add_job()`, `next_unfinished_job()`, `mark_complete()`, `has_unfinished_jobs`, `DiscoveryBudget.exhausted`, and `summary_counts()`.

- [ ] **Step 1: Add failing configuration tests**

```python
def test_site_wide_discovery_budget_defaults() -> None:
    settings = make_settings()
    assert settings.job_discovery_max_jobs_per_task == 200
    assert settings.job_discovery_max_runtime_seconds == 1800
    assert settings.job_discovery_page_timeout_seconds == 45
    assert settings.job_discovery_job_retry_limit == 2


@pytest.mark.parametrize("value", [39, 51])
def test_page_timeout_rejects_outside_approved_range(value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(job_discovery_page_timeout_seconds=value)
```

- [ ] **Step 2: Run the config tests and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_config.py -q`

Expected: FAIL because the new settings do not exist.

- [ ] **Step 3: Add the approved PATH C settings**

```python
job_discovery_max_jobs_per_task: int = Field(default=200, ge=1, le=2000)
job_discovery_max_runtime_seconds: int = Field(default=1800, ge=60, le=7200)
job_discovery_page_timeout_seconds: int = Field(default=45, ge=40, le=50)
job_discovery_job_retry_limit: int = Field(default=2, ge=0, le=5)
```

Do not change `job_discovery_max_candidates_per_task`; PATH A/B continue using their current behavior.

- [ ] **Step 4: Add failing inventory and checkpoint tests**

```python
def test_inventory_deduplicates_platform_id_before_url() -> None:
    state = DiscoveryRunState.new(task_id="t1", start_url="https://company.test")
    first = state.add_job(platform_job_id="42", detail_url="https://a.test/jobs/42", title_hint="A")
    second = state.add_job(platform_job_id="42", detail_url="https://b.test/position/42", title_hint="A")
    assert first.job_key == second.job_key
    assert len(state.inventory) == 1


def test_checkpoint_round_trip_preserves_unfinished_jobs() -> None:
    state = DiscoveryRunState.new(task_id="t1", start_url="https://company.test")
    job = state.add_job(platform_job_id="42", detail_url="https://a.test/jobs/42", title_hint="A")
    restored = DiscoveryRunState.from_checkpoint(state.to_checkpoint())
    assert restored.inventory[job.job_key].status == JobState.discovered
```

- [ ] **Step 5: Implement typed runtime state**

```python
class JobState(StrEnum):
    discovered = "discovered"
    fetching = "fetching"
    complete = "complete"
    retryable = "retryable"
    blocked = "blocked"
    removed = "removed"


@dataclass
class DiscoveredJob:
    job_key: str
    platform_job_id: str | None
    detail_url: str | None
    title_hint: str | None
    list_page_url: str | None
    category: str | None
    discovery_source: str
    status: JobState = JobState.discovered
    attempts: int = 0
    evidence_hash: str | None = None
    last_error: str | None = None


@dataclass
class DiscoveryRunState:
    task_id: str
    start_url: str
    allowed_domains: set[str]
    inventory: dict[str, DiscoveredJob]
    category_coverage: dict[str, CategoryCoverage]
    completed_job_keys: set[str]
    continuation_cursor: dict[str, Any] | None
    errors: list[dict[str, str]]
    budget: DiscoveryBudget
```

Checkpoint serialization must exclude complete JD bodies, cookies, secrets, and live Playwright objects.

- [ ] **Step 6: Extend public result schemas with storage and coverage fields**

Add `storage_uri: str | None` to `PageEvidence`, `field_evidence: dict[str, dict[str, Any]]` to `NormalizedJobCandidate`, and these defaulted fields to `DiscoveryRunResult`: `declared_job_count`, `discovered_job_count`, `completed_job_count`, `failed_job_count`, `category_coverage`, `coverage_complete`, `continuation_available`, `unresolved_job_keys`, and `diagnostics`. Preserve all existing constructor defaults.

- [ ] **Step 7: Run state/config/schema regression tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_config.py tests/unit/test_job_discovery_runtime_state.py tests/unit/test_job_discovery_tools.py -q`

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

```powershell
git add backend/app/config.py backend/app/services/job_discovery/runtime_state.py backend/app/services/job_discovery/schemas.py tests/unit/test_config.py tests/unit/test_job_discovery_runtime_state.py
git commit -m "feat: add typed site discovery runtime state"
```

---

### Task 3: Persist resumable checkpoints and field evidence

**Files:**
- Create: `alembic/versions/20260721_0012_supervisor_discovery_checkpoint.py`
- Modify: `backend/app/db/models.py:992-1111`
- Modify: `backend/app/repositories/job_discovery.py`
- Create: `tests/unit/test_supervisor_checkpoint_migration_contract.py`
- Modify: `tests/unit/test_job_discovery_repository.py`
- Modify: `tests/integration/test_mysql_migration.py`

**Interfaces:**
- Consumes: serialized `DiscoveryRunState` checkpoints and field-evidence dictionaries.
- Produces: nullable `JobDiscoveryTask.checkpoint_json`, nullable `DiscoveredJobCandidate.field_evidence_json`, `save_task_checkpoint(db, task, checkpoint)`, and `clear_task_checkpoint(db, task)`.

- [ ] **Step 1: Write failing model and repository tests**

```python
def test_save_and_clear_discovery_checkpoint(db_session, queued_task) -> None:
    save_task_checkpoint(db_session, queued_task, {"cursor": {"page": 3}})
    assert queued_task.checkpoint_json == {"cursor": {"page": 3}}
    clear_task_checkpoint(db_session, queued_task)
    assert queued_task.checkpoint_json is None


def test_candidate_model_accepts_field_evidence() -> None:
    candidate = DiscoveredJobCandidate(field_evidence_json={"title": {"source_type": "json_path"}})
    assert candidate.field_evidence_json["title"]["source_type"] == "json_path"
```

- [ ] **Step 2: Run repository/model tests and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_repository.py tests/unit/test_supervisor_checkpoint_migration_contract.py -q`

Expected: FAIL because columns and repository functions do not exist.

- [ ] **Step 3: Add ORM fields and migration**

```python
revision = "20260721_0012"
down_revision = "ffc4f5917966"


def upgrade() -> None:
    op.add_column("job_discovery_tasks", sa.Column("checkpoint_json", sa.JSON(), nullable=True))
    op.add_column(
        "discovered_job_candidates",
        sa.Column("field_evidence_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discovered_job_candidates", "field_evidence_json")
    op.drop_column("job_discovery_tasks", "checkpoint_json")
```

- [ ] **Step 4: Implement repository-only checkpoint writes**

```python
def save_task_checkpoint(db: Session, task: JobDiscoveryTask, checkpoint: dict[str, Any]) -> None:
    task.checkpoint_json = checkpoint
    db.flush()


def clear_task_checkpoint(db: Session, task: JobDiscoveryTask) -> None:
    task.checkpoint_json = None
    db.flush()
```

- [ ] **Step 5: Verify SQLite contracts and guarded MySQL roundtrip**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_supervisor_checkpoint_migration_contract.py tests/unit/test_job_discovery_repository.py -q`

Expected: PASS.

When an isolated `_test` MySQL URL is available, run:

```powershell
$env:ALLOW_DESTRUCTIVE_MYSQL_TESTS='1'
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe downgrade -1
.\.venv\Scripts\alembic.exe upgrade head
```

Expected: upgrade/downgrade/upgrade succeeds; never run this against a non-`_test` database.

- [ ] **Step 6: Commit Task 3**

```powershell
git add alembic/versions/20260721_0012_supervisor_discovery_checkpoint.py backend/app/db/models.py backend/app/repositories/job_discovery.py tests/unit/test_supervisor_checkpoint_migration_contract.py tests/unit/test_job_discovery_repository.py tests/integration/test_mysql_migration.py
git commit -m "feat: persist supervisor discovery checkpoints"
```

---

### Task 4: Implement the persistent browser session

**Files:**
- Create: `backend/app/services/job_discovery/browser_session.py`
- Modify: `backend/app/services/job_discovery/deepagents_runner.py:1123-1373,1602-1749`
- Create: `tests/integration/test_job_discovery_browser_session.py`
- Modify: `tests/integration/test_job_discovery_deepagents.py:438-481`

**Interfaces:**
- Consumes: `Settings`, allowed domains, and an optional Playwright factory for tests.
- Produces: `BrowserSession`, `PageSnapshot`, `InteractiveElement`, `NetworkRecord`, and `InteractionResult`.

- [ ] **Step 1: Add a failing stateful-navigation test**

```python
def test_clicks_second_rendered_job_link_without_restarting_browser(local_site_url: str) -> None:
    with BrowserSession(page_timeout_seconds=45, headless=True) as session:
        first = session.open(f"{local_site_url}/careers")
        jobs = [item for item in first.interactives if item.kind == "job_card"]
        assert len(jobs) == 2
        detail = session.click(jobs[1].element_id)
        assert "Backend Engineer" in detail.snapshot.visible_text
        assert session.launch_count == 1
```

The fixture page must place the desired job second so the existing `click_link` bug is covered.

- [ ] **Step 2: Run the browser-session test and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_job_discovery_browser_session.py -q`

Expected: FAIL because `BrowserSession` does not exist.

- [ ] **Step 3: Implement persistent lifecycle and observations**

```python
@dataclass(frozen=True)
class InteractiveElement:
    element_id: str
    kind: str
    text: str
    href: str | None
    selector: str


@dataclass(frozen=True)
class NetworkRecord:
    url: str
    method: str
    content_type: str
    parsed_json: dict[str, Any] | list[Any] | None


@dataclass(frozen=True)
class PageSnapshot:
    page_id: str
    url: str
    title: str
    visible_text: str
    dom_excerpt: str
    interactives: list[InteractiveElement]
    embedded_data: list[dict[str, Any]]
    blocked_reason: str | None = None


@dataclass(frozen=True)
class InteractionResult:
    snapshot: PageSnapshot
    route_changed: bool
    new_network_records: list[NetworkRecord]


class BrowserSession:
    def __enter__(self) -> BrowserSession:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        self._main_page_id = "main"
        self._pages = {self._main_page_id: self._page}
        self._install_network_capture(self._page)
        self.launch_count += 1
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._context.close()
        self._browser.close()
        self._playwright.stop()

    def open(self, url: str) -> PageSnapshot:
        self._assert_allowed_url(url)
        self._page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        self._wait_for_stable_observation(self._page)
        return self._snapshot(self._page, page_id=self._main_page_id)

    def click(self, element_id: str) -> InteractionResult:
        locator = self._element_locators[element_id]
        before_url = self._page.url
        before_network_count = len(self._network_records)
        locator.click(timeout=self._timeout_ms)
        self._wait_for_stable_observation(self._page)
        return InteractionResult(
            snapshot=self._snapshot(self._page, page_id=self._main_page_id),
            route_changed=self._page.url != before_url,
            new_network_records=self._network_records[before_network_count:],
        )

    def scroll(self) -> InteractionResult:
        before_network_count = len(self._network_records)
        self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        self._wait_for_stable_observation(self._page)
        return InteractionResult(
            snapshot=self._snapshot(self._page, page_id=self._main_page_id),
            route_changed=False,
            new_network_records=self._network_records[before_network_count:],
        )

    def go_back(self) -> PageSnapshot:
        self._page.go_back(wait_until="domcontentloaded", timeout=self._timeout_ms)
        return self._snapshot(self._page, page_id=self._main_page_id)

    def open_detail_page(self, url: str) -> PageSnapshot:
        self._assert_allowed_url(url)
        detail_page = self._context.new_page()
        page_id = uuid.uuid4().hex
        self._pages[page_id] = detail_page
        self._install_network_capture(detail_page)
        detail_page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        self._wait_for_stable_observation(detail_page)
        return self._snapshot(detail_page, page_id=page_id)

    def close_page(self, page_id: str) -> None:
        page = self._pages.pop(page_id)
        page.close()

    def network_records(self) -> list[NetworkRecord]:
        return list(self._network_records)
```

Use one Chromium browser/context for the task. Stable element IDs map to locators inside the session and are never serialized into checkpoints.

- [ ] **Step 4: Implement rendered interaction and network capture**

Capture anchors, buttons, role links, likely cards, route changes, popups, XHR, Fetch, GraphQL, JSON-LD, `__NEXT_DATA__`, and hydration scripts. Return response metadata and parsed JSON only when bounded and valid; do not place secrets or cookies in results.

- [ ] **Step 5: Rebind compatibility tools to the active session**

Replace static implementations of `open_rendered_url`, `extract_rendered_job_evidence`, `click_link`, `screenshot`, and `go_back` with closures over a `BrowserSession` when PATH C runtime is present. Retain legacy signatures for existing tests and PATH A/B imports. Fix `click_link` so all links are examined.

- [ ] **Step 6: Run browser and existing Deep Agents tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_job_discovery_browser_session.py tests/integration/test_job_discovery_deepagents.py -q`

Expected: PASS, including a real screenshot result and second-link navigation.

- [ ] **Step 7: Commit Task 4**

```powershell
git add backend/app/services/job_discovery/browser_session.py backend/app/services/job_discovery/deepagents_runner.py tests/integration/test_job_discovery_browser_session.py tests/integration/test_job_discovery_deepagents.py
git commit -m "feat: add persistent browser navigation for supervisor"
```

---

### Task 5: Classify pages and enforce evidence-based domain transitions

**Files:**
- Create: `backend/app/services/job_discovery/page_classifier.py`
- Create: `tests/unit/test_job_discovery_page_classifier.py`
- Modify: `backend/app/services/job_discovery/prompts/supervisor_base.txt`

**Interfaces:**
- Consumes: `PageSnapshot`, network records, and current allowed domains.
- Produces: `PageClassification`, `RecruitmentEntryCandidate`, `classify_page()`, `rank_recruitment_entries()`, and `approve_domain_transition()`.

- [ ] **Step 1: Add failing deterministic classification tests**

```python
def test_company_home_ranks_observed_careers_link() -> None:
    snapshot = snapshot_with_links([
        ("Products", "https://company.test/products"),
        ("加入我们", "https://jobs.company.test/campus"),
    ])
    classification = classify_page(snapshot, [])
    entries = rank_recruitment_entries(snapshot)
    assert classification.kind is PageKind.company_home
    assert entries[0].url == "https://jobs.company.test/campus"


def test_ats_domain_requires_observed_recruitment_link() -> None:
    assert approve_domain_transition(
        current_url="https://company.test",
        target_url="https://tenant.jobs.feishu.cn/123",
        observed_links={"https://tenant.jobs.feishu.cn/123"},
    )
    assert not approve_domain_transition(
        current_url="https://company.test",
        target_url="https://unrelated.test/jobs",
        observed_links=set(),
    )
```

- [ ] **Step 2: Run classifier tests and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_page_classifier.py -q`

Expected: FAIL because the classifier module does not exist.

- [ ] **Step 3: Implement deterministic classification and entry ranking**

```python
class PageKind(StrEnum):
    company_home = "company_home"
    career_home = "career_home"
    job_list = "job_list"
    job_detail = "job_detail"
    blocked = "blocked"
    unknown = "unknown"


@dataclass(frozen=True)
class PageClassification:
    kind: PageKind
    confidence: float
    signals: list[str]
```

Use JSON-LD/embedded-data signals first, then DOM sections, rendered cards, interactives, and text. Only `unknown` or conflicting results are sent to the Supervisor.

- [ ] **Step 4: Narrow the Supervisor prompt**

The prompt must state that the Agent chooses only from observed element IDs/URLs, never invents a destination, never owns job counts, and never serializes the final candidate collection.

- [ ] **Step 5: Run classifier and prompt-wiring tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_page_classifier.py tests/integration/test_job_discovery_deepagents.py -q`

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

```powershell
git add backend/app/services/job_discovery/page_classifier.py backend/app/services/job_discovery/prompts/supervisor_base.txt tests/unit/test_job_discovery_page_classifier.py tests/integration/test_job_discovery_deepagents.py
git commit -m "feat: classify recruitment pages before agent decisions"
```

---

### Task 6: Discover every job through categories, pagination, load-more, and scrolling

**Files:**
- Create: `backend/app/services/job_discovery/site_navigator.py`
- Create: `tests/integration/test_job_discovery_site_navigator.py`
- Create: `tests/fixtures/job_discovery/site_wide/company_home.html`
- Create: `tests/fixtures/job_discovery/site_wide/career_list.html`
- Create: `tests/fixtures/job_discovery/site_wide/job_detail.html`
- Create: `tests/fixtures/job_discovery/site_wide/hash_router.html`
- Create: `tests/fixtures/job_discovery/site_wide/infinite_scroll.html`
- Create: `tests/fixtures/job_discovery/site_wide/drawer_jobs.html`

**Interfaces:**
- Consumes: `BrowserSession`, `DiscoveryRunState`, `PageClassification`, and a `NavigationDecisionProvider` protocol.
- Produces: `SiteNavigator.discover_inventory()`, `SiteNavigator.collect_next_detail()`, and updated coverage/inventory state.

- [ ] **Step 1: Add failing full-inventory tests**

```python
def test_traverses_categories_and_numbered_pages_without_duplicates(site_runtime) -> None:
    state = site_runtime.discover("/company-home")
    assert state.category_coverage["校园招聘"].complete
    assert state.category_coverage["社会招聘"].complete
    assert len(state.inventory) == 7


def test_infinite_scroll_stops_after_three_empty_cycles(site_runtime) -> None:
    state = site_runtime.discover("/infinite-scroll")
    assert len(state.inventory) == 5
    assert state.continuation_cursor is None
    assert site_runtime.browser.empty_scroll_cycles == 3


def test_200_job_limit_produces_resumable_cursor(site_runtime) -> None:
    state = site_runtime.discover("/jobs?count=250", max_jobs=200)
    assert len(state.inventory) == 200
    assert state.continuation_cursor["reason"] == "job_limit"
```

- [ ] **Step 2: Run navigator tests and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_job_discovery_site_navigator.py -q`

Expected: FAIL because `SiteNavigator` and fixtures do not exist.

- [ ] **Step 3: Implement category and pagination traversal**

```python
class NavigationDecisionProvider(Protocol):
    def choose_recruitment_entry(self, candidates: list[RecruitmentEntryCandidate]) -> str:
        raise NotImplementedError

    def choose_ambiguous_control(self, snapshot: PageSnapshot, purpose: str) -> str | None:
        raise NotImplementedError


class SiteNavigator:
    def discover_inventory(self, start_url: str) -> DiscoveryRunState:
        snapshot = self._browser.open(start_url)
        snapshot = self._reach_job_list(snapshot)
        for category in self._top_level_categories(snapshot):
            self._traverse_category(category)
            if self._state.budget.exhausted:
                break
        return self._state

    def collect_next_detail(self) -> DetailCollection | None:
        job = self._state.next_unfinished_job()
        if job is None:
            return None
        return self._collect_detail_with_retry(job)


@dataclass(frozen=True)
class DetailCollection:
    job_key: str
    snapshot: PageSnapshot
    structured_payloads: list[dict[str, Any]]
    collection_method: str
```

Support numbered pages, next controls, load-more buttons, infinite scrolling, API cursors, top-level recruitment tabs, card drawers, independent detail URLs, and hash routes. Prefer an observed `all` option; otherwise traverse top-level mutually exclusive categories without creating a filter Cartesian product.

- [ ] **Step 4: Implement deterministic termination rules**

Stop a list when last/disabled-next is observed, API has no next cursor, declared total is reached, two pages add no jobs, or three scroll cycles add no jobs/network records. Budget exits set a continuation cursor and never mark unprocessed jobs complete.

- [ ] **Step 5: Add checkpoint-resume integration test**

```python
def test_resume_skips_completed_jobs_and_continues_page_cursor(site_runtime) -> None:
    first = site_runtime.discover("/jobs?count=250", max_jobs=200)
    second = site_runtime.resume(first.to_checkpoint(), max_jobs=200)
    assert second.summary_counts().completed_or_discovered == 250
    assert second.duplicate_job_count == 0
```

- [ ] **Step 6: Run traversal and runtime-state tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_job_discovery_site_navigator.py tests/unit/test_job_discovery_runtime_state.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 6**

```powershell
git add backend/app/services/job_discovery/site_navigator.py tests/integration/test_job_discovery_site_navigator.py tests/fixtures/job_discovery/site_wide
git commit -m "feat: traverse complete recruitment inventories"
```

---

### Task 7: Store complete encrypted evidence artifacts

**Files:**
- Create: `backend/app/services/job_discovery/evidence_artifacts.py`
- Modify: `backend/app/services/job_discovery/worker.py:127-161`
- Modify: `scripts/run_job_discovery_worker.py`
- Create: `tests/unit/test_job_discovery_evidence_artifacts.py`
- Modify: `tests/unit/test_job_discovery_worker.py`
- Modify: `tests/security/test_no_sensitive_logging.py`

**Interfaces:**
- Consumes: `EncryptedObjectStore`, `task_id`, canonical job identity, complete rendered text, and structured payloads.
- Produces: `FullJobEvidence`, `EvidenceArtifactStore.store() -> PageEvidence`, and `EvidenceArtifactStore.load(storage_uri) -> FullJobEvidence`.

- [ ] **Step 1: Add failing encrypted artifact tests**

```python
def test_complete_body_is_encrypted_and_preview_is_bounded(object_store, memory_blob_store) -> None:
    service = EvidenceArtifactStore(object_store)
    body = "岗位职责\n" + ("完整内容" * 5000)
    full = FullJobEvidence(
        canonical_url="https://company.test/jobs/42",
        platform_job_id="42",
        page_title="Backend Engineer",
        full_visible_text=body,
        structured_payloads=[],
        captured_at="2026-07-21T00:00:00Z",
        content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        collection_metadata={"platform_job_id": "42"},
    )
    evidence = service.store(
        task_id="t1",
        job_key="job-42",
        evidence=full,
    )
    assert len(evidence.text_excerpt or "") <= 5000
    assert evidence.storage_uri.startswith("encrypted-object://job-discovery/t1/")
    assert service.load(evidence.storage_uri).full_visible_text == body
    assert body.encode("utf-8") not in next(iter(memory_blob_store.objects.values())).body
```

- [ ] **Step 2: Run artifact tests and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_evidence_artifacts.py -q`

Expected: FAIL because `EvidenceArtifactStore` does not exist.

- [ ] **Step 3: Implement the artifact contract**

```python
@dataclass(frozen=True)
class FullJobEvidence:
    canonical_url: str
    platform_job_id: str | None
    page_title: str
    full_visible_text: str
    structured_payloads: list[dict[str, Any]]
    captured_at: str
    content_hash: str
    collection_metadata: dict[str, Any]


class EvidenceArtifactStore:
    def store(self, *, task_id: str, job_key: str, evidence: FullJobEvidence) -> PageEvidence:
        serialized = json.dumps(asdict(evidence), ensure_ascii=False, sort_keys=True).encode("utf-8")
        compressed = gzip.compress(serialized)
        key = f"job-discovery/{task_id}/{evidence.content_hash}.json.gz"
        self._object_store.put(
            key=key,
            plaintext=compressed,
            content_type="application/gzip",
        )
        return PageEvidence(
            evidence_type="job_detail",
            url=evidence.canonical_url,
            title=evidence.page_title,
            content_hash=evidence.content_hash,
            text_excerpt=evidence.full_visible_text[:5000],
            storage_uri=f"encrypted-object://{key}",
            metadata={"job_key": job_key, **evidence.collection_metadata},
        )

    def load(self, storage_uri: str) -> FullJobEvidence:
        key = storage_uri.removeprefix("encrypted-object://")
        raw = gzip.decompress(self._object_store.get(key=key))
        return FullJobEvidence(**json.loads(raw.decode("utf-8")))
```

Serialize deterministic JSON, gzip it, and pass the compressed bytes to `EncryptedObjectStore.put`. Use key `job-discovery/{task_id}/{content_hash}.json.gz` and URI `encrypted-object://{key}`.

- [ ] **Step 4: Inject artifact storage into the worker entrypoint**

Extend `JobDiscoveryWorker.__init__` with an optional `EvidenceArtifactStore`; production `scripts/run_job_discovery_worker.py` must construct the S3 client, `S3BlobStore`, `EncryptedObjectStore`, and artifact service from validated settings and close the client on exit. Existing unit tests may inject an in-memory implementation.

- [ ] **Step 5: Persist `storage_uri` for both dict and dataclass evidence**

Update `_persist_evidence` so `PageEvidence.storage_uri` is passed to `upsert_evidence`; do not write full bodies into `text_excerpt` or logs.

- [ ] **Step 6: Run evidence, worker, storage, and logging tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_evidence_artifacts.py tests/unit/test_job_discovery_worker.py tests/unit/test_encrypted_storage.py tests/security/test_no_sensitive_logging.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 7**

```powershell
git add backend/app/services/job_discovery/evidence_artifacts.py backend/app/services/job_discovery/worker.py scripts/run_job_discovery_worker.py tests/unit/test_job_discovery_evidence_artifacts.py tests/unit/test_job_discovery_worker.py tests/security/test_no_sensitive_logging.py
git commit -m "feat: preserve complete encrypted JD evidence"
```

---

### Task 8: Extract complete structured JDs with field-level evidence

**Files:**
- Create: `backend/app/services/job_discovery/structured_extraction.py`
- Modify: `backend/app/services/job_discovery/tools/evidence_verifier.py`
- Modify: `backend/app/services/job_discovery/tools/candidate_packager.py`
- Modify: `backend/app/services/job_discovery/worker.py:164-262`
- Create: `tests/unit/test_job_discovery_structured_extraction.py`
- Modify: `tests/unit/test_job_discovery_tools.py`

**Interfaces:**
- Consumes: one `FullJobEvidence`, optional list-page identity hints, and an optional strict structured-output LLM.
- Produces: `FieldEvidence`, `ExtractedJob`, `extract_structured_job()`, and candidates whose fields all have valid source references.

- [ ] **Step 1: Add failing structured JSON and text-span tests**

```python
def test_json_fields_include_json_path_sources() -> None:
    evidence = full_evidence(payload={
        "job": {
            "id": "42",
            "title": "Backend Engineer",
            "description": "负责服务开发",
            "requirements": "熟悉 Python",
        }
    })
    job = extract_structured_job(evidence)
    assert job.candidate.title == "Backend Engineer"
    assert job.candidate.field_evidence["title"]["source_path"] == "$.job.title"


def test_unreferenced_llm_field_is_discarded() -> None:
    job = extract_structured_job(
        full_evidence(text="岗位名称：后端工程师\n岗位职责：负责服务开发"),
        llm=fake_llm(requirements="精通 Kubernetes", requirements_span=None),
    )
    assert job.candidate.requirements == ""
    assert "Unverified field removed: requirements" in job.candidate.normalization_warnings
```

- [ ] **Step 2: Run extraction tests and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_structured_extraction.py -q`

Expected: FAIL because the structured extractor does not exist.

- [ ] **Step 3: Implement the three-stage extractor**

```python
@dataclass(frozen=True)
class FieldEvidence:
    evidence_hash: str
    source_type: Literal["json_path", "text_span"]
    source_path: str | None = None
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class ExtractedJob:
    candidate: NormalizedJobCandidate
    field_evidence: dict[str, FieldEvidence]
    extraction_method: str
```

Attempt known structured keys and recursive schema mapping first, semantic section extraction second, and strict LLM extraction last. Validate every JSON path/span against the same `content_hash` before returning it.

- [ ] **Step 4: Separate multi-job evidence before extraction**

When a page truly contains multiple complete job details, create one evidence slice per job with its own hash and source boundaries before candidate extraction. Remove the existing two-segment ceiling for this new PATH C extractor; PATH A/B behavior remains unchanged.

- [ ] **Step 5: Strengthen verifier and persistence**

Reject cross-job evidence hashes, title/ID mismatches, list-only evidence, invalid spans, and candidate fields absent from referenced evidence. Preserve `field_evidence` through packaging and write it to `field_evidence_json` in `_persist_candidates`.

- [ ] **Step 6: Run extraction and deterministic-tool regressions**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_structured_extraction.py tests/unit/test_job_discovery_tools.py tests/unit/test_job_discovery_worker.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 8**

```powershell
git add backend/app/services/job_discovery/structured_extraction.py backend/app/services/job_discovery/tools/evidence_verifier.py backend/app/services/job_discovery/tools/candidate_packager.py backend/app/services/job_discovery/worker.py tests/unit/test_job_discovery_structured_extraction.py tests/unit/test_job_discovery_tools.py
git commit -m "feat: extract evidence-traceable JD fields"
```

---

### Task 9: Assemble PATH C results from typed state and wire the existing Supervisor

**Files:**
- Create: `backend/app/services/job_discovery/supervisor_orchestrator.py`
- Modify: `backend/app/services/job_discovery/deepagents_runner.py:483-562,1757-1930,2010-2101`
- Modify: `backend/app/services/job_discovery/worker.py:516-565`
- Modify: `backend/app/services/job_discovery/prompts/supervisor_clean_start.txt`
- Modify: `backend/app/services/job_discovery/prompts/supervisor_snapshot_fallback.txt`
- Create: `tests/integration/test_job_discovery_supervisor_orchestrator.py`
- Modify: `tests/integration/test_job_discovery_worker_strategy.py`

**Interfaces:**
- Consumes: task input, settings, optional model, optional PATH A/B snapshot context, optional checkpoint, and artifact store.
- Produces: `run_supervisor_discovery(*, task: DiscoveryTaskInput, settings: Settings, model: Any, artifact_store: EvidenceArtifactStore, checkpoint: dict[str, Any] | None, snapshot_context: dict[str, Any] | None, checkpoint_callback: Callable[[dict[str, Any]], None] | None) -> DiscoveryRunResult` assembled from typed state.

- [ ] **Step 1: Add failing proof that final LLM output cannot erase collected jobs**

```python
def test_empty_final_agent_message_does_not_erase_collected_candidates(runtime_factory) -> None:
    runtime = runtime_factory(agent_final_output={"messages": [AIMessage(content="")]})
    result = runtime.run("/company-home")
    assert result.status == "succeeded"
    assert len(result.candidates) == 3
    assert len(result.evidence) == 3
```

- [ ] **Step 2: Add failing PATH A/B fallback compatibility tests**

```python
def test_snapshot_context_starts_path_c_without_rerunning_completed_steps(runtime_factory) -> None:
    result = runtime_factory(snapshot_context={
        "source": "snapshot",
        "completed_steps": [{"tool": "triage_link", "result": {"site_type": "career_site"}}],
        "failed_step": {"tool": "run_web_navigation", "error": "schema drift"},
    }).run("/careers")
    assert result.candidates
    assert "triage_link" not in result.diagnostics["repeated_tools"]
```

- [ ] **Step 3: Run orchestrator tests and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_job_discovery_supervisor_orchestrator.py -q`

Expected: FAIL because the orchestrator does not exist.

- [ ] **Step 4: Implement the PATH C service boundary**

```python
def run_supervisor_discovery(
    *,
    task: DiscoveryTaskInput,
    settings: Settings,
    model: Any,
    artifact_store: EvidenceArtifactStore,
    checkpoint: dict[str, Any] | None = None,
    snapshot_context: dict[str, Any] | None = None,
    checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
) -> DiscoveryRunResult:
    state = (
        DiscoveryRunState.from_checkpoint(checkpoint)
        if checkpoint is not None
        else DiscoveryRunState.from_task(task, settings)
    )
    with BrowserSession.from_settings(settings, allowed_domains=state.allowed_domains) as browser:
        runtime = SupervisorRuntime(
            state=state,
            browser=browser,
            model=model,
            artifact_store=artifact_store,
            snapshot_context=snapshot_context,
            checkpoint_callback=checkpoint_callback,
        )
        runtime.discover_inventory()
        while state.has_unfinished_jobs and not state.budget.exhausted:
            runtime.collect_and_extract_next_job()
        return runtime.build_result()
```

This function creates/restores `DiscoveryRunState`, opens one `BrowserSession`, builds a runtime-bound Web Navigation Agent, runs deterministic inventory/detail loops, calls the Supervisor only for ambiguous decisions, persists checkpoints through the callback, and derives the final result from state.

- [ ] **Step 5: Bind runtime tools without module-global task state**

`build_web_navigation_agent` and `build_discovery_supervisor_agent` receive a runtime/tool-provider object. Tool closures operate on that runtime. Remove correctness dependence on `_nav_*` and `_web_nav_*`; keep legacy helpers only for compatibility tests until all callers migrate.

- [ ] **Step 6: Wire worker PATH C only**

Replace direct `agent.invoke()` and `_fallback_with_record_fields_if_agent_missed_evidence()` for `executor_type == "supervisor"` with `run_supervisor_discovery`. Pass `task.checkpoint_json`, the PATH A/B `snapshot_context`, artifact store, and a callback that calls repository `save_task_checkpoint` and commits at safe checkpoints. Clear the checkpoint only after `succeeded`; retain it for resumable `partial_success`.

Do not alter router matching, adapter loading, snapshot execution, or strategy counters.

- [ ] **Step 7: Derive status and coverage from state**

The assembler must enforce:

```python
if state.coverage_complete and state.all_discovered_jobs_resolved:
    status = "succeeded"
elif state.completed_job_keys:
    status = "partial_success"
elif state.manual_review_reason:
    status = "needs_manual_review"
else:
    status = "failed"
```

Include declared/discovered/completed/failed/candidate counts, category coverage, continuation availability, and unresolved job keys in the result summary fields.

- [ ] **Step 8: Run PATH C, worker, and router non-regression tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_job_discovery_supervisor_orchestrator.py tests/integration/test_job_discovery_worker_strategy.py tests/unit/test_strategy_router.py tests/unit/test_snapshot_executor.py tests/unit/test_domain_adapter.py -q`

Expected: PASS; router tests show no behavioral change.

- [ ] **Step 9: Commit Task 9**

```powershell
git add backend/app/services/job_discovery/supervisor_orchestrator.py backend/app/services/job_discovery/deepagents_runner.py backend/app/services/job_discovery/worker.py backend/app/services/job_discovery/prompts/supervisor_clean_start.txt backend/app/services/job_discovery/prompts/supervisor_snapshot_fallback.txt tests/integration/test_job_discovery_supervisor_orchestrator.py tests/integration/test_job_discovery_worker_strategy.py
git commit -m "feat: run PATH C from typed discovery state"
```

---

### Task 10: Build the golden dataset, acceptance gates, and operational documentation

**Files:**
- Create: `tests/fixtures/job_discovery/golden/moka.json`
- Create: `tests/fixtures/job_discovery/golden/feishu.json`
- Create: `tests/fixtures/job_discovery/golden/mioffice.json`
- Create: `tests/fixtures/job_discovery/golden/nextjs.json`
- Create: `tests/fixtures/job_discovery/golden/static_site.json`
- Create: `tests/integration/test_job_discovery_golden_dataset.py`
- Modify: `tests/manual/test_non_alibaba_urls.py`
- Modify: `docs/job-discovery-agent-workflow.md`
- Modify: `docs/job-discovery-agent-operations.md`
- Modify: `backend/app/services/job_discovery/CLAUDE.md`

**Interfaces:**
- Consumes: sanitized captured DOM/network fixtures and expected job annotations.
- Produces: repeatable accuracy metrics and a live smoke test that reuses production result assembly.

- [ ] **Step 1: Define the golden fixture schema**

```json
{
  "site_family": "moka",
  "start_url": "https://fixture.test/moka/careers",
  "declared_job_count": 1,
  "pages": [
    {
      "url": "https://fixture.test/moka/careers",
      "html_fixture": "moka-careers.html",
      "network_fixture": "moka-careers-network.json"
    },
    {
      "url": "https://fixture.test/moka/jobs/42",
      "html_fixture": "moka-job-42.html",
      "network_fixture": "moka-job-42-network.json"
    }
  ],
  "expected_jobs": [
    {
      "platform_job_id": "42",
      "title": "Backend Engineer",
      "location": ["深圳"],
      "responsibilities": "负责服务端系统开发",
      "requirements": "熟悉 Python",
      "detail_url": "https://fixture.test/moka/jobs/42"
    }
  ]
}
```

Fixtures must be sanitized and contain no cookies, tokens, referral codes that behave as credentials, email addresses, or personal data.

- [ ] **Step 2: Add failing aggregate acceptance assertions**

```python
def test_golden_dataset_meets_accuracy_gate(golden_results) -> None:
    assert golden_results.recruitment_entry_discovery_rate == 1.0
    assert golden_results.inventory_coverage == 1.0
    assert golden_results.silent_job_loss == 0
    assert golden_results.detail_evidence_coverage == 1.0
    assert golden_results.title_id_url_precision == 1.0
    assert golden_results.job_recall >= 0.98
    assert golden_results.section_text_coverage >= 0.95
    assert golden_results.cross_job_contamination == 0
    assert golden_results.unsupported_field_generation == 0
    assert golden_results.result_protocol_failures == 0
```

- [ ] **Step 3: Run the golden gate and confirm failure before fixtures/harness exist**

Run: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_job_discovery_golden_dataset.py -q`

Expected: FAIL because the harness and fixtures are incomplete.

- [ ] **Step 4: Implement fixture replay and metric calculation**

Replay each fixture through the same page classifier, inventory, evidence, extraction, verifier, and result assembler used by PATH C. Do not substitute a test-only parser or test-only candidate builder.

- [ ] **Step 5: Update the six-URL manual smoke test**

Remove its duplicated `_parse_agent_result`. Call the production PATH C service and record:

- declared/discovered/completed/failed counts;
- category coverage;
- evidence and candidate counts;
- stop reason and continuation availability;
- runtime and page count;
- external barrier classification.

Store no raw Agent messages or secrets in the JSON output.

- [ ] **Step 6: Update architecture and operations documentation**

Document persistent-session behavior, configuration variables, checkpoint/resume semantics, object-store evidence, status invariants, golden acceptance commands, and the unchanged PATH A/B/C routing boundary.

- [ ] **Step 7: Run the complete focused gate**

```powershell
.\.venv\Scripts\python.exe -m ruff check backend/app/services/job_discovery scripts/run_job_discovery_worker.py tests/unit/test_job_discovery_* tests/integration/test_job_discovery_*
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_result_contract.py tests/unit/test_job_discovery_runtime_state.py tests/unit/test_job_discovery_page_classifier.py tests/unit/test_job_discovery_evidence_artifacts.py tests/unit/test_job_discovery_structured_extraction.py tests/unit/test_job_discovery_worker.py tests/unit/test_job_discovery_tools.py tests/integration/test_job_discovery_browser_session.py tests/integration/test_job_discovery_site_navigator.py tests/integration/test_job_discovery_supervisor_orchestrator.py tests/integration/test_job_discovery_golden_dataset.py tests/integration/test_job_discovery_worker_strategy.py -q
```

Expected: Ruff passes and all focused tests pass.

- [ ] **Step 8: Run unchanged PATH A/B regression gates**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_domain_adapter.py tests/unit/test_snapshot_executor.py tests/unit/test_strategy_router.py tests/integration/test_job_discovery_deepagents.py -q`

Expected: PASS with unchanged router selection and fallback behavior.

- [ ] **Step 9: Run the opt-in six-URL live smoke test**

Run only when required API keys and external-service approval are available:

`.\.venv\Scripts\python.exe -u tests/manual/test_non_alibaba_urls.py`

Expected for public unblocked sites: no result-protocol failures, no silent loss, every completed candidate has independent full-detail evidence, and incomplete coverage has an explicit cursor/reason. Login/captcha/removed pages must be classified separately.

- [ ] **Step 10: Commit Task 10**

```powershell
git add tests/fixtures/job_discovery/golden tests/integration/test_job_discovery_golden_dataset.py tests/manual/test_non_alibaba_urls.py docs/job-discovery-agent-workflow.md docs/job-discovery-agent-operations.md backend/app/services/job_discovery/CLAUDE.md
git commit -m "test: enforce site-wide JD discovery accuracy"
```

---

## Final Verification and Review Gate

- [ ] Confirm `git diff --check` passes.
- [ ] Confirm no migration other than `20260721_0012` was renamed or altered.
- [ ] Confirm Strategy Router, PATH A, and PATH B diffs are absent except compatibility tests/import wiring explicitly listed above.
- [ ] Confirm task checkpoints contain no full JD body, cookies, local storage, tokens, or Playwright objects.
- [ ] Confirm full evidence objects are encrypted and referenced through `storage_uri`.
- [ ] Confirm `succeeded` is impossible with zero candidates.
- [ ] Confirm `partial_success` is impossible with zero complete candidates.
- [ ] Confirm every unresolved inventory item appears in result counters and continuation state.
- [ ] Confirm every candidate field has a valid source path/span or is omitted with a warning.
- [ ] Request code review using `superpowers:requesting-code-review` before integration.

Recommended release sequence:

1. Merge Tasks 1-3 with the new PATH C behavior disabled behind an internal construction boundary, preserving current runtime behavior.
2. Merge Tasks 4-8 and enable the new runtime in fixture/integration environments.
3. Pass the golden dataset and PATH A/B regression gates.
4. Run the opt-in six-URL smoke test and inspect coverage gaps.
5. Enable the new PATH C implementation in the development worker; production rollout remains a separate operational decision.
