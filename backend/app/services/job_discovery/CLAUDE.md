# CLAUDE.md - Job Discovery Agent System

## Overview

The Job Discovery subsystem is the core of the platform's automated recruitment pipeline. It takes URLs from external sources (Tencent Smartsheet, manual import), classifies them via the **Strategy Router**, dispatches to the optimal execution path (**Adapter**, **SnapshotExecutor**, or **Supervisor Agent**), extracts structured job candidates, verifies them against evidence, and produces `DiscoveredJobCandidate` records for admin review.

## Architecture

```
                    ┌──────────────────────┐
                    │   StrategyRouter     │  URL -> fnmatch -> StrategyRecord
                    │   (strategy_router)   │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
     ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
     │ PATH A       │  │ PATH B       │  │ PATH C       │
     │ Adapter      │  │ SnapshotExec │  │ Supervisor   │
     │ (alibaba_spa)│  │ (YAML replay)│  │ (LLM Agent)  │
     │ ~12s          │  │ ~4s          │  │ 2-7 min       │
     └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
            │                 │                  │
            │  on failure     │  on failure      │
            └─────────────────┼──────────────────┘
                              ▼
                    ┌──────────────────┐
                    │  TrajectoryBuffer │  ← shared in-memory trace recorder
                    │  snapshot_context │  ← passed to Supervisor on takeover
                    └──────────────────┘
                              │
                              ▼
              ┌───────────────────────────────┐
              │  extract -> verify -> package   │  deterministic pipeline
              │  (jd_extraction, verifier,     │  (PATH A/B run it as a post-step;
              │   candidate_packager)          │   PATH C runs it *inside*
              └───────────────────────────────┘   run_web_navigation)
                              │
                              ▼
                   DiscoveryRunResult
                   (evidence + candidates)
```

## File Structure

```
job_discovery/
├── deepagents_runner.py          # ★ Core (~2990 lines)
│                                   Supervisor Agent builder, Web Nav Agent,
│                                   all tool wrappers, _fetch_alibaba_search_api,
│                                   fetch_wechat_article, _cached_fetch,
│                                   _fix_response_encoding (Chinese mojibake fix),
│                                   _dismiss_consent_dialog + consent allow/blocklists,
│                                   _extract_and_verify_candidates_from_evidence,
│                                   _extract_title_only_candidates,
│                                   _is_plausible_job_title (title false-positive filter)
│
├── result_contract.py            # ★ Supervisor result parsing/recovery
│                                   parse_agent_result: parse structured output,
│                                   and recover evidence/candidates from
│                                   run_web_navigation / extract_rendered_job_evidence
│                                   / package_candidates tool messages (so the final
│                                   LLM message cannot erase collected jobs)
│                                   enforce_result_invariants: succeeded=>needs candidates
│                                   _dedupe_candidate_dicts: semantic dedup at both the
│                                   tool-recovery and structured-merge paths (delegates
│                                   to deduplication/)
│
├── deduplication/                 # Candidate semantic deduplication (new)
│   └── canonical_job_deduplicator.py  # deduplicate_candidates(): _identity_key =
│                                       ("jd", company, core_hash) for full-JD,
│                                       ("title", normalize_title) for title-only;
│                                       _cluster_by_title_substring (city/level variants)
│
├── normalization/                 # JD text normalization (new)
│   └── jd_normalizer.py           # normalize_title/company/text + core_hash:
│                                   NFKC fold, zero-width strip, trailing （…） strip,
│                                   structural-punct delete (deletes 【】 chars, keeps content)
│
├── schemas.py                    # Pydantic/dataclass: DiscoveryTaskInput,
│                                   DiscoveryRunResult, PageEvidence,
│                                   NormalizedJobCandidate, StrategyRecord (in-memory),
│                                   OcrResult, WechatArticleResult
│
├── worker.py                     # Polling loop: claim task -> execute -> update status;
│                                   imports result_contract for result parsing
├── tasks.py                      # Background task definitions
│
├── adapters/                     # Domain-specific fast-path adapters
│   ├── base.py                   # DomainAdapter ABC (execute + validate)
│   └── alibaba_spa.py            # AlibabaSPAAdapter: Playwright -> XHR capture ->
│                                   per-evidence JD extraction -> verify -> package
│
├── strategy/                     # Strategy Router subsystem
│   ├── strategy_router.py        # URL fnmatch -> StrategyRecord lookup (MySQL)
│   ├── strategy_store.py         # CRUD + atomic state machine
│   │                               (active -> degraded -> unavailable; see below)
│   ├── snapshot_executor.py      # Deterministic YAML plan replay
│   │                               Template variables: {{task.source_url}},
│   │                               {{prev.result}}, {{prev.result.xxx}}
│   ├── trajectory_buffer.py      # In-memory tool-call recorder
│   │                               -> to_snapshot_context() on failure
│   │                               -> to_dict() for persistence
│   ├── trajectory_store.py       # Persist trajectories to MySQL
│   ├── trajectory_annotator.py   # LLM annotation of failed execution traces
│   └── error_classifier.py       # Classify errors (blocked/transient/permanent)
│
├── tools/                        # Deterministic tools (Agent-callable)
│   ├── link_triage.py            # URL -> {site_type, confidence, action}
│   ├── jd_extraction.py          # Regex/keyword JD extraction (NO LLM)
│   │                               _split_multi_job_page -> max 2 segments
│   │                               _extract_title, _extract_company, _extract_section
│   ├── evidence_verifier.py      # Filter candidates: evidence_refs, staleness, vagueness
│   ├── candidate_packager.py     # SHA-256 idempotency_key + similarity_group_key
│   ├── wechat_article_parser.py  # HTMLParser for WeChat articles
│   │                               Extracts: title, text, image_urls, email_instructions
│   └── ocr_pipeline.py           # ocr_image: PaddleOCR PP-OCRv5 (primary) or
│                                   Tesseract (fallback); WebP->PNG conversion;
│                                   PNG/JPEG dimension parsing; tall-image slicing
│
└── prompts/                      # Supervisor system prompt templates
    ├── supervisor_base.txt        # Base system prompt (includes prompt-level Loop Prevention)
    └── supervisor_clean_start.txt # Instructions when no snapshot_context

    Note: `build_supervisor_prompt` also calls `_load_prompt("supervisor_snapshot_fallback",
    required=False)` on takeover, but that file does not exist - the takeover branch
    silently falls back to base-only. Treat as dead-defensive loading; do not rely on a
    takeover-specific template until one is authored.
```

## Three Execution Paths

### PATH A - Adapter (fastest: ~12s)

**When**: `StrategyRecord.adapter` is set (e.g. `AlibabaSPAAdapter`).

**Flow**: Direct Python code, no LLM planning.
1. Adapter calls `_fetch_alibaba_search_api(url)` -> Playwright headless Chromium
2. Navigates SPA, captures XHR JSON responses + DOM visible text
3. `_generic_position_evidence_from_payload` walks each payload (max depth 5) looking for job-like objects (≥2 indicator fields + non-empty description/requirement)
4. Per-evidence JD extraction: each evidence item fed individually to `extract_jd_candidates` -> dedup by title+locations
5. `evidence_refs` injected before `verify_evidence` (deterministic extractor doesn't set them)
6. `package_candidates` adds idempotency keys

**On failure**: Raises exception -> trajectory records failed step -> Supervisor takeover with `snapshot_context`.

### PATH B - SnapshotExecutor (fast: ~4-6s)

**When**: `StrategyRecord.plan_yaml` is set (e.g. WeChat strategy).

**Flow**: Deterministic YAML replay, no LLM planning.
1. Parse YAML plan into ordered steps
2. Template substitution: `{{task.source_url}}`, `{{prev.result.text}}`, `{{prev.result.xxx}}`
3. Execute each step via `_call_tool_by_name(tool_name, **resolved_params)`
4. If all steps succeed: `_build_final_result()` aggregates evidence + candidates
5. Auto-generates `PageEvidence` (`evidence_type="rendered_page"`, `metadata.source="snapshot_auto"`) from text-producing steps when candidates exist but no explicit evidence

**WeChat plan** (3 deterministic steps):
```yaml
plan:
  - tool: triage_link           # URL -> site_type
    params: {url: "{{task.source_url}}"}
  - tool: fetch_wechat_article  # ReadGZH -> OCR images -> extract emails/URLs
    params: {url: "{{task.source_url}}"}
  - tool: extract_jd_candidates # Deterministic keyword extraction
    params: {page_text: "{{prev.result.text}}", url: "{{task.source_url}}"}
```

**On failure**: Returns `SnapshotExecutionResult(needs_supervisor_fallback=True)` with `snapshot_context`.

**On 0 candidates**: If `needs_manual_review` flag detected in trajectory -> skip Supervisor (blocked article). Otherwise -> Supervisor handoff.

### PATH C - Supervisor Agent (slow: 2-7 min)

**When**: No strategy match, or adapter/snapshot failure fallback.

**Flow**: LLM-in-the-loop DeepAgent.
1. `build_discovery_supervisor_agent(settings, model, snapshot_context)`
2. 9 tools: `triage_link`, `run_web_navigation` (-> WebNavigationAgent subagent), `parse_wechat_article`, `run_ocr`, `extract_jd_candidates`, `standardize_from_record_fields`, `verify_evidence`, `package_candidates`, `finish_with_manual_review`
3. `snapshot_context` injected into system prompt on takeover
4. L1 Loop Prevention: prompt-level rules (max 12 calls, no retries, fallback at 6)
5. Structured output via `_DiscoveryRunResultPydantic`
6. Final result parsed/recovered by `result_contract.parse_agent_result` +
   `enforce_result_invariants` (in worker.py) so collected jobs survive even if
   the LLM's final message is malformed or empty

**`run_web_navigation` is not a pure LLM delegation.** It also:
- Captures a **deterministic baseline** of the start URL's rendered evidence
  (`extract_rendered_job_evidence`) for non-WeChat URLs, so evidence does not
  depend on the inner Web Navigation Agent LLM choosing to call that tool.
- Merges agent-captured evidence with the baseline, deduping by `content_hash`.
- Deterministically extracts + verifies + packages candidates via
  `_extract_and_verify_candidates_from_evidence`, so its return dict carries
  `candidates` and `evidence_hash` directly (the Supervisor does NOT need to call
  `extract_jd_candidates`/`verify_evidence`/`package_candidates` itself for
  career sites — `supervisor_base.txt` says so explicitly).

**`_extract_and_verify_candidates_from_evidence`** (supervisor module): runs the
strict `extract_jd_candidates` per evidence page, attaches the `evidence_refs`
that `verify_evidence` requires, sanitizes empty/zero-width-only titles
(`​‌‍﻿`), and for `page_text` list pages with no JD-detail structure falls back
to `_extract_title_only_candidates` (loose suffix-based title extractor). The
latter yields *title-only* candidates flagged via `normalization_warnings`
(detail-page JD bodies are behind consent/privacy interstitials that this
system never circumvents — security hard gate #2).

**`_extract_title_only_candidates`** matches lines ending in `_JOB_TITLE_SUFFIXES`
(工程师/分析师/.../产品经理/管培生, plus 运营/制作). Each match is cleaned before the
suffix check: leading `【...】` campaign prefix, **trailing `【...】` cohort tag**
(e.g. ...研发工程师【2027届云弧计划】), trailing `（...）`, and trailing `-XXX`.

**`_is_plausible_job_title`** post-filters false positives with three rules:
(1) title contains `|` (banner/separator); (2) title is a bare **generic** category
word (经理/主管/总监/负责人/运营/制作) - bare *specific* role titles like
产品经理/工程师/管培生 are kept; (3) the title repeats across 2+ `page_text`
captures (sidebar tab, not a job).

**WebNavigationAgent subagent**: Separate DeepAgent (`build_web_navigation_agent`)
with 7 navigation tools (`open_url`, `open_rendered_url`,
`extract_rendered_job_evidence`, `read_dom`, `extract_links`, `click_link`,
`go_back`) and structured output via `_WebNavigationResultPydantic`.

`extract_rendered_job_evidence` may dismiss privacy/cookie consent interstitials
(see Consent Interstitial below) to read publicly-rendered JD content.

## Key Concepts

### StrategyRecord vs JobDiscoveryStrategy (ORM)

Two related objects; do not conflate:

- **`JobDiscoveryStrategy`** (ORM, `backend/app/db/models.py`, MySQL table
  `job_discovery_strategies`) is the persisted strategy. Notable columns:
  `url_pattern`, `adapter`, `plan_yaml`, `priority`, `status`, `enabled`,
  `degradation_threshold` (default 3), `recovery_threshold` (default 2),
  `success_count`, `error_count`, `consecutive_ok`, plus error/health-check
  audit fields. This is what `strategy_store` mutates and `strategy_router`
  matches against.
- **`StrategyRecord`** (`schemas.py`, in-memory dataclass) is a slim projection
  built via `StrategyRecord.from_orm(orm_obj)` for the execution path. It carries
  only: `id`, `url_pattern`, `site_type`, `description`, `priority`, `adapter`,
  `plan_yaml`, `status`, `success_count`. It does **not** carry the threshold
  fields — those live on the ORM model and are mutated by atomic SQL only.

Routing fields on both:
- `url_pattern` — fnmatch glob (e.g. `mp.weixin.qq.com/s/*`, `*talent.alibaba.com/*`)
- `adapter` — dotted class path for PATH A (e.g. `...adapters.alibaba_spa.AlibabaSPAAdapter`)
- `plan_yaml` — YAML plan for PATH B SnapshotExecutor
- `priority` — higher = checked first (ties broken by `success_count`)
- `status` — `active` / `degraded` / `unavailable`

### TrajectoryBuffer

In-memory recorder shared by all three paths. Every tool call records `{tool, status, params, result, error, error_type, timestamp}`. On failure: `to_snapshot_context()` builds `{source, strategy_id, completed_steps[], failed_step{}}`.

### Loop Prevention (prompt-level only)

The Supervisor relies on **prompt-level** loop prevention only: `supervisor_base.txt`
"Loop Prevention" section tells the LLM not to retry, max 12 calls, fallback at 6.

A prior programmatic guard (`_loop_guardian_wrap`, blocking the 3rd consecutive
identical tool+params call) was **removed** - it was never applied at runtime
(LangChain `StructuredTool` is incompatible with Python function wrappers) and
existed only as dead code with its own test. Do not re-add a function-wrapper
guard to Supervisor tools; rely on the prompt + `recursion_limit` instead.

### Supervisor invocation (streamed)

`invoke_supervisor_agent` runs the agent with `stream_mode="values"` rather than a
blocking `invoke`. On `GraphRecursionError` (large sites like xiaomi can hit
`recursion_limit`) it preserves the partial state already streamed instead of
discarding everything - so collected candidates survive a recursion crash.

### Page Cache (`_page_cache` + `_cached_fetch`)

Module-level dict `{url: (content, title, error)}`. WeChat URLs route through ReadGZH; others use `requests.get` (decoded via `_fix_response_encoding`). Subsequent `open_url`, `read_dom`, `extract_links` calls reuse cached result - no duplicate HTTP/ReadGZH calls per task.

### WeChat Raw HTML Cache (`_wechat_raw_html_cache`)

Separate `{url: raw_html}` cache populated by `_fetch_wechat_via_readgzh`. Used by `fetch_wechat_article` to extract `<img>` URLs for OCR without a second HTTP round-trip. Cleared by `_reset_nav_state`.

### Encoding Auto-Detection (`_fix_response_encoding`)

Many Chinese career sites serve pages with a misconfigured or missing `Content-Type charset`, so `requests` falls back to ISO-8859-1 and produces mojibake. `_cached_fetch` applies `resp.apparent_encoding` (or UTF-8) before decoding so the extracted text is readable.

### Consent Interstitial (`_dismiss_consent_dialog`)

`extract_rendered_job_evidence` may click a privacy/cookie consent button so publicly-rendered JD content becomes readable. Strictly bounded to the "read JD" path and the security hard gates:

- Only clicks elements whose text exactly matches (case-insensitive) an entry in
  `_CONSENT_BUTTON_TEXTS` (同意/Accept/Agree/Got it/…), ≤12 chars.
- Refuses to click anything if the page body (or the candidate button's text)
  contains any `_CONSENT_BLOCK_KEYWORDS`: 验证码/captcha/滑块/登录/扫码/robot,
  plus the WeChat verification-wall markers (`环境异常` / `完成验证后即可继续访问`).
- Never touches final-submit, login, captcha, or anti-bot elements. When a wall
  is detected the run surfaces `needs_manual_review` instead.

It also scrolls (up to 10 cycles; stops after 2 consecutive scrolls adding no
new XHR) to trigger lazy-loaded job listings, and always appends the rendered
`body.inner_text` as a `page_text` evidence page (the fallback the loose
title extractor reads when XHR payloads are encrypted/empty).

### Evidence Field Normalization (`verify_evidence`)

The tool normalizes field names from LLM/WebNavigationAgent output before
reconstructing `PageEvidence`: `type`→`evidence_type`, and
`content`/`page_text`/`text`/`description`→`text_excerpt`. Unknown keys are
dropped. This absorbs the common LLM habit of emitting `type`/`text` instead
of the dataclass field names.

### Candidate Deduplication (semantic)

The Supervisor re-runs `package_candidates` on evidence `run_web_navigation` already
packaged, producing candidates with different `idempotency_key` (byte-different) but
identical semantics - these survive the byte-level `_unique_items` check and create
duplicates. Semantic dedup collapses them:

- **`deduplication/canonical_job_deduplicator.py`** - `deduplicate_candidates()`:
  - Full-JD candidates (have `responsibilities`/`requirements`): identity key
    `("jd", normalize_company, core_hash(responsibilities, requirements))`.
  - Title-only candidates (no JD body, list-page fallback): identity key
    `("title", normalize_title(title))` - company deliberately EXCLUDED (one company
    per URL; `company_name` is attributed inconsistently across capture paths).
  - Within a full-JD identity group, `_cluster_by_title_substring` partitions by
    title overlap so a shared JD template does not merge distinct roles
    (算法工程师 vs 算法研究员) while city/level variants do merge
    (算法工程师 vs 算法工程师-北京).
- **`normalization/jd_normalizer.py`** - `normalize_title`/`normalize_company`/`normalize_text`
  + `core_hash`. NFKC fold, zero-width-char strip, **trailing （…）/(...) strip**
  (so 产品管培生 and 产品管培生（上海） merge), structural-punctuation delete
  (deletes 【】 bracket *characters* but keeps their content: "X【Y】" -> "XY").
- **`result_contract._dedupe_candidate_dicts`** - applied at **both** the tool-only
  recovery path and the structured-merge path, so neither emits duplicates regardless
  of which produced the candidates.

Result: 0 duplicates across all test sites (incl. 137-candidate xiaomi runs).

### Email Privacy

`fetch_wechat_article` extracts email addresses as structured `application_emails` metadata. The deterministic `extract_jd_candidates` (regex-based, no LLM) processes them safely - no raw emails ever enter an LLM prompt.

## Complete Tool Chain

| Tool | Source | Type | Signature |
|------|--------|------|-----------|
| `triage_link` | `tools/link_triage.py` | deterministic | `(url) -> dict` |
| `fetch_wechat_article` | `deepagents_runner.py` | deterministic + ReadGZH + OCR | `(url) -> dict` |
| `parse_wechat_article` | `tools/wechat_article_parser.py` | deterministic | `(html, url) -> dict` |
| `run_ocr` | `deepagents_runner.py` -> `tools/ocr_pipeline.py` | PaddleOCR (or Tesseract fallback) | `(image_base64) -> dict` |
| `extract_jd_candidates` | `tools/jd_extraction.py` | deterministic (regex) | `(page_text, url) -> JSON str` |
| `verify_evidence` | `tools/evidence_verifier.py` | deterministic (+field normalization) | `(candidates_json, evidence_json) -> JSON str` |
| `package_candidates` | `tools/candidate_packager.py` | deterministic | `(candidates_json, hash, source_key) -> JSON str` |
| `standardize_from_record_fields` | `deepagents_runner.py` | deterministic fallback | `(fields_json, evidence_json, url) -> JSON str` |
| `finish_with_manual_review` | `deepagents_runner.py` | signal | `(reason) -> dict` |
| `run_web_navigation` | `deepagents_runner.py` | LLM DeepAgent + deterministic extract/verify/package | `(start_url, settings=None, subagent=None, model=None) -> dict` |

`run_web_navigation` returns `evidence_pages`, `candidates` (already
extracted/verified/packaged), `evidence_hash`, `navigation_path`, `page_count`,
and `error`. For career sites the Supervisor should prefer it and reuse its
`candidates` directly rather than re-running the extraction tools.

WebNavigationAgent-only tools (not in the Supervisor's 9-tool list): `open_url`,
`open_rendered_url`, `extract_rendered_job_evidence`, `read_dom`,
`extract_links`, `click_link`, `go_back`.

## Testing

### Unit tests
```powershell
# All job_discovery unit tests
.\.venv\Scripts\python.exe -m pytest tests/unit/ -k job_discovery -v

# New subsystem unit tests (dedup / normalization / title filter / contract)
.\.venv\Scripts\python.exe -m pytest tests/unit/job_discovery/ -v

# Evidence verifier
.\.venv\Scripts\python.exe -m pytest tests/unit/ -k evidence_verifier -v
```

`tests/unit/job_discovery/` covers `test_canonical_job_deduplicator`,
`test_jd_normalizer`, `test_result_contract_dedup`, and `test_title_filter`
(title extraction + `_is_plausible_job_title` three-rule filter).

### Supervisor baseline (3 real URLs, gated)
```powershell
# PASS = unique == real_count AND dup_count == 0.
# deeproute 21 / pdd 22 reliably pass; xiaomi is environmentally limited
# (intermittent anti-bot; 137/151 with 0 dups when reachable).
$env:RUN_SUPERVISOR_BASELINE='1'
.\.venv\Scripts\python.exe -m pytest tests/integration/job_discovery/test_supervisor_baseline_real_urls.py -v
```

### Live smoke tests (require DEEPSEEK_API_KEY + TENCENT_DOCS_TOKEN + READGZH_API_KEY)
```powershell
# Full 4-URL strategy router smoke test (~120s)
.\.venv\Scripts\python.exe -u tests/manual/test_strategy_router_live_smoke.py

# Adapter failure -> Supervisor takeover verification
.\.venv\Scripts\python.exe -u tests/manual/test_adapter_failure_takeover.py
```

### Key env vars for live tests
- `DEEPSEEK_API_KEY` - LLM API key
- `TENCENT_DOCS_TOKEN` - Tencent Smartsheet API token
- `READGZH_API_KEY` - ReadGZH proxy API key (bypasses WeChat fingerprinting)

## Key Conventions

### When adding a new adapter
1. Subclass `DomainAdapter` in `adapters/`
2. Implement `execute(task, strategy, trajectory) -> DiscoveryRunResult`
3. Call `trajectory.record_step()` for each significant operation
4. On failure: raise exception (don't return failed result) - lets Supervisor take over
5. Register the dotted class path in the strategy's `adapter` field

### When adding a new snapshot tool
1. Define the function in `deepagents_runner.py`
2. Import + register in `snapshot_executor.py` `_ensure_tool_registry()`
3. Document in the YAML plan

### When modifying Supervisor prompts
1. Edit `prompts/supervisor_base.txt` for always-included content
2. Edit `prompts/supervisor_clean_start.txt` for clean-start instructions
   (`build_supervisor_prompt` reads these two files; the takeover-specific
   `supervisor_snapshot_fallback.txt` is referenced in code but not present -
   see File Structure note above.)

### Evidence format
- `evidence_type` values emitted by the code: `"job_detail_json"` (XHR),
  `"page_text"` (rendered DOM fallback, incl. consent-dismissed content),
  `"rendered_page"` (SnapshotExecutor auto-generated, with
  `metadata.source="snapshot_auto"`). `PageEvidence` also defines
  `screenshot`/`wechat_text`/`wechat_image`/`ocr_text`/`email_instruction`/
  `browser_trace` for other evidence kinds.
- `text_excerpt`: truncated to 1500 chars (XHR) / 5000 chars (snapshot auto) /
  8000 chars (rendered `page_text` baseline)
- Must include `content_hash` (SHA-256) for dedup
- `verify_evidence` requires `evidence_refs` on each candidate; the Supervisor's
  `_extract_and_verify_candidates_from_evidence` attaches them automatically

### Idempotency
- Candidate key: SHA-256 of normalized `company + title + location + apply_url + evidence_hash`
- Similarity group: SHA-256 of `company + title + recruitment_type + source_family`

## State Machine: JobDiscoveryStrategy (`status`)

```
active ──(1st error)──> degraded ──(error_count reaches degradation_threshold, default 3)──> unavailable
   ▲                          │
   └──(consecutive_ok reaches recovery_threshold, default 2)──┘
```

- `increment_error_count` (atomic SQL) flips to `degraded` on the **first** error,
  and to `unavailable` once `error_count` reaches `degradation_threshold`
  (default 3). The first failing CASE branch wins, so the threshold governs the
  degraded→unavailable transition — not active→degraded.
- `increment_success` recovers `degraded`→`active` after `recovery_threshold`
  (default 2) consecutive successes; it also resets `error_count` to 0.
- Only `active` and `degraded` strategies are returned by `get_active_strategies()`
  (filtered on `enabled == True`).
- `degradation_threshold` and `recovery_threshold` live on the ORM
  `JobDiscoveryStrategy`, not on the in-memory `StrategyRecord`.

## State Machine: JobDiscoveryTask

```
queued -> running -> succeeded | partial_success | needs_manual_review | failed | cancelled
```

Worker claims with lease timeout. Expired leases allow re-claim. `partial_success` when candidates found but budget exhausted.
