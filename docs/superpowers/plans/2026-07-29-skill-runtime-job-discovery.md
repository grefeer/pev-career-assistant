# Skill Runtime Job Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the legacy Supervisor/Strategy/Adapter execution path with a task-scoped `create_deep_agent + job-discovery Skill + bounded tools` runtime while preserving the existing Worker, review queue, MySQL records, and audit evidence.

**Architecture:** The Worker remains the sole task claimant and persistence owner. A new Skill runtime receives one `DiscoveryTaskInput`, creates an isolated artifact directory, invokes the validated Skill runtime, converts merged candidates and evidence files to `DiscoveryRunResult`, and returns a sanitized tool trace. The Worker persists evidence/candidates into existing tables and records the trace in the existing trajectory table; full artifacts receive stable storage URIs.

**Tech Stack:** Python 3.12, FastAPI configuration, SQLAlchemy/MySQL models/repositories, Deep Agents, Playwright, local task artifact directories, optional encrypted MinIO/S3 object store.

## Global Constraints

- Keep API routes free of business logic and repositories limited to SQL.
- Do not use site adapters, URL-pattern strategy matching, login bypass, CAPTCHA bypass, or private endpoint construction.
- Runtime must invoke `create_deep_agent + Skill + bounded tool` for normal discovery.
- Every run receives an isolated task directory; no shared `skill/job-discovery/output` directory may be used.
- Persist candidates in `discovered_job_candidates`, evidence in `job_discovery_evidence`, and tool trajectory in `job_discovery_trajectories`.
- Store no credentials, raw tokens, or full model messages in SQL logs or trajectories.

---

### Task 1: Task-scoped artifact model and tests

**Files:**
- Create: `backend/app/services/job_discovery/skill_artifacts.py`
- Create: `tests/unit/test_job_discovery_skill_artifacts.py`

**Interfaces:**
- Produces `SkillArtifactStore(task_id: str, root: Path)`.
- Produces `prepare() -> Path`, `artifact_uri(path: Path) -> str`, and `iter_evidence() -> list[SkillArtifact]`.

- [ ] **Step 1: Write failing tests**

```python
def test_artifacts_are_isolated_by_task(tmp_path: Path) -> None:
    first = SkillArtifactStore("task-a", tmp_path).prepare()
    second = SkillArtifactStore("task-b", tmp_path).prepare()
    assert first != second
    assert first.name == "job-discovery"
```

- [ ] **Step 2: Run the targeted test and verify failure**

Run: `python -m pytest tests/unit/test_job_discovery_skill_artifacts.py -q`

- [ ] **Step 3: Implement artifact directory creation, safe relative paths, and evidence enumeration**

```python
class SkillArtifactStore:
    def prepare(self) -> Path:
        target = self.root / self.task_id / "skill" / "job-discovery"
        shutil.copytree(SKILL_SOURCE, target, ignore=shutil.ignore_patterns("output", "__pycache__"))
        (target / "output").mkdir(exist_ok=True)
        return target
```

- [ ] **Step 4: Run targeted tests and commit**

Run: `python -m pytest tests/unit/test_job_discovery_skill_artifacts.py -q`

### Task 2: Skill executor contract and tests

**Files:**
- Create: `backend/app/services/job_discovery/skill_runtime.py`
- Create: `tests/unit/test_job_discovery_skill_runtime.py`

**Interfaces:**
- Consumes `DiscoveryTaskInput`, `Settings`, task id, and `SkillArtifactStore`.
- Produces `SkillRuntimeResult(result: DiscoveryRunResult, trace_steps: list[dict], artifact_root: Path)`.
- `run_skill_discovery(...)` uses `create_deep_agent` through the validated Skill runner and maps only persisted candidate/evidence artifacts to result objects.

- [ ] **Step 1: Write failing contract tests**

```python
def test_runtime_maps_verified_artifacts_to_result(tmp_path: Path, monkeypatch) -> None:
    runtime = SkillDiscoveryRuntime(settings, artifact_root=tmp_path)
    monkeypatch.setattr(runtime, "_invoke", fake_verified_run)
    outcome = runtime.run(task_input, task_id="task-1")
    assert outcome.result.status == "succeeded"
    assert outcome.result.candidates[0].responsibilities
    assert outcome.trace_steps[0]["tool"] == "browse"
```

- [ ] **Step 2: Run targeted test and verify failure**

Run: `python -m pytest tests/unit/test_job_discovery_skill_runtime.py -q`

- [ ] **Step 3: Implement a bounded runtime**

```python
class SkillDiscoveryRuntime:
    def run(self, task: DiscoveryTaskInput, *, task_id: str) -> SkillRuntimeResult:
        artifact = SkillArtifactStore(task_id, self.artifact_root)
        skill_dir = artifact.prepare()
        record = self._invoke(task=task, skill_dir=skill_dir)
        return _result_from_skill_artifacts(record, skill_dir)
```

- [ ] **Step 4: Verify candidate body, coverage failure, and no raw-model-message persistence**

Run: `python -m pytest tests/unit/test_job_discovery_skill_runtime.py -q`

- [ ] **Step 5: Commit**

### Task 3: Worker migration and persistence wiring

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/services/job_discovery/worker.py`
- Modify: `tests/unit/test_job_discovery_worker.py`

**Interfaces:**
- Adds `job_discovery_skill_runtime_enabled: bool = True` and `job_discovery_skill_artifact_root: str`.
- Worker calls `SkillDiscoveryRuntime.run()` before all legacy strategy/supervisor routing when enabled.
- Worker writes `execution_path="skill_agent"`, artifact URIs, and trace steps through existing persistence helpers.

- [ ] **Step 1: Write failing Worker test**

```python
def test_worker_uses_skill_runtime_not_legacy_supervisor(worker, monkeypatch) -> None:
    monkeypatch.setattr(worker, "_skill_runtime", fake_runtime)
    legacy = monkeypatch.patch("...build_discovery_supervisor_agent")
    assert worker.run_once() == 1
    legacy.assert_not_called()
```

- [ ] **Step 2: Run targeted test and verify failure**

Run: `python -m pytest tests/unit/test_job_discovery_worker.py -k skill_runtime -q`

- [ ] **Step 3: Add early Skill execution branch and persistence mapping**

```python
if self.settings.job_discovery_skill_runtime_enabled:
    outcome = self._skill_runtime.run(task_input, task_id=task.id)
    _persist_evidence(db, task, outcome.result.evidence)
    _persist_candidates(db, task, outcome.result.candidates)
    _persist_skill_trace(db, task, outcome.trace_steps, outcome.result)
    _mark_task_from_result(...)
    db.commit()
    return 1
```

- [ ] **Step 4: Run Worker unit tests and commit**

Run: `python -m pytest tests/unit/test_job_discovery_worker.py -q`

### Task 4: Legacy execution disablement and audit documentation

**Files:**
- Modify: `backend/app/services/job_discovery/worker.py`
- Modify: `backend/app/services/job_discovery/ARCHITECTURE.zh-CN.md`
- Modify: `skill/job-discovery/SKILL.md`
- Test: `tests/integration/test_job_discovery_skill_worker.py`

**Interfaces:**
- Legacy adapter/strategy/supervisor code remains importable for rollback only and is not reached while Skill runtime flag is enabled.
- Task summary contains `execution_path="skill_agent"`, artifact root URI, coverage verdict, and candidate counts.

- [ ] **Step 1: Add an integration test with fake Skill runtime**

```python
def test_skill_worker_persists_trace_evidence_and_jds(db_session_factory, settings):
    worker = JobDiscoveryWorker(db_session_factory, settings, skill_runtime=fake_runtime)
    assert worker.run_once() == 1
    assert task.result_summary_json["execution_path"] == "skill_agent"
    assert trajectory.completed_steps
```

- [ ] **Step 2: Run integration test and verify failure**

Run: `python -m pytest tests/integration/test_job_discovery_skill_worker.py -q`

- [ ] **Step 3: Document exact MySQL and artifact locations**

- [ ] **Step 4: Run focused unit/integration suite and commit**

Run: `python -m pytest tests/unit/test_job_discovery_skill_artifacts.py tests/unit/test_job_discovery_skill_runtime.py tests/unit/test_job_discovery_worker.py tests/integration/test_job_discovery_skill_worker.py -q`

## Self-Review

- Coverage: tasks isolate artifacts, execute the Skill runtime, preserve existing MySQL persistence, disable legacy execution by default, and document all storage.
- No placeholder scan: no deferred implementation items are used; each task names concrete files, interfaces, tests, and commands.
- Type consistency: `SkillRuntimeResult` is produced by runtime, consumed by Worker, and converted only through existing `DiscoveryRunResult` persistence helpers.

## Execution Handoff

This plan is executed inline in the current session because the user explicitly requested autonomous implementation on `master` without a worktree.
