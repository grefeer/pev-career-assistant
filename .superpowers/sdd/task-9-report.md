# Task 9 Report: Health Readiness and Compose Development Stack

## Scope

- Added compatible `/api/health`, process-only `/api/health/live`, and dependency-aware `/api/health/ready` endpoints.
- Readiness independently checks MySQL `SELECT 1`, Redis `PING`, and S3 `head_bucket`; failures return only fixed `up`/`down` values and HTTP 503.
- FastAPI lifespan ensures the object bucket before serving and preserves ownership semantics for injected and application-owned resources.
- Added a six-service Compose stack (MySQL 8.4, Redis 8.0, pinned MinIO, migrate, backend, frontend) and three named volumes.
- Added a root-only MySQL initialization script that creates only `career_assistant_test` and no users or data.
- Updated the backend image to include Alembic migration files and added `.dockerignore` to exclude local environments, caches, secrets, and runtime state.

## TDD evidence

### RED

Command:

```text
.venv\Scripts\python.exe -m pytest tests\contract\test_health_api.py -v
```

Observed before implementation: `live` and `ready` both returned 404 (`2 failed`). After adding the lifespan ownership contract, the focused suite had `3 failed`, including the unsupported `blob_store` injection argument.

### GREEN

The focused health and checkpoint lifecycle suite passed `8/8`. The health tests prove:

- liveness never invokes failed external dependencies;
- readiness checks all three failed dependencies and returns the fixed 503 payload without exception text, hostnames, usernames, or credential terms;
- all healthy dependencies return `ready` and three `up` values;
- an injected blob store is ensured once before traffic and is neither replaced nor closed.

## Compose and dependency evidence

- `docker compose config --quiet`: exit 0.
- Service count: 6; named volume count: 3. Resolved configuration was not printed.
- Passwords were read from Windows User-scope `DB_PASSWORD` and `REDIS_PASSWORD`, copied only to Process scope, never printed, and never written to a host file or persistent volume. Redis writes its password only to the container's non-volume `/tmp/redis.conf` with mode 0600.
- URL-encoded password variants and temporary application/encryption secrets were derived only in process.
- Host port overrides used for verification: MySQL `3307` because a non-Docker listener occupied `3306`; Redis `6380` because the user's `redis-custom` remained on `6379`.
- MySQL 8.4, passworded Redis 8.0, and the pinned MinIO release all reached `healthy`. The existing `redis-custom` container remained running and unchanged.
- Host Alembic migration against the Compose MySQL main database exited 0.
- A host backend using the three Compose dependencies completed lifespan startup and returned `ready` with MySQL, Redis, and object store all `up`.

## Integration and regression evidence

- Full real-dependency integration suite: `6/6 passed` using root plus `DB_PASSWORD`, passworded Redis 8 on logical DB 0, and MinIO.
- An initial DB 15 integration run passed `5/6`; Redis Search correctly rejected index creation outside DB 0. The complete suite was rerun on DB 0 because LangGraph Redis checkpoint indexes require it.
- Full repository suite: `266 passed, 4 skipped` (the four external integration tests skip when their environment variables are intentionally absent from the general regression run).
- Ruff over `backend` and `tests`: passed.

## Open concern

Docker Desktop's local build session stalled without output or image creation for the worktree context, a `.dockerignore`-reduced context, legacy builder, and a minimal `%TEMP%` context. Docker daemon/buildx queries remained healthy and local dependency images ran normally. No shared build cache was cleared and Docker Desktop was not restarted. Therefore the backend/frontend image build, Compose `migrate` container exit, and six-container full-stack state could not be verified on this host; dependency containers were retained for the next gate.

## Review fixes

### Container credential boundary

- Added a Linux container entrypoint that accepts only raw `DB_PASSWORD` and `REDIS_PASSWORD`, percent-encodes them in memory, sets service URLs without printing them, and preserves the Compose command via `execvp`.
- Missing credentials return one fixed redacted error. Unit RED was the missing entrypoint module; GREEN was `4/4`, later `5/5` with the frontend and shared-image assertions.
- Removed mandatory `DB_PASSWORD_URLENCODED`, `REDIS_PASSWORD_URLENCODED`, `DATABASE_URL`, and `REDIS_URL` Compose inputs. A programmatic resolved-config test proves Redis's real password is absent from `Config.Cmd` and that backend/migrate share one image.
- Redis now writes an owner-only mode-0600 temporary config from `$$REDIS_PASSWORD` to the container-only, non-volume `/tmp` directory at runtime and re-enters the official image entrypoint so Redis 8 JSON/Search modules remain loaded. The recreated container was healthy and authenticated JSON/Search probes passed.

### Lifespan failure cleanup and readiness timeout

- RED reproduced an `ensure_bucket()` startup failure leaving the owned Redis client unclosed.
- `AsyncExitStack` now registers Redis and S3 cleanup immediately after creation, covering create, ensure, serve, and shutdown phases. GREEN proves owned S3, Redis, and checkpointer cleanup plus state removal, while pre-injected resources remain open and present.
- Added a validated `readiness_timeout_seconds` setting (default 2, range 1–30). Redis uses connect/socket timeouts and S3 uses connect/read timeouts with at most two attempts. A lifespan-owned MySQL readiness engine alone uses PyMySQL connect/read/write timeouts; the business `SessionLocal` engine does not inherit probe deadlines. SQLite receives no MySQL connect arguments. The readiness engine and factory are created once per lifespan, disposed on startup failure/shutdown, and never replace pre-injected factories.
- Added public `S3BlobStore.check_bucket()`; readiness no longer accesses private client/bucket fields.

### Reproducible frontend and final full stack

- Frontend Dockerfile now copies `package*.json` and runs `npm ci`. The original lockfile produced `Invalid Version` under Node 24; regenerating it from `package.json` with the same Node 24 image fixed the reproducible install.
- An isolated Node 24 container passed `npm ci` and the Vite production build. A subsequent frontend image build passed.
- Backend image completed in 5 seconds. The initial combined build failed only at the old frontend lockfile; build history identified the exact `npm ci` error without another speculative build loop.
- Compose `migrate` and `backend` now explicitly share the backend image, fixing the missing implicit migrate image found during the first full-stack start.
- Final six-service state: MySQL, Redis, MinIO, and backend healthy; frontend running and HTTP 200; migrate exited 0. `/api/health/ready` returned all three dependencies `up`.
- Final real-dependency integration suite passed `6/6`; full repository suite passed `283` with `4` expected external-environment skips; Ruff passed.

The original build concern above is resolved by the regenerated frontend lockfile and successful full-stack verification.

### Follow-up review: credential controls and engine isolation

- RED proved that both the backend entrypoint and Redis runtime helper accepted CR/LF in credentials. GREEN rejects CR/LF (the backend rejects all ASCII control characters) with fixed redacted errors that never echo values.
- The executable Redis helper tests pass credentials containing single quotes, double quotes, and backslashes, authenticate successfully, and verify `/tmp/redis.conf` mode 0600. Separate CR/LF tests exit 78 with only the fixed error.
- RED also proved the business MySQL engine inherited the 2-second probe timeout. GREEN separates the lifespan-owned readiness engine from the global business engine and verifies disposal during `ensure_bucket()` failure.

## 6. Fix applied

### Finding 1: Removed dead recursion_limit

Removed the unused `recursion_limit = 30 if snapshot_context is not None else 50` line. The variable was set in `build_discovery_supervisor_agent()` but never passed to `create_deep_agent`. The function now omits it entirely.

### Finding 2: _load_prompt raises for missing required files

Added `required: bool = True` parameter to `_load_prompt()`. When `required=True` and the file is missing, `FileNotFoundError` is raised. When `required=False`, returns empty string (previous default behavior).

Updated `build_supervisor_prompt()` calls:
- `_load_prompt("supervisor_base", required=True)` — base is required
- `_load_prompt("supervisor_clean_start", required=False)` — mode templates optional
- `_load_prompt("supervisor_snapshot_fallback", required=False)` — mode templates optional

Also added:
- `build_supervisor_prompt()` function that assembles the supervisor prompt from template files, accepting optional `snapshot_context` for breakpoint takeover
- Three prompt template files: `prompts/supervisor_base.txt`, `prompts/supervisor_clean_start.txt`, `prompts/supervisor_snapshot_fallback.txt`
- `_format_snapshot_steps()` and `_summarize_params()` helper functions

**Test results after fix:**
```
============================= test session starts =============================
platform win32 -- Python 3.12.5, pytest-8.4.2, pluggy-1.6.0
cachedir: .pytest_cache
rootdir: D:\Python\langgraph-multi-agent-career-assistant-main
configfile: pyproject.toml
plugins: anyio-4.14.2, langsmith-0.10.3, asyncio-1.4.0
asyncio: mode=Mode.STRICT
collecting ... collected 63 items / 62 deselected / 1 selected

tests/integration/test_job_discovery_deepagents.py::TestSupervisorSystemPrompt::test_prompt_contains_key_elements PASSED [100%]

====================== 1 passed, 62 deselected in 5.85s =======================
```
