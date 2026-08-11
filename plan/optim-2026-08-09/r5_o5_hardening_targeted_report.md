# Round 5 hardening targeted evaluation

## Scope

Eight mechanism-focused live questions were run after the final unit-test
pass: `Q071`, `C003`, `C014`, `Q133`, `Q040`, `R042`, `R002`, and `R004`.
The run used real model/public-tool paths and the exact requested `.venv`
interpreter. It completed with 8/8 JSON results and return code 0.

## Results

| ID | Result | Observation | Interpretation |
|---|---|---|---|
| C003 | succeeded | preparation plan produced | O5 evidence/structured fallback path remains usable |
| C014 | waiting_user | Liepin detail page returned login/empty shell | security boundary remains honest; no login bypass |
| Q071 | waiting_user | no usable JD evidence in current step | evidence gate prevents an ungrounded completion |
| Q133 | waiting_user | sheet quota exhausted and public search failed | zero-review false PASS converted to an honest hand-off |
| Q040 | succeeded | control path succeeded with 2 artifacts | legitimate successful path preserved |
| R042 | waiting_user | live Smartsheet quota exhausted | external data-source condition, not a code conclusion |
| R002 | waiting_user | live Smartsheet quota exhausted | external data-source condition, not OCR evidence |
| R004 | waiting_user | live Smartsheet quota exhausted | external data-source condition, not OCR evidence |

The targeted run had zero verifier decisions on these cases. That is expected
for the blocked/early hand-off paths and should not be counted as verifier
coverage. The important result is that Q071 and Q133 no longer receive a
tool-free or OCR-failure-backed success status.

## Resource check

- 54 telemetry samples, 10-second interval.
- Minimum available memory: 16.75 GB.
- Maximum observed eval-tree RSS: 0.20 GB.
- After completion: no `chrome-headless-shell.exe` or `node.exe` process remained.

## Boundary

This targeted run does not claim a full-83 score. C014 did not become a PASS;
the captured login-wall evidence correctly keeps it human-gated. R002/R004/R042
were dominated by the live Smartsheet quota wall and are not valid OCR
comparisons. The three-round 83-question stability evaluation remains the final
step and has not been restarted yet.
