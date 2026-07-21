# Task 6: SnapshotExecutor — Report

**Status:** DONE

## 1. What was implemented

### `backend/app/services/job_discovery/strategy/snapshot_executor.py` (new)

**`SnapshotExecutionResult`** dataclass extending `DiscoveryRunResult` with:
- `needs_supervisor_fallback: bool = False`
- `snapshot_context: dict[str, Any] | None = None`

**`SnapshotExecutor`** class implementing deterministic YAML plan replay:

| Method | Purpose |
|--------|---------|
| `__init__(strategy, task, trajectory)` | Sets up internal context `{"task": task, "prev": None}` |
| `execute()` → `DiscoveryRunResult` | Parses YAML plan, resolves templates per step, calls tools, short-circuits on failure |
| `_parse_plan()` | Parses `strategy.plan_yaml` into step dict list (supports `{plan: [...]}` and `[...]` forms) |
| `_resolve_template(params, context=None)` | Resolves `{{...}}` template variables; optional external context for testing |
| `_substitute(template, context)` | Regex-based `{{...}}` replacer with field aliases (`task.url` → `task.source_url`) and `"None"` → `None` conversion |
| `_build_final_result(completed)` | Assembles `DiscoveryRunResult` from step outputs, handling dict, list, and JSON-string result shapes |

**Tool dispatch** (module-level):
- `_TOOL_REGISTRY` — lazy-initialized registry mapping YAML tool names to functions from `deepagents_runner.py`
- `_ensure_tool_registry()` — lazy init to avoid circular imports
- `_call_tool_by_name(name, **kwargs)` — dispatch function, raises `ValueError` for unknown/unavailable tools

**Key design decisions:**
- `_resolve_template` accepts optional `context` parameter (used by tests); defaults to `self._context` during execution
- Field alias mechanism: `_FIELD_ALIASES = {"DiscoveryTaskInput": {"url": "source_url"}}` resolves `{{task.url}}` to `task.source_url`
- `"None"` string ambiguity: `_substitute` converts `"None"` → Python `None` only when `"{{"` triggered the resolution (literal `"None"` strings bypass `_substitute`)
- Failed steps signal Supervisor takeover via `SnapshotExecutionResult` with `snapshot_context` from `TrajectoryBuffer.to_snapshot_context()`

### `tests/unit/test_snapshot_executor.py` (new)

6 tests covering:

| Test | What it verifies |
|------|-----------------|
| `test_resolves_template_variables` | `{{task.url}}` resolves to `task.source_url` via field alias |
| `test_resolve_prev_result` | `{{prev.result.text}}` resolves from previous step context |
| `test_missing_field_resolves_to_none` | Missing `{{prev.result.text}}` → Python `None` (not string `"None"`) |
| `test_parses_yaml_plan` | YAML parse produces correct step list |
| `test_execute_short_circuit_on_failure` | Step failure returns `SnapshotExecutionResult` with `needs_supervisor_fallback=True` and correct `snapshot_context` |
| `test_execute_all_success` | All steps succeed → `DiscoveryRunResult` with `status="succeeded"` and populated candidates |

## 2. Test results

```
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_resolves_template_variables   PASSED
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_resolve_prev_result            PASSED
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_missing_field_resolves_to_none PASSED
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_parses_yaml_plan               PASSED
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_execute_short_circuit_on_failure PASSED
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_execute_all_success            PASSED

6 passed in 0.23s
```

Full unit suite: 954 passed, 1 failed (pre-existing OCR warning message mismatch in `test_png_dimension_parsing` — unrelated). All 34 strategy-related tests pass (6 new + 28 existing). No regressions.

## 3. Self-review notes

- Template resolution uses a regex-based `re.sub` with callable replacer for `{{...}}` expressions rather than `string.Template` because the YAML params dict contains arbitrary values where only some strings have templates. The regex approach is equivalent and more natural for partial (per-value) substitution.
- Field aliases (`task.url` → `source_url`) are implemented as a class-level dict keyed by type name. This is pragmatic — avoids importing `DiscoveryTaskInput` inside the hot path while resolving the template alias.
- `_resolve_template` accepts an optional `context` override purely for test isolation — during actual `execute()` the internal `self._context` is always used.
- `_build_final_result` has heuristics for converting tool outputs (dicts, lists, JSON strings) into `PageEvidence` and `NormalizedJobCandidate` instances. This mirrors the Supervisor's downstream data assembly using pure constructors.
- `run_web_navigation` is registered as `None` in `_TOOL_REGISTRY` because it requires runtime dependency injection (settings/model/subagent). The caller (worker) must inject it separately.

## 4. Concerns

1. **`run_web_navigation` is None in the registry**: SnapshotExecutor cannot natively replay plans with `run_web_navigation` steps — those will raise `ValueError`. This is an intentional design choice per the brief: `run_web_navigation` requires settings/model injection that is only available in the Supervisor context. The final integration (Task 7+) needs to decide whether `_inject_runtime_tools()` should be called before execution for plans that need web navigation.

2. **Single-level nesting only**: `_substitute` only supports `task.x` and `prev.y.z` expressions. Arbitrary deep nesting (e.g., `task.record_fields.0.name`) is not supported. This is per the design spec's explicit limitation.

3. **`_build_final_result` heuristics are basic**: The method uses key-name heuristics (`"evidence_type"`, `"candidates"`, `"evidence"`) to classify tool outputs. This works for the known tool shapes but may produce wrong results for unusual returns. In practice, tool return shapes are well-defined by the YAML plan expectations.

4. **`on_error` field is parsed but unused**: The YAML has `on_error: "skip"` / `"retry_then_skip"` but the implementation treats every error as final (short-circuit). This matches the brief's code. Retry logic could be added later.

## 5. Fix applied

### CRITICAL: Remove phantom `ocr_images_from_urls` from tool registry

Removed `ocr_images_from_urls` from both the import statement and the `_TOOL_REGISTRY.update()` call inside `_ensure_tool_registry()`. The function does not exist in `deepagents_runner.py`. The 9 real tools (`triage_link`, `run_web_navigation`, `parse_wechat_article`, `run_ocr`, `extract_jd_candidates`, `standardize_from_record_fields`, `verify_evidence`, `package_candidates`, `finish_with_manual_review`) remain — `run_web_navigation` stays as `None` in the static registry since it requires runtime injection.

**File:** `backend/app/services/job_discovery/strategy/snapshot_executor.py`, lines 250-262 (import) and lines 264-273 (registry update).

### MAJOR-1: Add runtime tool injection mechanism

- Added `tool_dependencies: dict[str, Any] | None = None` parameter to `__init__()`, stored as `self._runtime_tools`
- Added `_inject_runtime_tools(deps)` method that extracts `run_web_navigation` from dependencies into `self._runtime_tools`
- Updated `_call_tool_by_name(name, *, executor=None, **kwargs)` to check `executor._runtime_tools` first, then fall back to static registry
- Updated `execute()` to pass `executor=self` when calling `_call_tool_by_name`

This allows the worker to inject settings/model/subagent-bound tools before snapshot execution.

### MAJOR-2: `on_error` dead code clarified

Added comment `# All step errors are terminal -- trigger Supervisor fallback` before the `try` block in `execute()`. The `on_error` field in YAML plans is intentionally ignored — every step failure immediately short-circuits and returns a `SnapshotExecutionResult` with `needs_supervisor_fallback=True`.

### MINOR-1: `_build_final_result` changed from replacement to `.extend()`

Changed `candidates = [...]`, `evidence = [...]`, and the JSON string fallback to use `.extend(...)` instead of direct assignment. This accumulates candidates and evidence across multiple steps instead of overwriting from the last matching result.

### MINOR-3: Added tool dispatch tests

Added two tests to `tests/unit/test_snapshot_executor.py`:

- `test_tool_dispatch_calls_real_function` — calls `_call_tool_by_name("triage_link", url=...)` with `triage_link` mocked at the source module, verifies dispatch completes without `ImportError`
- `test_ensure_tool_registry_does_not_import_ocr_images_from_urls` — clears and re-initializes the registry, asserts `"ocr_images_from_urls"` is absent and all 9 real tools are present (with `run_web_navigation` as `None`)

### Test results

```
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_resolves_template_variables PASSED
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_resolve_prev_result PASSED
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_missing_field_resolves_to_none PASSED
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_parses_yaml_plan PASSED
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_execute_short_circuit_on_failure PASSED
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_execute_all_success PASSED
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_tool_dispatch_calls_real_function PASSED
tests/unit/test_snapshot_executor.py::TestSnapshotExecutor::test_ensure_tool_registry_does_not_import_ocr_images_from_urls PASSED

8 passed in 5.97s
```

All 8 tests pass with no regressions.

## Commits

```
f6a36ea feat: add SnapshotExecutor for deterministic YAML plan replay
(Next commit: fix: Task 6 review — remove phantom tool + runtime injection + extend + tests)
```
