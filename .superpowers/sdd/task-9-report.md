# Task 9 Report: Supervisor Prompt File Loading + Snapshot Context + Alibaba Shortcut Removal

**Status:** DONE

## 1. Changes to `deepagents_runner.py`

### 1a. Added Prompt Loading Functions (after `_build_job_discovery_llm`)

- **`_load_prompt(name)`** — reads a template file from `prompts/` directory by name (without `.txt` extension). Returns empty string for missing files during migration.
- **`build_supervisor_prompt(snapshot_context=None)`** — assembles the Supervisor system prompt:
  - Always loads `supervisor_base.txt` as the base layer.
  - When `snapshot_context is None`: appends `supervisor_clean_start.txt` (fresh-start execution mode).
  - When `snapshot_context` is provided: formats `supervisor_snapshot_fallback.txt` template with snapshot context variables (source, strategy_id, completed_steps, failed_step tool/params/error).
  - Returns joined parts as a single prompt string.
- **`_format_snapshot_steps(completed_steps)`** — formats completed snapshot steps as human-readable numbered list.
- **`_summarize_params(params)`** — creates short parameter summaries for display.

### 1b. Removed Hardcoded `_SUPERVISOR_SYSTEM_PROMPT`

Replaced the inline string constant with a backward-compatible alias:
```
_SUPERVISOR_SYSTEM_PROMPT = build_supervisor_prompt()
```
All existing imports of `_SUPERVISOR_SYSTEM_PROMPT` continue to work.

### 1c. Modified `build_discovery_supervisor_agent()`

- Extended signature: `snapshot_context: dict | None = None` parameter added.
- Uses `build_supervisor_prompt(snapshot_context)` instead of `_SUPERVISOR_SYSTEM_PROMPT`.
- Sets `recursion_limit` based on mode: 30 for snapshot fallback, 50 for clean start.

### 1d. Removed Hardcoded Alibaba SPA Shortcut

- **`extract_rendered_job_evidence()`**: Replaced the Alibaba-specific `capture_response` (which only captured `campus-talent.alibaba.com/position/search` responses) with a generic JSON response capture that inspects `content-type` header for `json` or `javascript`. Uses `_generic_position_evidence_from_payload` instead of the removed Alibaba parser.
- **`_alibaba_position_evidence_from_search_payload()`**: Removed entirely. The `AlibabaSPAAdapter` now uses `_generic_position_evidence_from_payload` directly.
- **`_fetch_alibaba_search_api()`** and **`_generic_position_evidence_from_payload()`**: Preserved — used by `AlibabaSPAAdapter`.

## 2. Changes to `alibaba_spa.py`

- Updated imports: removed `_alibaba_position_evidence_from_search_payload`.
- `execute()` now calls `_generic_position_evidence_from_payload` directly instead of trying the Alibaba-specific parser first.
- Updated docstring to reflect new import.

## 3. Changes to Integration Tests

- **`test_job_discovery_deepagents.py`**:
  - Replaced `_alibaba_position_evidence_from_search_payload` import with `_generic_position_evidence_from_payload` and `build_supervisor_prompt`.
  - `TestSupervisorSystemPrompt`: Updated assertions to match new prompt template content. Added `test_snapshot_context_prompt` for snapshot context validation. Added `test_backward_compatible_alias`.
  - `TestRenderedJobEvidence`: Changed to test `_generic_position_evidence_from_payload` with a generic payload structure.

## 4. Test Results

```
Full unit suite (excluding executor tests with missing keyring dep):
  960 passed, 1 failed (pre-existing async: test_evidence_matching_agents)

Job discovery unit + integration tools:
  133 passed, 8 failed (all 8 pre-existing async, missing pytest-asyncio)
```

All non-async tests pass. The 8 async failures are pre-existing: `pytest-asyncio` is not installed in the virtual environment.

## 5. Files Modified

| File | Change |
|------|--------|
| `backend/app/services/job_discovery/deepagents_runner.py` | Prompt loading functions, removed `_SUPERVISOR_SYSTEM_PROMPT` string, removed `_alibaba_position_evidence_from_search_payload`, updated `extract_rendered_job_evidence`, updated `build_discovery_supervisor_agent` |
| `backend/app/services/job_discovery/adapters/alibaba_spa.py` | Updated to use `_generic_position_evidence_from_payload` instead of `_alibaba_position_evidence_from_search_payload` |
| `tests/integration/test_job_discovery_deepagents.py` | Updated imports, `TestSupervisorSystemPrompt`, `TestRenderedJobEvidence` |
