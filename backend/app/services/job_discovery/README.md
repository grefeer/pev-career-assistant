# Job Discovery Service

The job-discovery subsystem turns a URL (Tencent Smartsheet, manual import, or
admin-created task) into verified `DiscoveredJobCandidate` records for admin
review. Routing is decided by the **Strategy Router**; execution follows one of
three PEV paths, with a coverage-unverified legacy fallback.

## Execution Paths (PEV gray migration)

| Path | What it is | Coverage | When it runs |
|------|-----------|----------|--------------|
| **PATH A** | Certified site driver / adapter (`DomainAdapter`) | Verified | A matching `JobDiscoveryStrategy` with an `adapter` is **enabled** and PEV is on |
| **PATH B** | Deterministic executor replaying a `SnapshotPlan` + `CrawlPlan` | Verified | A matching strategy has a `plan_yaml` and PEV is on |
| **PATH C** | `CrawlPlan` generation / repair agent (planner) | Verified | No strategy matches; PEV + planner on |
| **Legacy PATH C** | Supervisor Agent (LLM-in-the-loop) | **Unverified** | PEV off, planner unavailable, or adapter/executor fallback |

- **CoverageVerifier is the only completion authority.** A run counts as
  complete only when `verify_coverage` returns a positive terminal verdict
  (`completion_evidence` present). Legacy Supervisor runs have no coverage and
  are therefore always `coverage-unverified`.
- **Legacy results are saved but never mixed with PEV PASS.** The worker
  summary tags each result with `execution_path`
  (`path_a_adapter` / `path_b_crawl_plan` / `legacy_path_c`),
  `coverage_verified` (bool), `coverage` (dict | None), and
  `legacy_fallback_reason` (str | None) so admin/eval output can separate them.

## PEV PASS definition

A result is **PEV PASS** only when ALL hold:

```
coverage_verified   = true
coverage_complete   = true
failed_detail_count = 0
candidate_count     == unique_listing_count   (canonical multi-region merges
                                               reported separately, not as dups)
count_apply_url_is_listpage = 0
body coverage       = 100%, legal auth walls excepted
```

Legacy (coverage-unverified) results are listed in a separate bucket and do
**not** count toward the PEV pass rate.

## Flags (`backend/app/config.py` -> `Settings`)

| Flag | Default | Effect |
|------|---------|--------|
| `job_discovery_strategy_enabled` | `False` | Consult the Strategy Router at all |
| `job_discovery_pev_enabled` | `False` | Enable PEV (PATH A / PATH B); when off, `CompleteCrawlAdapter` sites fall back to legacy |
| `job_discovery_planner_enabled` | `False` | Enable the PATH C planner agent (generates/repairs CrawlPlans) |
| `job_discovery_legacy_path_c_enabled` | `True` | Allow the legacy Supervisor as a coverage-unverified fallback |
| `job_discovery_planner_max_inspection_pages` | `3` | Planner inspection-page budget |

All four default to **gray** (PEV off, legacy on). Turning PEV on does not
rewrite anything; sites without an enabled adapter/plan simply flow through
PATH C or legacy.

## Gray Rollout

`scripts/seed_strategies.py` ships four site adapters disabled (`enabled=False`).
Promote them one at a time, in this order, only after **three consecutive
coverage-verified live smokes** pass (`GRAY_ROLLOUT_ORDER`):

1. **Moka** (`app.mokahr.com/*`)
2. **飞书** (`*.jobs.feishu.cn/*`)
3. **汇川** (`recruit.inovance.com/*`)
4. **小红书** (`job.xiaohongshu.com/*`)

Promotion = flip that one row's `JobDiscoveryStrategy.enabled` to `True`. Other
sites stay legacy.

### Rollback (per-site, disable only that strategy)

Disable a promoted site on ANY of (`GRAY_ROLLBACK_TRIGGERS`):

- expected/raw listing count drift
- positive terminal signal lost (no `completion_evidence`)
- `failed_detail_count > 0`
- `count_apply_url_is_listpage > 0`
- new blocked marker (`login` / `captcha` / `anti_bot` / `permission_denied`)
- listing count inconsistent across 3 consecutive runs

Rollback does **not** delete the new contracts, modify the global result
invariant, or affect already-stable sites.

## Manual Review (hard gates)

- Login / captcha / anti-bot / authenticated career walls -> `needs_manual_review`
  (Task 7: 401/403 with SPA session-auth is `authentication_required`, mapped to
  `DiscoveryBlockReason.permission_denied`). Never bypassed, never retried as
  structure.
- WeChat snapshots never enter the CrawlPlan agent; a hard deadline terminates
  hangs (Task 6).
- The agent never auto-submits; final submit is always human-controlled.

## Live Gates

```powershell
# 10-URL eval (Step 9) - direct supervisor baseline across 10 public URLs,
#   bucketed by the PEV PASS gate. Skips (never PASS) without the gate env + key.
$env:RUN_TEN_URL_EVAL='1'
$env:JOB_DISCOVERY_PEV_ENABLED='1'
.\.venv\Scripts\python.exe tests/integration/job_discovery/test_supervisor_ten_url_eval.py -v

# Per-site promotion smoke - run a gray site through PATH A with its strategy
#   enabled; require 3 consecutive PASS before promoting.
$env:FLAGS_use_onednn='0'
.\.venv\Scripts\python.exe tests/manual/test_pev_live_smoke.py --site moka
```

See `docs/job-discovery-agent-workflow.md` for the architecture and
`docs/job-discovery-agent-operations.md` for startup/config/operations.
