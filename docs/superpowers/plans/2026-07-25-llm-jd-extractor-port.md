# Port A's Per-Page LLM JD-Extractor into B's PATH C (Legacy Supervisor)

> Confirmed mapping (user, 2026-07-25): the skill `skill/job-discovery/` (A) corresponds to
> `backend/app/services/job_discovery` **PATH C** (the Legacy Supervisor). So porting A's
> patterns into PATH C is the faithful port. PEV PATH A/B stay deterministic (the PEV master
> plan `plan.md` line 37 mandates "Executor 和 Verifier 不调用 LLM"; line 35 freezes
> `tools/jd_extraction.py`). This plan does NOT touch `plan.md`.

## Goal

Port the single highest-value pattern proven in A — **per-page LLM JD-body extraction + verify-retry** — into B's PATH C, behind a new flag (default off). Then run B's 10-URL eval before/after and compare against A, to measure whether B's title-only gap closes.

## Scope

**PORT (the quality win — richer JD bodies):**
- Per-page LLM jd-body extraction: one structured-output LLM call per `page_text` evidence page.
- Verify-retry: 1 retry on parse/validation failure.
- DeepSeek lenient recipe (proven in A's v1.x): `max_tokens=8192` + lenient JSON-array parser (recover from truncation) + 1 retry. Mirrors `skill-eval-a-b-resolution` "B" resolution.

**LANDING (PATH C only):**
- `deepagents_runner.py::_extract_and_verify_candidates_from_evidence`, at the title-only fallback fork (`is_page_text and not has_detail`, ~L2966-2969).
- Call the LLM extractor **first** when the flag is on; if it returns candidates with non-empty `responsibilities`/`requirements`, extend `all_candidates` and **skip** the title-only fallback. If it returns empty or raises, fall through to the existing `_extract_title_only_candidates` (current behavior preserved bit-for-bit).
- `text`, `page_url`, `ref` are already in scope at that fork — exactly what the extractor needs.

**DO NOT PORT (out of scope):**
- parallel-fetch — browse-layer speed; B paginates via the Web-Nav agent + `_MAX_LIST_PAGES`; 90% time cut is infeasible per `/goal`.
- load-more fix — browse-layer; B's pagination is a separate mechanism; separate probe if needed.
- canonical dedup — **already in B** (`deduplication/canonical_job_deduplicator.py`).

**DO NOT TOUCH (gray-window + PEV-constraint safety):**
- `result_contract.enforce_result_invariants` (off-limits per memory + `plan.md`).
- supervisor `subagents=[...]` list, `prompts/supervisor_base.txt` (legacy PATH C blast radius).
- `tools/jd_extraction.py` (frozen), `tools/evidence_verifier.py` (frozen).
- `post_crawl_pipeline.py` and any PATH A/B code (PEV deterministic Executor — "no LLM").
- any existing flag default.

## Safety / constraints respected

- New module + new flag + one injection branch. **Default OFF = byte-identical to today.** Gray window undisturbed; `enforce_result_invariants` legacy semantics unchanged.
- LLM-extracted candidates carry `evidence_refs=[ref]` + `normalization_warnings=["LLM-extracted from list-page text"]`; downstream `_verify_evidence` → `deduplicate_candidates` → subsumption (~L2981-3028) accepts them with no further plumbing.
- Security gates preserved: the extractor is pure text-in / candidates-out — no browsing, no tool-calling, no bypass. Blocked pages never reach it (handled upstream in `run_web_navigation`).
- Three-layer rule: new module is a **service-layer function** (no HTTP, no SQL).

## Files

**NEW:**
- `backend/app/services/job_discovery/extraction/__init__.py`
- `backend/app/services/job_discovery/extraction/llm_jd_extractor.py` — `extract_jd_candidates_llm(page_text, url, *, settings, model=None) -> list[NormalizedJobCandidate]`; structured-output LLM + lenient JSON-array parser + verify-retry (1 round). Reuses `_build_job_discovery_llm(settings)` with `max_tokens=8192`.
- `backend/app/services/job_discovery/prompts/llm_jd_extractor.txt` — extraction prompt (port of skill's `jd_extractor` system prompt: job boundaries, resp/req split, original language, **no invention**).
- `tests/unit/job_discovery/test_llm_jd_extractor.py` — mocked LLM: returns body candidates; retry on bad JSON; empty on failure; no-invention guard.

**MODIFY:**
- `backend/app/config.py` — add `job_discovery_llm_extraction_enabled: bool = False` (env `JOB_DISCOVERY_LLM_EXTRACTION_ENABLED`), next to the other `job_discovery_*` flags (~L88-113).
- `backend/app/services/job_discovery/deepagents_runner.py` — thread optional `settings=None, model=None` into `_extract_and_verify_candidates_from_evidence` (defaults = current behavior); at the title-only fork, when `settings.job_discovery_llm_extraction_enabled`, call `extract_jd_candidates_llm` first. Caller `run_web_navigation` (~L546) passes its `settings`/`model` through.
- `tests/integration/job_discovery/test_supervisor_ten_url_eval.py` — add `count_with_body` / `count_title_only` to the per-URL record + print line (mirror `test_supervisor_ten_url_quality.py::_has_body`), so the comparison reports richness.

## Test plan

1. **Unit** (`test_llm_jd_extractor.py`, mocked LLM): body extraction; verify-retry on bad JSON; empty-on-failure; no-invention (LLM given title-only text returns no fabricated body).
2. **Integration** (`_extract_and_verify_candidates_from_evidence` with a `page_text` evidence page that has no regex-matchable JD body but LLM-extractable body): flag ON → body candidates; flag OFF → title-only (current).
3. **Regression**: `tests/unit/job_discovery/` + `tests/integration/test_job_discovery_deepagents.py` pass with flag OFF (no behavior change). `ruff check` clean.

## Eval / comparison methodology

Run B's `tests/integration/job_discovery/test_supervisor_ten_url_eval.py` (same 10 URLs as A; in-memory SQLite; no Docker/Redis/MinIO) **twice, fresh**:
- **B-before**: flag OFF — delete `tests/manual/_ten_url_eval_<slug>.json` first.
- **B-after**: `JOB_DISCOVERY_LLM_EXTRACTION_ENABLED=1`.

Both runs env: `RUN_TEN_URL_EVAL=1` + `JOB_DISCOVERY_PEV_ENABLED=1` + `DEEPSEEK_API_KEY`.

**Metrics per URL** (apples-to-apples with A's table): `raw / uniq / dups / real / recall / elapsed / status / count_with_body` (new).

**For A**: use the user's table for count/elapsed/status; compute A's `count_with_body` post-hoc from existing `tests/manual/_skill_ten_url_<slug>_merged.json` (same `_has_body` definition). Re-run A fresh only for any URL whose merged file is missing/stale.

**Comparison table**: `A | B-before | B-after`, per URL, on count + count_with_body + elapsed. **Key question:** does B-after's `count_with_body` rise toward A's (i.e. does the port close the title-only gap)?

**Concurrency note**: per-page LLM calls are sequential in v1 (correctness first). If xiaomi's ~16 pages make extraction too slow, parallelize with `ThreadPoolExecutor(max_workers=4)` over the pages needing LLM extraction — measure and decide. (This is a time concern; the port's value is quality/richness.)

## Out of scope / deferred

- parallel-fetch, load-more (browse-layer; separate probes).
- Wiring LLM extraction into PATH A/B (`post_crawl_pipeline`) — would revisit the "Executor no LLM" PEV constraint; defer.
- Per-URL adapters (forbidden by `/goal`).

## Deliverable

1. Committed: new `extraction/llm_jd_extractor.py` + `prompts/llm_jd_extractor.txt` + flag + injection + tests.
2. A comparison report (table) `A vs B-before vs B-after`, with a verdict on whether the port closed B's title-only gap.
