# Task 1 implementation report — result contract and status semantics

## Changed files

- `backend/app/services/job_discovery/result_contract.py` (new): shared agent-result parsing, parse diagnostics, and result-status invariants.
- `backend/app/services/job_discovery/worker.py`: compatibility parser wrapper, parse-failure trajectory metadata, and invariant enforcement before persistence.
- `tests/unit/test_job_discovery_result_contract.py` (new): reverse message scan, text content blocks, and zero-candidate status-invariant coverage.
- `tests/unit/test_job_discovery_worker.py`: verifies an empty `partial_success` result persists as `needs_manual_review` with `parse_failed`.

## TDD evidence

### Red: shared parser contract

Command (the requested worktree-local `.venv` was absent, so the existing checkout's shared interpreter was used by absolute path):

```powershell
D:\Python\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_result_contract.py -q
```

Observed output: collection failed with `ModuleNotFoundError: No module named 'backend.app.services.job_discovery.result_contract'` (expected before creating the module).

### Green: shared parser contract

```powershell
D:\Python\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_result_contract.py -q
```

Observed output: `4 passed in 0.31s`.

### Red: worker status persistence

```powershell
D:\Python\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_worker.py -q
```

Observed output: `1 failed, 17 passed`; the task persisted `partial_success` instead of the required `needs_manual_review`.

### Green: worker wiring and regression

```powershell
D:\Python\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_result_contract.py tests/unit/test_job_discovery_worker.py -q
```

Observed output: `22 passed in 8.03s`.

## Verification

```powershell
git diff --check
D:\Python\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe -m ruff check backend/app/services/job_discovery/result_contract.py backend/app/services/job_discovery/worker.py tests/unit/test_job_discovery_result_contract.py tests/unit/test_job_discovery_worker.py
```

Observed output: `All checks passed!` (including clean diff whitespace and Ruff).

## Self-review

- Parsing accepts `structured_response`, direct result dictionaries, Pydantic `model_dump()` values, raw/fenced JSON, and text content blocks.
- Message candidates are processed newest-first, ensuring a trailing tool result cannot hide the prior structured AI response.
- Parse failures expose only message types and count to the trajectory; message bodies are neither logged nor retained in the diagnostic metadata.
- Empty successful results become `failed + parse_failed`; empty partial results become `needs_manual_review + parse_failed` before any persistence decision.
- No Path A/B or Strategy Router logic was changed.

## Concerns

- The isolated worktree has no `.venv`, so test commands used the original checkout's existing virtual environment via absolute path. Source modifications and test collection remained in the isolated worktree.
- `.superpowers/sdd/progress.md` was already modified and is intentionally not staged or committed by this task.

---

## Re-review fix: tool-only recovery status semantics

### Scope

- Preserved tool-recovered candidates and evidence when no authoritative final
  result is available.
- Changed candidate-bearing recovery from `succeeded` to
  `partial_success + parse_failed`, because completion coverage is unknown.
- Kept evidence-only recovery at `needs_manual_review + parse_failed`.
- Did not modify PATH A, PATH B, the Strategy Router, SQL, or log content.

### Red

After adding the candidate-only and incomplete-coverage regressions:

```powershell
D:\Python\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_result_contract.py -q
```

Observed output: `2 failed, 5 passed in 0.96s`. Both new recovery tests
received `succeeded`, demonstrating the unsafe pre-fix behavior.

### Green

```powershell
D:\Python\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_result_contract.py tests/unit/test_job_discovery_worker.py -q
```

Observed output: `25 passed in 10.24s`.

### Verification

```powershell
D:\Python\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe -m ruff check backend/app/services/job_discovery/result_contract.py tests/unit/test_job_discovery_result_contract.py
git diff --check
```

Observed output: `All checks passed!`; `git diff --check` exited 0.

### Concerns

- The isolated worktree has no `.venv`, so checks used the original checkout's
  existing interpreter by absolute path while collecting source and tests from
  this isolated worktree.
- `.superpowers/sdd/progress.md` remained pre-existing, unrelated work and is
  not staged.
