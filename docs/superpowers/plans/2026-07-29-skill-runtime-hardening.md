# Skill Runtime Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the default Skill job-discovery runtime retry-safe, actually bounded, coverage-verifiable, independent from the legacy Supervisor module, and capable of encrypted object-store audit retention.

**Architecture:** A run-specific artifact directory is created beneath each task. The runtime owns bounded tool execution and artifact validation; Worker owns state transitions and persistence. An optional injected `EncryptedObjectStore` promotes task artifacts to encrypted object keys before evidence is written.

**Tech Stack:** Python 3.12, Deep Agents, pytest, SQLAlchemy, existing `EncryptedObjectStore`.

## Global Constraints

- No Adapter, strategy URL matching, login bypass, CAPTCHA bypass, or private endpoint construction in the Skill path.
- Each task attempt writes only below its task/run directory.
- Worker persists only sanitized trace data and artifact URIs, never model messages or credentials.
- Object-store upload failures produce manual review; they never silently leave a task marked successful.

---

### Task 1: Run-isolated artifacts and bounded tool policy

**Files:**
- Modify: `backend/app/services/job_discovery/skill_artifacts.py`
- Modify: `backend/app/services/job_discovery/skill_runtime.py`
- Modify: `tests/unit/test_job_discovery_skill_artifacts.py`
- Modify: `tests/unit/test_job_discovery_skill_runtime.py`

- [ ] Write failing tests showing a second run gets a fresh output directory and a third browse/second coverage call is rejected.
- [ ] Run the targeted tests and verify the current implementation fails.
- [ ] Add `run_id`, `SkillToolPolicy`, output-path validation, browse/coverage counters, and configured page/candidate limits.
- [ ] Re-run targeted tests and commit.

### Task 2: Artifact proof and legacy dependency removal

**Files:**
- Create: `backend/app/services/job_discovery/llm_factory.py`
- Modify: `backend/app/services/job_discovery/deepagents_runner.py`
- Modify: `backend/app/services/job_discovery/skill_runtime.py`
- Modify: `tests/unit/test_job_discovery_skill_runtime.py`

- [ ] Write failing tests for unreferenced candidate evidence and for the independent LLM factory import.
- [ ] Run targeted tests and verify failure.
- [ ] Validate candidate references against evidence pages; enforce configured candidate caps; extract the model builder to `llm_factory.py`.
- [ ] Re-run targeted tests and commit.

### Task 3: Encrypted object-store publication and Worker handling

**Files:**
- Modify: `backend/app/services/job_discovery/skill_artifacts.py`
- Modify: `backend/app/services/job_discovery/skill_runtime.py`
- Modify: `backend/app/services/job_discovery/worker.py`
- Modify: Worker bootstrap call-site
- Modify: `tests/unit/test_job_discovery_worker.py`

- [ ] Write failing tests for encrypted artifact URIs and upload failure → manual review.
- [ ] Run tests and verify failure.
- [ ] Publish evidence artifacts via injected `EncryptedObjectStore`; replace file URI with `object://` key only after successful writes.
- [ ] Make Worker persist published URI and route upload failure to manual review.
- [ ] Re-run focused suite and commit.

### Task 4: Live runtime verification and documentation

**Files:**
- Modify: `backend/app/services/job_discovery/ARCHITECTURE.zh-CN.md`
- Create/modify: `tests/integration/test_job_discovery_skill_worker.py`

- [ ] Add an injected-runtime integration test covering task status, candidate, evidence URI, and trajectory.
- [ ] Run focused unit/integration suite.
- [ ] Execute the existing ten-URL fixture through the production runtime under its live-test gate; record only outcome metadata.
- [ ] Update storage and operational documentation; commit.
