# Task 3A Report: Align manual job application channels

## Status

DONE

## Commit

- `fix: align manual job application channels` (this commit)
- The commit is limited to the Task 3A backend validator, service/API tests,
  administrator UI matcher/tests, and this report.

## Implementation

- Added one `_valid_manual_opaque_channel` helper for the `qr`, `weixin`, and
  `wechat` schemes.
- Manual opaque channels require a non-empty path, reject hierarchical
  authorities, queries, fragments, and all whitespace through the existing
  safe split boundary, and can be verified only with `gui_eligible=false`.
- Preserved the existing HTTP(S) hostname/port validation and strict mailto
  address validation unchanged.
- Kept the administrator UI aligned to the exact manual scheme set
  (`mailto`, `qr`, `weixin`, and `wechat`) through one shared matcher used by
  both initialization and live edits.
- Removed the UI heuristic that treated arbitrary `二维码`, `qrcode`, or
  `qr-code` text, including those strings inside an HTTP URL, as a manual
  application channel.

## TDD RED

Service RED:

```powershell
D:\Python\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe -m pytest tests/unit/test_job_review_service.py -k 'manual_application_channels or malformed_application_channels' -q
```

Result: the six new accepted/manual-only cases failed at `save_completion`
with `IncompleteJobError("apply_url")`; the invalid locator cases already
remained rejected without raw URL exceptions.

API RED:

```powershell
D:\Python\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe -m pytest tests/contract/test_jobs_api.py::test_admin_can_save_and_verify_qr_application_without_gui -q
```

Result: the QR completion request returned HTTP 422 instead of 200.

Frontend RED:

```powershell
D:\nodejs\npm.cmd test -- src/features/jobs/__tests__/AdminJobReview.spec.ts --reporter=verbose
```

Result: 4 failed and 26 passed. The four failures proved that free-form
`二维码`, `qrcode`, `qr-code`, and an HTTP URL containing `二维码` were being
misclassified as manual schemes.

## TDD GREEN

Focused backend service/API suites:

```text
100 passed in 15.66s
```

Focused administrator UI suite:

```text
30 passed in 3.20s
```

The regressions cover:

- `qr`, `weixin`, and `wechat` save + verify with `gui_eligible=false`;
- rejection of all three schemes with `gui_eligible=true`;
- empty, whitespace-bearing, hierarchical, unsupported, and plain-text
  locators without leaking `urlsplit` or port errors;
- API completion to verification for a QR channel, preserving
  `status=verified` and `gui_eligible=false`;
- exact UI recognition for mailto/QR/Weixin/Wechat schemes and rejection of
  unsupported free-form claims.

## Final verification

- Full Python: `627 passed, 10 skipped in 81.08s`.
- Ruff: `All checks passed!` for `backend` and `tests`.
- Full frontend: `5 passed` files and `56 passed` tests.
- Production frontend build: Vite built 21 modules successfully.
- `git diff --check`: passed.

All Python commands used the root project virtual environment. Frontend
commands used `D:\nodejs\npm.cmd` because `C:\Windows\System32\npm` is a
zero-byte placeholder executable that returns success without running npm.

## Concerns

- No blocking concerns.
- The 10 Python skips are existing opt-in integration gates in the default
  local environment; no Task 3A test was skipped.
