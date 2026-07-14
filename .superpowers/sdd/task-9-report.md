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
- Passwords were read from Windows User-scope `DB_PASSWORD` and `REDIS_PASSWORD`, copied only to Process scope, and never printed or written.
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
