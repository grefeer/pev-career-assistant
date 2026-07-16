# Final Contract Fixes Report

Status: DONE

## Scope

Closed only the two final-review contract gaps: decision reason-code enforcement and
release-runbook portability/gate completeness.

## Decision contract

- Added domain-owned reject reason codes:
  `invalid_source`, `wrong_company`, `insufficient_job_details`, and
  `unsafe_or_invalid_apply_channel`.
- Added domain-owned expire reason codes:
  `closed_on_official_site`, `deadline_passed`, and
  `application_channel_unavailable`.
- `JobReviewService.reject` and `JobReviewService.expire` now raise
  `IncompleteJobError("reason_code")` for unknown or cross-decision codes.
- `JobDecisionRequest` applies the same decision-specific allowlists, preserving
  `reason_code=null` for verify and returning HTTP 422 before route execution for
  invalid reject/expire payloads.
- The frontend decision payload is a discriminated union with typed reject and expire
  reason codes. The review UI reason refs use those types and retain the existing
  stable-code select controls.
- Existing `closed` test fixtures were replaced with
  `closed_on_official_site`.

## TDD evidence

- RED service/schema run: 7 expected failures because unknown and cross-decision
  reason codes did not raise validation errors.
- RED API run: 3 expected failures because invalid payloads returned 200/409 instead
  of 422.
- GREEN focused reason-contract runs: 15 passed (unit) and 8 passed (API).

## Release runbook

- Integration URLs now use `MYSQL_HOST_PORT`, `REDIS_HOST_PORT`, and
  `MINIO_HOST_PORT`, with Compose defaults `3306`, `6379`, and `9000`.
- Commands resolve the repository-root `.venv` Python executable to an absolute path,
  set the repository root explicitly, and invoke frontend scripts through `npm.cmd`.
- The complete release gate now includes frontend test, typecheck, and build.

## Verification

- Focused backend: `127 passed`.
- Ruff: `All checks passed!`.
- Frontend tests: `5 files passed`, `56 tests passed`.
- Frontend typecheck: exit 0.
- Frontend production build: exit 0.
- Full Python suite: `664 passed, 11 skipped`.
- `git diff --check`: clean.
