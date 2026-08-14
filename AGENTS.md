# AGENTS.md - Career Assistant Platform

## Project Context

Multi-agent personal career assistant. Full-stack: FastAPI + Vue 3 + MySQL + Redis + MinIO + Docker Compose. The default runtime is a self-built **adaptive Planner–Executor–Verifier (PEV) agent runtime** (`backend/app/services/agent_runtime/`) driving four career skills (`backend/app/services/career_skills/`). A Windows executor skeleton/simulator (`executor/`) supports human-reviewed form filling. See `CLAUDE.md` for the architecture and workflow map and `docs/pev-agent-architecture.zh-CN.md` for sequence diagrams.

> The previous LangGraph/Deep-Agents job-discovery pipeline (Supervisor / Web Navigation Agent / skill runtime / worker) has been retired and removed. Do not add code under those names; the current `job-discovery` is a career skill inside the PEV runtime.

## How to Work in This Repo

### Before Any Code Change

1. Read the relevant WP tech doc in `docs/` (WP1 for platform, WP2 for jobs) and `docs/pev-agent-architecture.zh-CN.md` for the PEV runtime.
2. Check `docs/platform-foundation-handover-summary.md` for the platform-foundation baseline.
3. Understand which layer your change belongs to (API/Service/Repository).
4. Check `backend/app/config.py` for any relevant feature flags (e.g. `agent_harness_enabled`).

### Layer Discipline (Mandatory)

```
api/routes/*.py     -> Parse request, call service, return DTO. NO SQL, NO business logic.
services/*.py       -> All business rules. NO raw SQL, NO HTTP concerns.
repositories/*.py   -> SQL only. NO business decisions.
domain/*.py         -> Pure contracts: enums, allowlists, validation helpers. NO imports from services/repositories/api.
```

When tracing code: `routes/ -> services/ -> repositories/`. Always.

Health-check SQL is an intentional infrastructure probe rather than business data access.

### Python Conventions

```python
# Imports: stdlib -> third-party -> project (absolute from repository root)
from __future__ import annotations
import json
from typing import Any

from langchain_openai import ChatOpenAI

from backend.app.config import Settings
from backend.app.services.agent_runtime.schemas import AgentTaskRequest, ExecutionPlan

# Type hints: modern syntax (Python 3.12+)
def fetch(url: str) -> tuple[str | None, str | None]:
    ...

# Dataclass patterns for deterministic tools
from dataclasses import dataclass

@dataclass
class TriageResult:
    site_type: str
    confidence: float
    recommended_action: str
```

### Adding a New Tool to a Career Skill

Tools are deterministic Python functions registered in the `ToolRegistry`. Each tool declares its input/output Pydantic models, allowed roles, and owning skill.

1. Implement the deterministic logic (a pure function or a small service call) — keep it side-effect-free aside from explicit persistence.
2. Define `InputModel` / `OutputModel` Pydantic schemas (`extra="forbid"`).
3. Register a `ToolDefinition` in `backend/app/services/career_skills/registry.py` under the right `skill_name` and `allowed_roles` (executor, verifier, or both).
4. If the skill is new, add a manifest entry in `career_skills/manifest.py` (`requires_evidence`, `supports_user_data`).
5. Add unit coverage: tool determinism in `tests/unit/` plus the skill-level test (`test_*pev_skill.py` / `test_job_matching_skill.py`).
6. Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_agent_runtime*.py tests/unit/test_*pev_skill.py tests/unit/test_job_matching_skill.py -q`

Remember: `ToolRegistry.invoke` converts any handler exception into a `ToolObservation(status="failed", error_code=...)`; never raise across the agent boundary. Tools without a `skill_name` are excluded from scoped (per-step) catalogs - register every Executor/Verifier tool under a skill so it's reachable, or it will be invisible to the Executor.

### Adding a New API Endpoint

1. Define DTO schemas in the appropriate `backend/app/api/*_schemas.py` module (field whitelists, never expose internal fields).
2. Add the route in `backend/app/api/routes/*.py`.
3. Register in `backend/app/api/router.py` if a new route module is added.
4. Admin-only routes: use `require_admin` dependency.
5. Student/user routes: use `get_current_user` dependency, filter by ownership + status.
6. Test: unit coverage for services and contract coverage for endpoints; add integration coverage when real infrastructure is involved.

### Working with the PEV Runtime

```python
# The runtime is assembled once in main.py lifespan and stored on app.state.agent_run_service.
# API requests just queue a run and stream events; the background task executes the PEV loop.

from backend.app.services.agent_runtime.service import AgentRunService
from backend.app.services.agent_runtime.schemas import AgentTaskRequest, AgentBudget

task = AgentTaskRequest(
    goal="...",
    allowed_skills=["job-discovery", "job-matching"],
    context={...},
    budget=AgentBudget(),
)
# service.queue_run(...) persists a queued run; execute_queued_run runs the PEV loop
# in a background task. Stream progress via GET /agent-runs/{id}/events/stream (SSE).

# Adding a new agent behavior:
# - Planner/Executor/Verifier each have a system-prompt constant and a decide() loop.
# - The harness (runtime.py) enforces budgets, state transitions, persistence, and
#   verification routing; do NOT add control flow to the agents that the harness owns.
# - New tool = see "Adding a New Tool to a Career Skill" above.
```

Key files: `runtime.py` (orchestrator), `planner_agent.py` / `executor_agent.py` / `verifier_agent.py`, `observation_projection.py` (shared bounded decision-state projection), `model_gateway.py` (DeepSeek, schema-first + local JSON retry), `tool_registry.py`, `schemas.py`, `service.py` (user-scoped business service).

### Model Gateway Notes

- DeepSeek `deepseek-v4-*` models must use `temperature=0` and `extra_body={"thinking":{"type":"disabled"}}`, and run through the provider `json_mode` structured transport (`prefer_local_json_validation=False`, keyed on the **model name**, not the base URL — an `OPENAI_BASE_URL` override must not change it). The fallback ladder stays: provider structured output -> local JSON retry -> `invalid_model_response`. A final `invalid_model_response` degrades the run to `waiting_user` (recoverable) — it does not fail the run.
- The model key is read only from the environment (`DEEPSEEK_API_KEY` / `OPENAI_API_KEY`); never hardcode or log it.

### Database Changes

1. Create Alembic migration: `alembic revision -m "description"`
2. Write `upgrade()` and `downgrade()` in the generated file
3. Update ORM models in `backend/app/db/models.py`
4. Run: `.\.venv\Scripts\alembic.exe upgrade head`
5. Test downgrade roundtrip: `.\.venv\Scripts\alembic.exe downgrade -1; .\.venv\Scripts\alembic.exe upgrade head`
6. Never rename an existing migration - create a new one

## Test Patterns

### Unit Tests (SQLite in-memory)

```python
# Use pytest fixtures for DB session
def test_something(db_session):
    repo = AgentRunRepository(db_session)
    result = repo.create_queued_run(...)
    assert result.status == RunStatus.QUEUED

# For PEV tools: test input->output determinism
def test_match_observed_jobs_orders_by_score():
    result = match_observed_jobs(jobs=[...], facts={...})
    assert result[0].job_id == expected_top
```

### Live Tests (gated)

```python
# Live PEV end-to-end and live skill smoke tests are env-gated.
pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_PEV_E2E"),
    reason="set RUN_LIVE_PEV_E2E=1",
)
```

### Opt-in Destructive MySQL Tests

```python
# Require: ALLOW_DESTRUCTIVE_MYSQL_TESTS=1
# Require: TEST_MYSQL_URL database name ends with _test
# Never run against production database
```

## Security Checklist for Any Change

- [ ] No secrets in code, logs, error messages, or audit records
- [ ] Student APIs only return `verified` jobs
- [ ] JobPosting completion/review/decision writes check `review_version` (409 on conflict)
- [ ] No code path grants `task:submit` scope
- [ ] No auto-skip of login/captcha/anti-bot
- [ ] Public-page fetching follows redirects manually and re-validates each hop (`_fetch_validated` + `_assert_public_url`); no 302 to private/cloud-metadata addresses
- [ ] API field whitelists in DTOs (never expose raw payloads or tokens)
- [ ] Device actions validate task lease, not just device token
- [ ] User A cannot read/modify User B's data (owner-scoped runs/events/artifacts)

## Common Pitfalls

1. **DeepSeek thinking mode**: When using `deepseek-v4-*` models, add `extra_body={"thinking": {"type": "disabled"}}` to avoid empty tool-call responses.

2. **Skill-authority scoping**: Each `PlanStep` allows exactly one skill. A step that mixes skills makes some tools invisible to the Executor and the deliverable is never produced. Decompose multi-deliverable goals into one step per deliverable.

3. **Evidence-bound tools**: Only tool-produced evidence (with `source_url` + `content_hash`) is persisted. Never trust a model-proposed URI.

4. **Duplicate tool calls**: A consecutive identical call after a success returns `duplicate_tool_call` without consuming budget. If a test expects a retry, make the first call fail (the dedup only suppresses repeats after success). Three consecutive no-progress decisions (deduped re-calls or a blocked search) are a stall: the executor stops and asks the user (`needs_user`, `_MAX_CONSECUTIVE_STALLS` in `executor_agent.py`) rather than burning turns.

5. **Opt-in test gates**: Tests requiring external services use exact env-var names (`RUN_LIVE_PEV_E2E`, `ALLOW_DESTRUCTIVE_MYSQL_TESTS`). Don't rename them.

6. **Budgets are hard ceilings**: `AgentBudget` / `ToolCallBudget` / `AgentTurnBudget` are enforced by the harness. Exhausting them fails the run with a stable error code (`replan_budget_exhausted`, `tool_budget_exhausted`, etc.); agents cannot exceed them. A verifier that keeps returning `RETRY_EXECUTOR` past the retry cap is a stuck loop, not a failure: the harness routes the step to `waiting_user` with the verifier feedback as the question (human-in-the-loop recovery), instead of failing the run. `recover()`/`resume()` resume the replan count from the persisted plan count (`max(0, revision - 1)`), so a crashed run can't re-spend budget already used on replanning.

7. **Frontend dirty state**: Admin/profile forms track dirty state. Navigating away shows a confirmation dialog. Don't disable this.

## File Reading Order (for onboarding)

| # | File | What to learn |
|---|------|---------------|
| 1 | `backend/app/config.py` | All settings, feature flags, validation |
| 2 | `backend/app/main.py` | FastAPI app + lifespan (assembles the PEV runtime) |
| 3 | `backend/app/db/models.py` | All ORM models (AgentRun, JobPosting, ApplicationTask, ...) |
| 4 | `backend/app/api/dependencies.py` | Auth injection |
| 5 | `backend/app/services/auth.py` | Registration, login, JWT |
| 6 | `backend/app/domain/agent_runtime.py` | PEV lifecycle contracts (roles, states, transitions) |
| 7 | `backend/app/services/agent_runtime/runtime.py` | PEV orchestrator (plan -> execute -> verify loop) |
| 8 | `backend/app/services/agent_runtime/schemas.py` | Decisions, observations, budgets |
| 9 | `backend/app/services/career_skills/registry.py` | The 8 tools across 4 skills |
| 10 | `docs/pev-agent-architecture.zh-CN.md` | Full architecture + sequence diagrams |
