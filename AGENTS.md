# AGENTS.md — Career Assistant Platform

## Project Context

Multi-agent campus-recruitment career assistant. Full-stack: FastAPI + Vue 3 + MySQL + Redis + MinIO + Docker Compose. The repository contains an opt-in LangGraph/Deep Agents job-discovery pipeline, evidence-based matching workflows, and a Windows executor skeleton/simulator for human-reviewed form filling. See `CLAUDE.md` for the architecture and workflow map.

## How to Work in This Repo

### Before Any Code Change

1. Read the relevant WP tech doc in `docs/` (WP1 for platform, WP2 for jobs)
2. Check `docs/platform-foundation-handover-summary.md` for the platform-foundation baseline; for job discovery also read `docs/functional-verification-summary-2026-07-19.md` and the job-discovery workflow/operations docs
3. Understand which layer your change belongs to (API/Service/Repository)
4. Check `backend/app/config.py` for any relevant feature flags

### Layer Discipline (Mandatory)

```
api/routes/*.py     → Parse request, call service, return DTO. NO SQL, NO business logic.
services/*.py       → All business rules. NO raw SQL, NO HTTP concerns.
repositories/*.py   → SQL only. NO business decisions.
domain/*.py         → Pure contracts: enums, allowlists, validation helpers. NO imports from services/repositories/api.
```

When tracing code: `routes/ → services/ → repositories/`. Always.

Known debt: `backend/app/api/routes/job_discovery.py` still performs candidate approval/rejection queries and writes directly. Do not copy that pattern; when changing those flows, move the business operation into a service and SQL into the repository.

### Python Conventions

```python
# Imports: stdlib → third-party → project (absolute from repository root)
from __future__ import annotations
import json
from typing import Any

import requests
from langchain_openai import ChatOpenAI

from backend.app.config import Settings
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate

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

### Adding a New Tool to Job Discovery

1. Put reusable deterministic logic in `backend/app/services/job_discovery/tools/`
2. Export reusable functions from `tools/__init__.py`
3. Add the JSON/agent-callable wrapper in `deepagents_runner.py` when the deterministic function's native types are not tool-call friendly
4. Add the wrapper to `final_tools` in `build_discovery_supervisor_agent()`
5. Add unit coverage in `tests/unit/test_job_discovery_tools.py` and wiring coverage in `tests/integration/test_job_discovery_deepagents.py`
6. Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_tools.py tests/integration/test_job_discovery_deepagents.py -q`

### Adding a New API Endpoint

1. Define DTO schemas in the appropriate `backend/app/api/*_schemas.py` module (field whitelists, never expose internal fields)
2. Add route in appropriate `backend/app/api/routes/*.py` file
3. Register in `backend/app/api/router.py` if new route module
4. Admin-only routes: use `require_admin` dependency
5. Student routes: use `get_current_user` dependency, filter by ownership + status
6. Test: unit coverage for services and contract coverage for endpoints; add integration coverage when real infrastructure is involved

### Working with the DeepAgent System

```python
# Build an agent — always use the factory, never construct manually
from backend.app.services.job_discovery.deepagents_runner import build_web_navigation_agent

agent = build_web_navigation_agent(settings=settings, model=model)

# Invoke with a structured prompt
result = agent.invoke({"messages": [HumanMessage(content=prompt)]})

# Parse structured output through the normalizer
parsed = _parse_web_navigation_agent_result(result)

# Adding a new tool to Web Navigation Agent:
# 1. Define function in deepagents_runner.py
# 2. Add to the tools list in build_web_navigation_agent()
# 3. Add to the tools list in create_web_navigation_subagent()
# 4. Mention in _WEB_NAVIGATION_SYSTEM_PROMPT
```

### Module-Level State (Double Tracker Pattern)

The web navigation system uses TWO parallel state trackers:

```python
# Used by _fetch_page() and run_web_navigation()
_web_nav_page_count, _web_nav_max_pages, _web_nav_history, _web_nav_current_url

# Used by open_url() and SubAgent tools
_nav_page_count, _nav_max_pages, _nav_history, _nav_current_url
```

When adding code that touches page counting, update the right tracker:
- Supervisor path → `_web_nav_*`
- SubAgent tool path → `_nav_*`

**Always declare `global` at the TOP of any function that modifies these**, before any conditionals.

### ReadGZH / WeChat Integration

```python
# WeChat URLs auto-detected and routed through ReadGZH proxy
# In _fetch_page() and open_url(), this happens transparently:
if _is_wechat_url(url):
    text, title, error = _fetch_wechat_via_readgzh(url)
    if error is None:
        return text, title, None
    # Falls through to: direct HTTP → browser fallback → error

# Runtime code first reads os.environ["READGZH_API_KEY"]. Set it explicitly
# before live tests; do not rely on checkout-specific ancestor paths.
```

### Database Changes

1. Create Alembic migration: `alembic revision -m "description"`
2. Write `upgrade()` and `downgrade()` in the generated file
3. Update ORM models in `backend/app/db/models.py`
4. Run: `.\.venv\Scripts\alembic.exe upgrade head`
5. Test downgrade roundtrip: `.\.venv\Scripts\alembic.exe downgrade -1; .\.venv\Scripts\alembic.exe upgrade head`
6. Never rename an existing migration — create a new one

## Test Patterns

### Unit Tests (SQLite in-memory)

```python
# Use pytest fixtures for DB session
def test_something(db_session):
    repo = JobDiscoveryRepository(db_session)
    result = repo.create_or_get_task(...)
    assert result.status == JobDiscoveryTaskStatus.QUEUED

# For agent tools: test input→output determinism
def test_triage_wechat_url():
    result = triage_link("https://mp.weixin.qq.com/s/abc123")
    assert result.site_type == "wechat_article"
```

### Integration and Live Tests

```python
# Only tests that access live Tencent/ReadGZH/public URLs are gated by this variable.
# The mocked Deep Agents integration suite does not require it.
pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_TENCENT_DISCOVERY"),
    reason="set RUN_LIVE_TENCENT_DISCOVERY=1",
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
- [ ] API field whitelists in DTOs (never expose raw payloads or tokens)
- [ ] Device actions validate task lease, not just device token
- [ ] User A cannot read/modify User B's data

## Common Pitfalls

1. **Python 3.12 `global` rules**: Global declarations must appear before any use of the variable in the function. Put them at the function top.

2. **DeepSeek thinking mode**: When using `deepseek-v4-*` models, add `extra_body={"thinking": {"type": "disabled"}}` to avoid empty tool-call responses.

3. **Opt-in test gates**: Tests requiring external services use exact env-var names. Don't rename them.

4. **Image/video-heavy WeChat content**: The current OCR helper inspects image inputs but does not extract text yet, so these cases require manual review.

5. **Idempotency keys**: Changing `agent_version` creates new tasks for the same URL. Design intent, not duplication bug.

6. **Worker lease timeouts**: Default 600s. If a task runs > 10 minutes, another worker can claim it. Consider increasing `job_discovery_task_timeout_seconds` for long-running discovery.

7. **Frontend dirty state**: Admin forms track dirty state. Navigating away shows confirmation dialog. Don't disable this.

## File Reading Order (for onboarding)

| # | File | What to learn |
|---|------|---------------|
| 1 | `backend/app/config.py` | All settings and validation |
| 2 | `backend/app/main.py` | FastAPI app + lifespan |
| 3 | `backend/app/db/models.py` | All ORM models |
| 4 | `backend/app/api/dependencies.py` | Auth injection |
| 5 | `backend/app/services/auth.py` | Registration, login, JWT |
| 6 | `backend/app/services/applications.py` | ApplicationTask state machine |
| 7 | `backend/app/services/job_sync.py` | Tencent sync orchestration |
| 8 | `backend/app/services/job_discovery/deepagents_runner.py` | Supervisor, Web Navigation Agent, and agent-callable wrappers |
| 9 | `backend/app/services/job_discovery/worker.py` | Worker polling loop |
| 10 | `backend/app/domain/job_review.py` | Reason code allowlists |
