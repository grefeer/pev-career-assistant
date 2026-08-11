# Round 5 O5 hardening plan

## Objective

Implement the approved five-part hardening pass in the isolated worktree
`codex/round5-o5-hardening`, preserve all existing evaluation logs, and rerun
three full 83-question stability evaluations after verification.

## Status

- [x] Read the Round 5 plan and required platform/PEV documents.
- [x] Confirm the requested `.venv` contains PaddleOCR, PaddlePaddle, and PIL.
- [x] Run the first cold PaddleOCR probe; no production OCR toggle has been changed.
- [x] Add failing tests for lifecycle, evidence honesty, retry ledger, and OCR cache.
- [x] Implement and verify the four code hardening areas.
- [x] Write the OCR probe decision report; keep OCR disabled because the measured path is borderline and lacks safety margin.
- [x] Run targeted tests, relevant unit tests, and lint.
- [x] Run targeted live eval and audit Q071/C003/C014/Q133 plus controls.
- [x] Run full unit suite with project `.env` injected only into the test process: 1508 passed.
- [x] Run ruff and `git diff --check`.
- [ ] Run three new full 83-question stability evaluations with resource telemetry. Round 1 completed; Round 2 was intentionally stopped at 10/83 per user request; Round 3 has not started.
- [ ] Complete flip/resource audit and final verification.

## Constraints

- No edits to `tests/question/eval_runner.py` or existing test files.
- No skip/xfail, answer hardcoding, login/captcha/anti-bot bypass, or task submission.
- Do not delete or overwrite previous logs.
- Use the exact user-selected Python interpreter for all Python commands.

## Evidence so far

- Interpreter: `D:\Program Files\JetBrains\PyCharm Community Edition 2024.2.2\proj\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe`.
- Cold probe: `temp/wechat_repro_out/ocr/wechat_img_00.png`, 1080x2169, two OCR slices, 56.7 seconds wall time, 36 output characters, confidence 0.9163.
- Same content hash cache hit: 106 ms in the CLI process.
- This is single-image timing; a multi-image WeChat article remains above the 60–120 second/article target unless all images are cache hits.

## Full evaluation checkpoint

- Round 1: `tests/question/eval_results/r5_o5_hardening_final_1/`, 83/83,
  return code 0; 69 succeeded, 14 waiting_user, 0 failed. Resource telemetry
  recorded a minimum of 15.81 GiB available memory and a maximum evaluation
  process-tree RSS of 0.87 GiB; the tree was empty at completion.
- Round 2: `tests/question/eval_results/r5_o5_hardening_final_2/`, 10/83
  partial JSON results, stopped by user request; return code 1. These results
  are retained but excluded from all stability statistics.
- Round 3: not started.

## Recorded full-test history

- User-provided O5 full-test sequence: **65/83, 65/83, 71/83, 70/83,
  69/83**.
- Aggregate: **340/415**, mean **68.0/83 (81.9%)**, range **65–71/83**.
- The 71/83 result is therefore a peak observation, not a stable guarantee.
- Detailed record: `round5_o5_hardening_final_report.md`.

## Decision rule

Keep WeChat OCR disabled for live evaluation unless a representative article
probe demonstrates the full article is within 120 seconds and failures remain
`wechat_ocr_failed`/human-gated. Implement content-hash caching regardless.
