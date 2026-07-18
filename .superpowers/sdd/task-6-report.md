# Task 6: Phase 6 — Worker — Report

## Status

Complete.

## Files Created

| File | Lines | Purpose |
|------|-------|---------|
| `backend/app/services/job_discovery/worker.py` | 283 | `JobDiscoveryWorker` class with `run_once()` / `run_loop()` |
| `scripts/run_job_discovery_worker.py` | 42 | CLI entry point supporting `--once` flag |
| `tests/unit/test_job_discovery_worker.py` | 455 | 15 unit tests across 4 scenarios |

## Commit

`d9651b6fe0aaca3832de0f4c36e24d625993a29e` — `feat: add job discovery worker with polling loop and tests`

## Implementation Summary

### worker.py

- **`JobDiscoveryWorker(db_factory, settings)`** — polls `job_discovery_tasks` queue, claims tasks via `claim_next_task`, processes them with the Discovery Supervisor Agent, persists results.
- **`run_once()`** — Opens a DB session, claims a task, builds `DiscoveryTaskInput` from the task + raw record fields, invokes the agent synchronously (`agent.invoke(...)`), parses the structured output (supports `structured_response` key, raw dict, or last-message JSON), persists evidence/candidates, and marks the task as succeeded/partial_success/needs_manual_review/failed. Returns 1 if processed, 0 if no tasks. Exceptions are caught, task marked as failed, returns 0.
- **`run_loop(poll_interval=10.0)`** — Infinite loop calling `run_once()`, sleeping when queue is empty. Catches `KeyboardInterrupt`.
- **Helper functions** — `_build_worker_id()` (hostname + PID), `_parse_agent_result()` (3-strategy result parsing), `_persist_evidence()`, `_persist_candidates()` (handle both `PageEvidence`/`NormalizedJobCandidate` dataclass instances and plain dicts from structured output).

### Entry Point

`scripts/run_job_discovery_worker.py` — Imports project root, checks `job_discovery_enabled` setting, supports `--once` flag for single-run mode.

### Test Coverage (15 tests)

| Test Class | Tests | Coverage |
|---|---|---|
| `TestWorkerId` | 1 | Worker ID format |
| `TestParseAgentResult` | 5 | structured_response, direct dict, last message JSON, unparseable fallback, empty messages |
| `TestSuccessfulTask` | 2 | Full succeeded flow (evidence + candidates persisted), partial_success |
| `TestManualReviewTask` | 2 | `needs_manual_review` with block_reason, unknown block_reason fallback |
| `TestCrashRecovery` | 2 | Agent exception -> `mark_task_failed`, empty queue returns 0 |
| `TestRunLoop` | 3 | Loop iteration, sleep on empty, KeyboardInterrupt handling |

## Concerns

1. **Entry-point dependency**: `scripts/run_job_discovery_worker.py` imports from `backend.*` which requires the full project environment. This is consistent with other scripts in the project.
2. **No integration test**: The worker is tested with mocked agent. An integration test with a real (or near-real) agent would increase confidence, but is not in the current scope.
3. **agent.invoke() vs ainvoke()**: The worker uses synchronous `agent.invoke()` rather than `asyncio.run(agent.ainvoke(...))`. This is consistent with the synchronous `run_once()` signature in the spec. If async support is needed later, `run_once` can be made async and `asyncio.run()` or `asyncio.create_task()` used in the loop.

## Test Results

```
193 passed (all job_discovery + config + mapper tests)
```

All existing tests continue to pass. No regressions.
