# CLAUDE.md — Job Discovery Agent System

## Overview

The Job Discovery subsystem is the core of the platform's automated recruitment pipeline. It takes URLs from external sources (Tencent Smartsheet, manual import), classifies them via the **Strategy Router**, dispatches to the optimal execution path (**Adapter**, **SnapshotExecutor**, or **Supervisor Agent**), extracts structured job candidates, verifies them against evidence, and produces `DiscoveredJobCandidate` records for admin review.

## Architecture

```
                    ┌──────────────────────┐
                    │   StrategyRouter     │  URL → fnmatch → StrategyRecord
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
              │  extract → verify → package   │  deterministic pipeline
              │  (jd_extraction, verifier,     │
              │   candidate_packager)          │
              └───────────────────────────────┘
                              │
                              ▼
                   DiscoveryRunResult
                   (evidence + candidates)
```

## File Structure

```
job_discovery/
├── deepagents_runner.py          # ★ Core (2072 lines)
│                                   Supervisor Agent builder, Web Nav Agent,
│                                   all tool wrappers, _fetch_alibaba_search_api,
│                                   fetch_wechat_article, _cached_fetch,
│                                   LoopGuardian (_loop_guardian_wrap et al)
│
├── schemas.py                    # Pydantic: DiscoveryTaskInput, DiscoveryRunResult,
│                                   PageEvidence, NormalizedJobCandidate,
│                                   StrategyRecord, OcrResult, WechatArticleResult
│
├── worker.py                     # Polling loop: claim task → execute → update status
├── tasks.py                      # Background task definitions
│
├── adapters/                     # Domain-specific fast-path adapters
│   ├── base.py                   # DomainAdapter ABC (execute + validate)
│   └── alibaba_spa.py            # AlibabaSPAAdapter: Playwright → XHR capture →
│                                   per-evidence JD extraction → verify → package
│
├── strategy/                     # Strategy Router subsystem
│   ├── strategy_router.py        # URL fnmatch → StrategyRecord lookup (MySQL)
│   ├── strategy_store.py         # CRUD + atomic state machine
│   │                               (active → degraded → unavailable, 3 failures)
│   ├── snapshot_executor.py      # Deterministic YAML plan replay
│   │                               Template variables: {{task.source_url}},
│   │                               {{prev.result}}, {{prev.result.xxx}}
│   ├── trajectory_buffer.py      # In-memory tool-call recorder
│   │                               → to_snapshot_context() on failure
│   │                               → to_dict() for persistence
│   ├── trajectory_store.py       # Persist trajectories to MySQL
│   ├── trajectory_annotator.py   # LLM annotation of failed execution traces
│   └── error_classifier.py       # Classify errors (blocked/transient/permanent)
│
├── tools/                        # Deterministic tools (Agent-callable)
│   ├── link_triage.py            # URL → {site_type, confidence, action}
│   ├── jd_extraction.py          # Regex/keyword JD extraction (NO LLM)
│   │                               _split_multi_job_page → max 2 segments
│   │                               _extract_title, _extract_company, _extract_section
│   ├── evidence_verifier.py      # Filter candidates: evidence_refs, staleness, vagueness
│   ├── candidate_packager.py     # SHA-256 idempotency_key + similarity_group_key
│   ├── wechat_article_parser.py  # HTMLParser for WeChat articles
│   │                               Extracts: title, text, image_urls, email_instructions
│   └── ocr_pipeline.py           # PaddleOCR (PP-OCRv5) image text extraction
│
└── prompts/                      # Supervisor system prompt templates
    ├── supervisor_base.txt        # Base system prompt (includes L1 Loop Prevention)
    ├── supervisor_clean_start.txt # Instructions when no snapshot_context
    └── supervisor_snapshot_fallback.txt  # Takeover instructions with
                                           # {completed_steps}, {failed_step_xxx}
```

## Three Execution Paths

### PATH A — Adapter (fastest: ~12s)

**When**: `StrategyRecord.adapter` is set (e.g. `AlibabaSPAAdapter`).

**Flow**: Direct Python code, no LLM planning.
1. Adapter calls `_fetch_alibaba_search_api(url)` → Playwright headless Chromium
2. Navigates SPA, captures XHR JSON responses + DOM visible text
3. `_generic_position_evidence_from_payload` walks each payload (max depth 5) looking for job-like objects (≥2 indicator fields + non-empty description/requirement)
4. Per-evidence JD extraction: each evidence item fed individually to `extract_jd_candidates` → dedup by title+locations
5. `evidence_refs` injected before `verify_evidence` (deterministic extractor doesn't set them)
6. `package_candidates` adds idempotency keys

**On failure**: Raises exception → trajectory records failed step → Supervisor takeover with `snapshot_context`.

### PATH B — SnapshotExecutor (fast: ~4-6s)

**When**: `StrategyRecord.plan_yaml` is set (e.g. WeChat strategy).

**Flow**: Deterministic YAML replay, no LLM planning.
1. Parse YAML plan into ordered steps
2. Template substitution: `{{task.source_url}}`, `{{prev.result.text}}`, `{{prev.result.xxx}}`
3. Execute each step via `_call_tool_by_name(tool_name, **resolved_params)`
4. If all steps succeed: `_build_final_result()` aggregates evidence + candidates
5. Auto-generates `PageEvidence` from text-producing steps when candidates exist but no explicit evidence

**WeChat plan** (3 deterministic steps):
```yaml
plan:
  - tool: triage_link           # URL → site_type
    params: {url: "{{task.source_url}}"}
  - tool: fetch_wechat_article  # ReadGZH → OCR images → extract emails/URLs
    params: {url: "{{task.source_url}}"}
  - tool: extract_jd_candidates # Deterministic keyword extraction
    params: {page_text: "{{prev.result.text}}", url: "{{task.source_url}}"}
```

**On failure**: Returns `SnapshotExecutionResult(needs_supervisor_fallback=True)` with `snapshot_context`.

**On 0 candidates**: If `needs_manual_review` flag detected in trajectory → skip Supervisor (blocked article). Otherwise → Supervisor handoff.

### PATH C — Supervisor Agent (slow: 2-7 min)

**When**: No strategy match, or adapter/snapshot failure fallback.

**Flow**: LLM-in-the-loop DeepAgent.
1. `build_discovery_supervisor_agent(settings, model, snapshot_context)`
2. 9 tools: `triage_link`, `run_web_navigation` (→ WebNavigationAgent subagent), `parse_wechat_article`, `run_ocr`, `extract_jd_candidates`, `standardize_from_record_fields`, `verify_evidence`, `package_candidates`, `finish_with_manual_review`
3. `snapshot_context` injected into system prompt on takeover
4. L1 Loop Prevention: prompt-level rules (max 12 calls, no retries, fallback at 6)
5. Structured output via `_DiscoveryRunResultPydantic`

**WebNavigationAgent subagent**: Separate DeepAgent with 9 navigation tools (`open_url`, `open_rendered_url`, `extract_rendered_job_evidence`, `read_dom`, `extract_links`, `click_link`, `get_visible_text`, `screenshot`, `go_back`).

## Key Concepts

### StrategyRecord (MySQL `job_discovery_strategies` table)

| Field | Purpose |
|-------|---------|
| `url_pattern` | fnmatch glob (e.g. `mp.weixin.qq.com/s/*`, `*talent.alibaba.com/*`) |
| `adapter` | Dotted class path for PATH A (e.g. `...adapters.alibaba_spa.AlibabaSPAAdapter`) |
| `plan_yaml` | YAML plan for PATH B SnapshotExecutor |
| `priority` | Higher = checked first |
| `state` | `active` → `degraded` → `unavailable` (3 consecutive failures via atomic SQL) |
| `degradation_threshold` | Failures before degrading (default 3) |
| `recovery_threshold` | Successes before recovering (default 2) |

### TrajectoryBuffer

In-memory recorder shared by all three paths. Every tool call records `{tool, status, params, result, error, error_type, timestamp}`. On failure: `to_snapshot_context()` builds `{source, strategy_id, completed_steps[], failed_step{}}`.

### LoopGuardian (L1 + L2)

- **L1** (prompt): `supervisor_base.txt` "Loop Prevention" section — tells LLM not to retry, max 12 calls, fallback at 6
- **L2** (standalone): `_loop_guardian_wrap(tool_fn, tool_name)` — detects identical tool+params ≥3× consecutively, blocks execution. Available for programmatic use but NOT auto-applied to Supervisor tools (LangChain `StructuredTool` calling convention is incompatible with Python function wrappers)

### Page Cache (`_page_cache` + `_cached_fetch`)

Module-level dict `{url: (content, title, error)}`. WeChat URLs route through ReadGZH; others use `requests.get`. Subsequent `open_url`, `read_dom`, `extract_links` calls reuse cached result — no duplicate HTTP/ReadGZH calls per task.

### WeChat Raw HTML Cache (`_wechat_raw_html_cache`)

Separate `{url: raw_html}` cache populated by `_fetch_wechat_via_readgzh`. Used by `fetch_wechat_article` to extract `<img>` URLs for OCR without a second HTTP round-trip. Cleared by `_reset_nav_state` and `_reset_web_nav_state`.

### Email Privacy

`fetch_wechat_article` extracts email addresses as structured `application_emails` metadata. The deterministic `extract_jd_candidates` (regex-based, no LLM) processes them safely — no raw emails ever enter an LLM prompt.

## Complete Tool Chain

| Tool | Source | Type | Signature |
|------|--------|------|-----------|
| `triage_link` | `tools/link_triage.py` | deterministic | `(url) → dict` |
| `fetch_wechat_article` | `deepagents_runner.py` | deterministic + ReadGZH + OCR | `(url) → dict` |
| `parse_wechat_article` | `tools/wechat_article_parser.py` | deterministic | `(html, url) → dict` |
| `run_ocr` | `tools/ocr_pipeline.py` | PaddleOCR | `(image_base64) → dict` |
| `extract_jd_candidates` | `tools/jd_extraction.py` | deterministic (regex) | `(page_text, url) → JSON str` |
| `verify_evidence` | `tools/evidence_verifier.py` | deterministic | `(candidates_json, evidence_json) → JSON str` |
| `package_candidates` | `tools/candidate_packager.py` | deterministic | `(candidates_json, hash, source_key) → JSON str` |
| `standardize_from_record_fields` | `deepagents_runner.py` | deterministic fallback | `(fields_json, evidence_json, url) → JSON str` |
| `finish_with_manual_review` | `deepagents_runner.py` | signal | `(reason) → dict` |
| `run_web_navigation` | `deepagents_runner.py` | LLM DeepAgent | `(start_url, settings) → dict` |

## Testing

### Unit tests
```powershell
# All job_discovery unit tests
.\.venv\Scripts\python.exe -m pytest tests/unit/ -k job_discovery -v

# LoopGuardian (16 scenarios, no LLM/network)
.\.venv\Scripts\python.exe tests/unit/test_loop_guardian.py

# Evidence verifier
.\.venv\Scripts\python.exe -m pytest tests/unit/ -k evidence_verifier -v
```

### Live smoke tests (require DEEPSEEK_API_KEY + TENCENT_DOCS_TOKEN + READGZH_API_KEY)
```powershell
# Full 4-URL strategy router smoke test (~120s)
.\.venv\Scripts\python.exe -u tests/manual/test_strategy_router_live_smoke.py

# Adapter failure → Supervisor takeover verification
.\.venv\Scripts\python.exe -u tests/manual/test_adapter_failure_takeover.py
```

### Key env vars for live tests
- `DEEPSEEK_API_KEY` — LLM API key
- `TENCENT_DOCS_TOKEN` — Tencent Smartsheet API token
- `READGZH_API_KEY` — ReadGZH proxy API key (bypasses WeChat fingerprinting)

## Key Conventions

### When adding a new adapter
1. Subclass `DomainAdapter` in `adapters/`
2. Implement `execute(task, strategy, trajectory) → DiscoveryRunResult`
3. Call `trajectory.record_step()` for each significant operation
4. On failure: raise exception (don't return failed result) — lets Supervisor take over
5. Register the dotted class path in the strategy's `adapter` field

### When adding a new snapshot tool
1. Define the function in `deepagents_runner.py`
2. Import + register in `snapshot_executor.py` `_ensure_tool_registry()`
3. Document in the YAML plan

### When modifying Supervisor prompts
1. Edit `prompts/supervisor_base.txt` for always-included content
2. Edit `prompts/supervisor_clean_start.txt` for clean-start instructions
3. Edit `prompts/supervisor_snapshot_fallback.txt` for takeover instructions
4. Template variables in snapshot_fallback: `{source}`, `{strategy_id}`,
   `{failed_step_count}`, `{completed_steps}`, `{failed_step_tool}`,
   `{failed_step_params}`, `{failed_step_error}`

### Evidence format
- `evidence_type`: `"job_detail_json"` (XHR), `"rendered_page"` (DOM fallback), `"snapshot_auto"` (auto-generated)
- `text_excerpt`: truncated to 1500 chars (XHR) or 5000 chars (DOM)
- Must include `content_hash` (SHA-256) for dedup
- `verify_evidence` requires `evidence_refs` on each candidate

### Idempotency
- Candidate key: SHA-256 of normalized `company + title + location + apply_url + evidence_hash`
- Similarity group: SHA-256 of `company + title + recruitment_type + source_family`

## State Machine: StrategyRecord

```
active ──(3 consecutive failures)──→ degraded ──(3 more)──→ unavailable
  ↑                                      │
  └────(2 consecutive successes)─────────┘
```

Atomic SQL updates via `strategy_store.increment_failure()` / `increment_success()`. Only `active` and `degraded` strategies are returned by `get_active_strategies()`.

## State Machine: JobDiscoveryTask

```
queued → running → succeeded | partial_success | needs_manual_review | failed | cancelled
```

Worker claims with lease timeout. Expired leases allow re-claim. `partial_success` when candidates found but budget exhausted.
