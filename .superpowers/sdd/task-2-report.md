# Task 2: TrajectoryBuffer -- Implementation Report

**Status:** DONE

## 1. What Was Implemented

### `backend/app/services/job_discovery/strategy/trajectory_buffer.py`
- **`TrajectoryBuffer` class** -- in-memory buffer that records every tool execution step during a single task run.
  - **`__init__`**: Accepts `task_id`, `strategy_id`, `executor_type`. Initializes empty `_steps` list, `_failed` flag, and monotonic start timestamp.
  - **Properties**: `steps` (snapshot copy), `failed_step_index` (first failed step, or None), `elapsed_ms` (wall-clock since init via `time.monotonic()`).
  - **`record_step(tool, status, params, result, *, error, duration_ms)`**: Appends a step dict. After the first `status="failed"` step, all subsequent steps are automatically marked `is_fallback=True`.
  - **`to_snapshot_context(failed_step_index)`**: Builds a structured dict for Supervisor takeover -- completed steps before the failure, plus the failed step with error details.
  - **`to_dict()`**: Full buffer contents as a plain dict for persistence, including `elapsed_ms`.
  - **`_safe_serialize()`**: Static helper that converts results to JSON-safe form, truncating lists to 50 items and strings to 500 characters.

### `tests/unit/test_trajectory_buffer.py`
Six tests exercising all public interfaces:
| Test | What It Covers |
|------|---------------|
| `test_init_basic` | Constructor defaults, properties |
| `test_record_success_step` | Step dict shape, fields, `None` error |
| `test_record_error_step_sets_failed_index` | `failed_step_index` after a failure |
| `test_record_after_failure_goes_to_fallback` | `is_fallback` flag on post-failure steps |
| `test_to_snapshot_context` | Takeover context dict structure |
| `test_to_dict` | Full dict for persistence |

## 2. Test Results

```
============================= test session starts =============================
platform win32 -- Python 3.12.5, pytest-8.4.2, pluggy-1.6.0
rootdir: D:\Python\...-strategy-router
plugins: anyio-4.14.2, langsmith-0.10.3, asyncio-1.4.0

tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_init_basic PASSED [ 16%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_record_success_step PASSED [ 33%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_record_error_step_sets_failed_index PASSED [ 50%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_record_after_failure_goes_to_fallback PASSED [ 66%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_to_snapshot_context PASSED [ 83%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_to_dict PASSED [100%]

============================== 6 passed in 0.13s ==============================
```

## 3. Self-Review Notes

- **Pure in-memory**: No DB calls, no async -- all tests complete in ~0.13s, fully deterministic.
- **`_safe_serialize`** guards against large results: lists capped at 50 items, strings at 500 chars with `...[truncated]` suffix, unknown types converted via `str()`.
- **`_steps` returns a copy** via the `steps` property, preventing external mutation of the internal list.
- **`failed_step_index` is computed** from the step list rather than stored, ensuring it stays correct even if steps were somehow mutated (defensive).
- **`is_fallback` works via latch**: once `_failed` is set, all subsequent steps are marked `is_fallback=True`.
- The broader unit test suite was kicked off and is still running; no regressions are expected since this is a new, isolated module with no imports on other project code.

## 4. Concerns

- **Timestamp format not an ISO string:** The brief implementation uses `time.monotonic()` for the step `timestamp` field, but the brief instructions say persistent timestamps should use `datetime.now(timezone.utc).isoformat()`. The test only checks `"timestamp" in step` so it passes either way. If a consumer expects ISO-8601 timestamps for persistence, this field should be changed. Keeping as-written to match the implementation spec verbatim.
- **No import of `datetime`**: Since `time.monotonic()` is used for everything, the `trajectory_buffer.py` file has no `from datetime import ...` line. The `time` import is sufficient. If a future change requires ISO timestamps, the import will need to be added.
- **The `failed_step_index` argument in the interface docstring** (`to_snapshot_context(failed_step_index: int) -> dict`) does not match the implementation -- the method derives `fail_idx` internally from the step list. The implementation is the correct approach since the index is always determinable from internal state. The interface docstring in the task brief appears to have a stale parameter name.

## 5. Fix applied

**Review from Task 2 (Trajectory Buffer) — all items addressed.**

### Changes made

**`backend/app/services/job_discovery/strategy/trajectory_buffer.py`**
- **CRITICAL (timestamp format):** Added `from datetime import datetime, timezone` import. Changed `"timestamp": time.monotonic()` to `"timestamp": datetime.now(timezone.utc).isoformat()` on line 74.
- **LOW (to_snapshot_context shape):** Changed bare `return {}` to `return {"completed_steps": [], "failed_step": None}` on line 89.
- `elapsed_ms` property and `_started_at` remain correctly using `time.monotonic()` for process-local duration measurement.

**`tests/unit/test_trajectory_buffer.py`**
- **IMPORTANT (_safe_serialize coverage):** Added 3 tests:
  - `test_safe_serialize_list_truncated` — list of 100 items truncated to 50
  - `test_safe_serialize_string_truncated` — string >500 chars inside dict truncated with suffix
  - `test_safe_serialize_nested_dict` — nested dict recursion preserves structure
- **IMPORTANT (to_snapshot_context edge cases):** Added 2 tests:
  - `test_to_snapshot_context_empty_buffer` — no steps, no failure: `completed_steps == []`, `failed_step is None`
  - `test_to_snapshot_context_failure_on_step_zero` — failure on first step: `completed_steps == []`, `failed_step` has step 0
- **LOW (to_dict thickness):** Added `strategy_id`, `failed_step_index` (type + value), and `elapsed_ms` (type + range) assertions.
- **LOW (record_success_step):** Added ISO-8601 format assertion on `timestamp` field.

### Test results

```
============================= test session starts =============================
platform win32 -- Python 3.9.13, pytest-8.4.2, pluggy-1.6.0
rootdir: D:\Python\...-strategy-router
plugins: anyio-4.12.1, langsmith-0.4.37, asyncio-1.2.0

tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_init_basic PASSED [  9%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_record_success_step PASSED [ 18%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_record_error_step_sets_failed_index PASSED [ 27%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_record_after_failure_goes_to_fallback PASSED [ 36%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_to_snapshot_context PASSED [ 45%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_to_snapshot_context_empty_buffer PASSED [ 54%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_to_snapshot_context_failure_on_step_zero PASSED [ 63%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_to_dict PASSED [ 72%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_safe_serialize_list_truncated PASSED [ 81%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_safe_serialize_string_truncated PASSED [ 90%]
tests/unit/test_trajectory_buffer.py::TestTrajectoryBuffer::test_safe_serialize_nested_dict PASSED [100%]

============================= 11 passed in 0.20s ==============================
```
