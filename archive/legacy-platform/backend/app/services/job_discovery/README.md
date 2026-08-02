# Job Discovery Service

The job-discovery subsystem turns a URL (Tencent Smartsheet, manual import, or
admin-created task) into `DiscoveredJobCandidate` records. By default the
**Skill Discovery Runtime** (`create_deep_agent` + job-discovery Skill +
restricted `run_skill_script` tool + per-evidence-page `jd_extractor`
subagent) handles public-page browsing, per-page JD extraction, dedup, and the
coverage gate.

Candidates are **no longer promoted to `JobPosting(verified)` via admin review**.
They are delivered to users through **Personalized Discovery v1** (pre-review,
owner-scoped recommendation, card labelled 「自动发现，建议自行确认」). The
verified-only `/api/jobs` job center is fed by the WP2 manual-import/completion
workflow and is decoupled from discovery candidates.

> See [docs/job-discovery-legacy-architecture-summary.md](../../../../docs/job-discovery-legacy-architecture-summary.md)
> for the superseded Supervisor / Strategy Router / PEV architecture (now a
> rollback-only fallback).

## Default runtime: Skill Discovery Runtime

`JOB_DISCOVERY_SKILL_RUNTIME_ENABLED=true` (default in
`backend/app/config.py`). The worker calls the skill runtime **before** any URL
strategy match, adapter, or legacy Supervisor, so those paths do not
participate in a default task. They survive only as rollback code when the flag
is explicitly disabled.

- Per-task isolated artifacts under
  `JOB_DISCOVERY_SKILL_ARTIFACT_ROOT/<task_id>/skill/job-discovery/`
  (`output/evidence/pages/*.txt`, screenshots, `tool_trace.jsonl`,
  `browse_metadata.json`, `coverage_gate_result.json`, `output/candidates_merged.json`).
- Restricted script whitelist: `browse / validate / normalize / deduplicate /
  ocr_image / state / read_evidence / write_candidates / coverage_gate`.
- `SkillToolPolicy` budget: `max_browse_calls=2`, `max_coverage_gate_calls=1`,
  `max_pages=20`, `max_candidates=10`. The runtime uses its own browse/coverage
  counters as the completion gate — **not** the legacy PEV `verify_coverage`.
- System prompt explicitly forbids bypassing login/captcha/anti-bot and forbids
  using URL adapters or strategy matching.
- Persistence: `result_summary_json` (`execution_path=skill_agent`, no raw model
  messages), `discovered_job_candidates`, `job_discovery_evidence` (`storage_uri`
  → isolated artifact files), `job_discovery_trajectories` (safely truncated tool
  name/status/duration; no tokens / raw model sessions).

## Discovery delivery: Personalized Discovery v1

Candidates do **not** go through admin approve/reject → `JobPosting(verified)`.
They are delivered via Personalized Discovery v1 — owner-scoped pre-review
recommendations that skip admin review, card labelled 「自动发现，建议自行确认」.

- Task-level gate: 证据核验 + 覆盖完整 + URL 安全 + 去重 + 相关性达标.
- Independent of the verified-only `/api/jobs` path; never mutates `JobPosting`,
  `JobRelevanceScore`, or `review_version`.
- Initial coverage: only the four migrated complete-crawl adapters (Moka, Feishu,
  Inovance, Xiaohongshu); other sources produce only owner-scoped status.
- See [docs/superpowers/specs/2026-07-25-personalized-job-discovery-v1-design.md](../../../../docs/superpowers/specs/2026-07-25-personalized-job-discovery-v1-design.md).

> **Target state (2026-07-29)**: discovery candidates bypassing admin review via
> personalized discovery is the documented target architecture. Code-side
> discovery candidate `approve`/`reject` → `JobPosting` promotion and
> `AdminJobReview.vue` still exist; migration is tracked separately.

---

## Legacy execution paths (PEV gray migration — fallback only)

> The paths below run **only when `JOB_DISCOVERY_SKILL_RUNTIME_ENABLED=false`**.
> With the default (true), none of them participate. Kept as reference for the
> rollback path.

Routing was decided by the **Strategy Router**; execution followed one of three
PEV paths, with a coverage-unverified legacy fallback.

| Path | What it is | Coverage | When it runs |
|------|-----------|----------|--------------|
| **PATH A** | Certified site driver / adapter (`DomainAdapter`) | Verified | A matching `JobDiscoveryStrategy` with an `adapter` is **enabled** and PEV is on |
| **PATH B** | Deterministic executor replaying a `SnapshotPlan` + `CrawlPlan` | Verified | A matching strategy has a `plan_yaml` and PEV is on |
| **PATH C** | `CrawlPlan` generation / repair agent (planner) | Verified | No strategy matches; PEV + planner on |
| **Legacy PATH C** | Supervisor Agent (LLM-in-the-loop) | **Unverified** | PEV off, planner unavailable, or adapter/executor fallback |

- **CoverageVerifier is the only completion authority** (legacy paths). A run
  counts as complete only when `verify_coverage` returns a positive terminal
  verdict (`completion_evidence` present). Legacy Supervisor runs have no
  coverage and are therefore always `coverage-unverified`.
- **Legacy results are saved but never mixed with PEV PASS.** The worker summary
  tags each result with `execution_path`
  (`path_a_adapter` / `path_b_crawl_plan` / `legacy_path_c`),
  `coverage_verified` (bool), `coverage` (dict | None), and
  `legacy_fallback_reason` (str | None) so admin/eval output can separate them.

### PEV PASS definition (legacy)

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
| `job_discovery_skill_runtime_enabled` | `True` | **Default path**: Skill Discovery Runtime runs before strategy/adapter/Supervisor |
| `job_discovery_skill_artifact_root` | `var/job-discovery-skill` | Per-task isolated skill artifact root |
| `job_discovery_strategy_enabled` | `False` | Consult the Strategy Router at all (legacy) |
| `job_discovery_pev_enabled` | `False` | Enable PEV (PATH A / PATH B); when off, `CompleteCrawlAdapter` sites fall back to legacy |
| `job_discovery_planner_enabled` | `False` | Enable the PATH C planner agent (generates/repairs CrawlPlans) |
| `job_discovery_legacy_path_c_enabled` | `True` | Allow the legacy Supervisor as a coverage-unverified fallback |
| `job_discovery_planner_max_inspection_pages` | `3` | Planner inspection-page budget |

With the skill runtime default on, the legacy flags stay gray (PEV off, legacy
on) and are inert. Turning PEV on does not rewrite anything; sites without an
enabled adapter/plan simply flow through PATH C or legacy — and only when the
skill runtime is disabled.

## Gray Rollout (legacy PEV adapters)

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
  (401/403 with SPA session-auth is `authentication_required`, mapped to
  `DiscoveryBlockReason.permission_denied`). Never bypassed, never retried as
  structure.
- WeChat snapshots never enter the CrawlPlan agent; a hard deadline terminates
  hangs.
- The agent never auto-submits; final submit is always human-controlled.

## Live Gates (legacy path verification)

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
