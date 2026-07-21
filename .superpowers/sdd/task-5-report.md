# Task 5 Report: DomainAdapter Base + AlibabaSPAAdapter

**Status: DONE**

---

## 1. What Was Implemented

### New Files

- **`backend/app/services/job_discovery/adapters/__init__.py`** -- Package init that exports `DomainAdapter` and `AlibabaSPAAdapter`.
- **`backend/app/services/job_discovery/adapters/base.py`** -- `DomainAdapter` abstract base class defining the contract for all domain-specific fast-path adapters:
  - `url_pattern: str` class attribute for URL matching
  - `execute(task, strategy, trajectory) -> DiscoveryRunResult` (abstract)
  - `validate(url) -> bool` (abstract)
- **`backend/app/services/job_discovery/adapters/alibaba_spa.py`** -- `AlibabaSPAAdapter` concrete adapter that:
  - Calls the Alibaba campus `/position/search` JSON API directly (bypassing browser/LLM)
  - Uses `_alibaba_position_evidence_from_search_payload` for structured extraction
  - Falls back to `_generic_position_evidence_from_payload` for other payload shapes
  - Runs JD extraction, evidence verification, and candidate packaging deterministically
  - Records all steps via the `TrajectoryBuffer`
  - Validates URLs using `fnmatch` with scheme-stripping (consistent with `StrategyRouter`)
- **`tests/unit/test_domain_adapter.py`** -- 7 tests covering:
  - ABC instantiation prevention
  - Concrete subclass execution
  - Alibaba pattern matching and validation
  - API failure handling
  - Full success path with mocked dependencies

### Modified Files

- **`backend/app/services/job_discovery/schemas.py`** -- Added `StrategyRecord` dataclass with `from_orm()` factory method for decoupling from the ORM layer (imported `Any` for the type hint).
- **`backend/app/services/job_discovery/deepagents_runner.py`** -- Added two helper functions:
  - `_fetch_alibaba_search_api(url)` -- Direct HTTP call to Alibaba's `/position/search` endpoint, extracting batchId and campusShareCode from the source SPA URL.
  - `_generic_position_evidence_from_payload(payload, source_url)` -- Recursive job-object finder for arbitrary JSON payloads (ported from main repo).

### Design Notes

- The `validate()` method in `AlibabaSPAAdapter` tries matching both the full URL and the scheme-stripped netloc+path, consistent with `StrategyRouter._pattern_matches` behaviour.
- The adapter imports helper functions from `deepagents_runner.py` temporarily. A follow-up should extract these to a shared utility module.
- `StrategyRecord` uses `str` for `id` to accommodate both string UUIDs and integer PKs from different DB backends.

---

## 2. Test Results

```
tests/unit/test_domain_adapter.py::TestDomainAdapterBase::test_cannot_instantiate_abc PASSED
tests/unit/test_domain_adapter.py::TestDomainAdapterBase::test_concrete_subclass_works PASSED
tests/unit/test_domain_adapter.py::TestAlibabaSPAAdapter::test_url_pattern PASSED
tests/unit/test_domain_adapter.py::TestAlibabaSPAAdapter::test_validate_valid_url PASSED
tests/unit/test_domain_adapter.py::TestAlibabaSPAAdapter::test_validate_invalid_url PASSED
tests/unit/test_domain_adapter.py::TestAlibabaSPAAdapter::test_execute_api_failure_returns_failed PASSED
tests/unit/test_domain_adapter.py::TestAlibabaSPAAdapter::test_execute_success_path PASSED
```

**7/7 passed.**

Existing strategy/adapter/trajectory tests: **63/63 passed** (test_strategy_models, test_strategy_router, test_strategy_store, test_trajectory_buffer, test_error_classifier).
Existing job discovery tools tests: **76/76 passed.**

---

## 3. Self-Review Notes

- The `_fetch_alibaba_search_api` function makes a best-effort direct HTTP call to the Alibaba `/position/search` API. If the API endpoint changes or requires additional headers/cookies, the adapter will fail gracefully and mark the task as `failed` (caught by the try/except in `execute()`).
- The `_generic_position_evidence_from_payload` function copies logic from the main repo's `deepagents_runner.py` into the worktree; this function already exists in the main repo but was missing from the worktree.
- The adapter's dependencies (`verify_evidence`, `package_candidates`, etc.) are imported lazily inside `execute()` to avoid circular import issues and keep the adapter module lightweight at import time.
- The task brief had a typo in the Step 5 code: `_alibaba_position_evidence_from_search_payload(search_data)` was missing the required `source_url` argument. This was fixed during implementation.

---

## 4. Concerns

1. **`_fetch_alibaba_search_api` depends on undocumented API endpoint.** The Alibaba campus SPA's `/position/search` endpoint may change without notice, or may require request signatures that the current simple HTTP GET cannot provide. The adapter will fail gracefully in this case, falling through to `failed` status, and the task will be picked up by the Supervisor fallback path (when integrated).

2. **No integration test for real Alibaba API.** The success path test uses mocked dependencies. A live integration test would require an actual Alibaba campus URL and network access, which is out of scope for unit tests.

3. **Two strategies for matching URL patterns.** The `StrategyRouter` normalizes URLs (strips query/fragment, forces https) and then matches. The `AlibabaSPAAdapter.validate()` strips the scheme only. For consistency, both use `fnmatch`, but the normalization differs slightly. This is acceptable because `validate()` is a lightweight pre-check, while `StrategyRouter.match()` is the authoritative matching pipeline.

4. **`_generic_position_evidence_from_payload` may produce false positives.** Its recursive walk looks for dicts with 2+ job-indicator keys. Non-job payloads (e.g., user profile data, pagination metadata) could trigger it. This is mitigated by the subsequent evidence verification step.
