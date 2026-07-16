Status: DONE

# Task 7 Report: Job review release gates

## Scope and recovered-work audit

The recovered worktree was audited before any edit or staging. It contained 22
modified files and two untracked files, with no staged content. Every path is
part of Task 7 or is required by a Task 7 controller resolution; no temporary
debugging, truncated implementation, or unrelated user change was found.

- Release behavior and contract fixes: `.env.example`, `README.md`,
  `backend/app/api/job_schemas.py`, `backend/app/api/routes/jobs.py`,
  `backend/app/repositories/jobs.py`, `docker-compose.yml`,
  `docs/platform-foundation-handover-summary.md`, and
  `docs/runbooks/platform-foundation.md`.
- Frontend release gate: `frontend/package.json`, `frontend/package-lock.json`,
  and the new `frontend/tsconfig.app.json`.
- Shared destructive-test protection: `tests/conftest.py`,
  `tests/integration/job_sync_gate_safety.py`, the new
  `tests/integration/test_mysql_gate_safety.py`, and the migrated MySQL users in
  `test_application_state_machine_mysql.py`, `test_mysql_migration.py`,
  `test_job_sync_mysql.py`, and `test_tencent_smartsheet_live.py`.
- Task 7 regression evidence: `tests/security/test_no_sensitive_logging.py`,
  `tests/contract/test_jobs_api.py`, `tests/unit/test_container_entrypoint.py`,
  `tests/unit/test_job_models.py`, `tests/unit/test_job_repository.py`, and
  `tests/unit/test_job_sync_service.py`.

The extra production changes close release-gate defects exposed by Task 7:

- a changed source candidate now increments `review_version` even at v0, making
  an already loaded administrator form stale;
- the existence of a `JobVerification`, rather than a resettable status/version
  pair, protects reviewed canonical fields from resync overwrite;
- the review path locks `job_postings` without a joined `job_sources` row lock,
  preserving a finite, exercised source-to-posting lock order;
- reject/expire require a nonblank stable reason and verify requires a null
  reason, so the route no longer synthesizes fallback reasons.

## Delivered release gates

- Security regression coverage deep-inspects formatted logs, `LogRecord` args
  and extras, and the HTTP response with independent business, source, raw
  record, credential, token, authorization, MCP trace, payload hash, external
  record id, and complete-upstream-response sentinels.
- Real MySQL coverage exercises `JobSyncService`, immutable raw history,
  reviewed canonical-field preservation, source-candidate/version updates,
  stale administrator rejection, finite lock waits, and two concurrent review
  commits producing exactly one state transition and one `JobVerification`.
- Migration coverage seeds representative 0003 rows, upgrades to 0004, checks
  backfill/index/content, writes reviewed state/history, downgrades to 0003, and
  verifies the documented loss boundary while retaining raw evidence.
- One shared fail-closed fixture requires exact
  `ALLOW_DESTRUCTIVE_MYSQL_TESTS=1`, a nonempty MySQL URL, and a database name
  ending in `_test` before destructive tests can connect.
- Operations documentation covers maintenance windows, backup, stopped writes,
  metadata-lock observation, migration loss, lifecycle/409 handling, manual
  channels, resync/version behavior, immutable review events, configurable host
  ports, and explicit Alembic revision verification.
- Compose declares revision `20260716_0004`; the frontend now has an explicit
  `vue-tsc` release gate.

## Evidence recovered from the interrupted worker

These results were reported in the handoff but their original command output is
not available in this recovered turn, so they are recorded as prior evidence,
not substituted for the fresh gates below:

- shared guard: 16 passed; safe default selection: 7 skipped;
- dedicated `career_assistant_test` MySQL selection: 9 passed;
- full Python suite with real MySQL connected: 661 passed, 4 skipped;
- Ruff, frontend 56 tests, `vue-tsc`, and Vite build passed;
- base Compose MySQL on 3307 and Redis on 6380 were healthy; host ports 9000 and
  9001 were occupied by another MinIO stack.

## Fresh verification after takeover

- `ruff check backend src tests scripts`: passed.
- Shared guard suite: 15 passed. This is the authoritative current collection
  count; the recovered 16-pass count was stale. The audited cases still cover
  opt-in-before-URL access, exact opt-in values, empty URLs, non-MySQL backends,
  unsafe database names, accepted `_test` URLs, and optimized Python.
- Focused unit/contract/security gate: 192 passed.
- Safe default destructive selection with both MySQL variables removed:
  2 passed, 7 skipped. Every skip named
  `ALLOW_DESTRUCTIVE_MYSQL_TESTS=1` exactly.
- The same default selection including the opt-in live Tencent test:
  2 passed, 8 skipped; the additional skip named
  `TEST_TENCENT_DOCS_TOKEN` exactly.
- Real MySQL gate, after constructing a credential-safe URL in process for only
  the isolated `career_assistant_test` schema on host port 3307 and setting the
  explicit destructive opt-in: 9 passed.
- Frontend: 5 files / 56 tests passed; `vue-tsc --noEmit -p
  tsconfig.app.json` passed; Vite production build passed with 21 modules
  transformed.
- No host `node.exe` process referenced this worktree after the frontend gates.

## Fresh Compose verification

User-scope secrets were loaded into only the verification process and were never
printed. Existing backend/frontend images were reused with `--no-build`. Host
ports were configured as MySQL 3307, Redis 6380, MinIO 19000, MinIO console
19001, Backend 18000, and Frontend 15173 to avoid the unrelated 9000/9001
collision.

- MySQL, Redis, MinIO, and Backend: healthy.
- Migration container: exited 0.
- Container Alembic current: `20260716_0004`.
- Backend `/api/health/live`: HTTP 200.
- Backend `/api/health/ready`: HTTP 200 with MySQL, Redis, and object store up.
- Frontend `/`: HTTP 200.

The reused frontend image predates this Task 7 gate-only package/config change,
but Task 7 changes no frontend runtime source; the fresh local Vite build and
served-image HTTP check cover the two relevant boundaries.

## Skips and concerns

- The seven destructive MySQL skips are intentional fail-closed behavior when
  the explicit opt-in is absent; the same tests passed against the isolated
  `_test` schema when enabled.
- The live Tencent read-only gate remains opt-in and was skipped because
  `TEST_TENCENT_DOCS_TOKEN` was not loaded for the default gate. No claim is made
  that a live upstream Tencent request was freshly exercised.
- The recovered 661/4 full-suite result is retained as prior evidence. Fresh
  takeover verification deliberately reran the Task 7 focused, safety, real
  MySQL, frontend, and runtime-stack gates requested for integration.
- No blocking concern remains.
