Status: DONE

# Task 6 Report: Administrator job review workspace

## Commit

- `feat: add administrator job review workspace`
- Scope is frontend-only: administrator job DTO/API clients, review component,
  role-aware App navigation, and their Vitest coverage. No backend files changed.

## Delivered

- Added administrator review-queue and verified-lifecycle clients with explicit
  bounded pagination, non-terminal status filtering, encoded job ids, completion
  PATCH, and decision POST payloads carrying the authoritative `review_version`.
- Added a responsive, component-scoped administrator review workspace. It shows
  only the strict eight-field normalized source candidate whitelist and edits
  every completion field: company, title, full JD, locations, recruitment
  types, industries, apply URL, referral code, and deadline.
- Enforced the action matrix: pending completion can save/reject, pending review
  can save/verify/reject, rejected can save only, and verified expiry exists only
  in the separate `GET /admin/jobs/verified` lifecycle list.
- Added stable enumerated reject and expire reason codes with human-readable UI
  labels. Arbitrary text is never sent as `reason_code`.
- Mail, QR, WeChat-style, and explicit QR-code channels force
  `gui_eligible=false`; verification remains disabled while completion edits are
  dirty or until the GUI/manual choice is explicit.
- Added loading, empty, total/page feedback, busy and duplicate-submit guards,
  before-unload protection, guarded job/tab/refresh/logout transitions, and
  monotonically versioned list requests.
- Every async write captures the target job id and version. Late save/decision
  responses update only their target and preserve a newer user selection.
- Only exact HTTP 409 plus `detail.error_code === "stale_job_review"` triggers
  automatic reload. Reload failure, invalid transition, 401, 403, 422, and
  general failures have distinct safe messages.
- Added administrator-only navigation. Logout and profile role loss reset the
  workspace to analysis, preventing an admin-to-student blank view.

## TDD RED

Initial focused command:

```powershell
npm.cmd --prefix frontend run test -- AdminJobReview.spec.ts jobsApi.spec.ts App.spec.ts
```

The intended RED failed because `AdminJobReview.vue`, all four administrator API
functions, and the administrator App navigation did not exist. The component
suite failed import resolution, two administrator API tests failed with missing
functions, and two App tests failed because the role-only tab was absent. The
four pre-existing tests in those files still passed.

During self-review, two additional regression tests were first observed RED:

- a late verification reload switched from the administrator's newer selection
  back to the first queue row;
- explicit refresh discarded a dirty draft without confirmation.

Both failed deterministically before the selection-preserving reload and refresh
guard were added.

## GREEN and verification

Fresh final evidence:

- Focused administrator/API/App tests: **3 files, 25 tests passed**.
- Full frontend tests: **5 files, 43 tests passed**.
- `npm.cmd --prefix frontend run build`: passed, **21 modules transformed**.
- `npm.cmd --prefix frontend ci --ignore-scripts`: passed; package/lock are
  consistent and unchanged.
- `npm.cmd --prefix frontend ls --depth=0`: passed with the declared dependency
  tree intact.
- `git diff --check`: passed.

The tests cover role/navigation/logout, loading/empty/errors, paging and queue
status, full completion payload/version, dirty/busy/double-click/late responses,
exact error branching, the complete state/action matrix, manual-channel GUI
rules, stable reasons, source-change warnings, forbidden upstream fields,
verify reload, expire success, and stale expire reload failure.

## Concerns

- No blocking concerns.
- The project has no `typecheck` script or `vue-tsc` dependency, so no standalone
  TypeScript typecheck claim is made. Vite production bundling and Vitest
  compilation both passed.
