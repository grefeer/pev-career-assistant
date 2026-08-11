# Round 5 O5 hardening — full-test record

## User-provided full-test history

The current O5 full-test results are recorded as:

| Run | Score | Rate |
|---:|---:|---:|
| 1 | 65/83 | 78.3% |
| 2 | 65/83 | 78.3% |
| 3 | 71/83 | 85.5% |
| 4 | 70/83 | 84.3% |
| 5 | 69/83 | 83.1% |

Aggregate: **340/415**, mean **68.0/83 (81.9%)**, minimum **65**, maximum
**71**, spread **6** questions. Therefore, 71/83 is an observed peak rather
than a stable guaranteed score.

## Round 5 hardening checkpoint

- The post-hardening full round completed with 83/83 result files and exit
  code 0: 69 succeeded, 14 waiting_user, 0 failed.
- Resource telemetry for that round recorded a minimum of 15.81 GiB available
  memory and a maximum evaluation process-tree RSS of 0.87 GiB. The process
  tree was empty at completion.
- A subsequent round was stopped by user request after 10/83 results. Its
  files and logs remain on disk and are excluded from all score calculations.

## Interpretation

The five-run sequence shows material environment/evidence variability. The
appropriate headline is the observed range **65–71/83**, not the peak 71/83.
The next useful evaluation should preserve identical runtime parameters and
report per-ID flips, especially for external-source, login-wall, quota, and
JD-evidence cases.
