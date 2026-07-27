---
name: job-discovery
description: >
  Automated job posting discovery from Tencent Smartsheet career URLs. Reads source URLs from
  smartsheet, browses career sites via Playwright to capture page text, then uses LLM extraction
  to produce structured job descriptions. Use when the user wants to discover, collect, or extract
  job postings from career sites, crawl recruitment pages, sync job data from Tencent Docs, or
  batch-extract position details from any list of career URLs. Also use when the user mentions
  "抓取招聘信息", "提取岗位JD", "批量爬职位", "同步招聘数据", or similar Chinese phrases.
compatibility: requires Python 3.10+, playwright (pip), tencent-docs skill (for smartsheet access)
---

# Job Discovery Agent

Extract structured job descriptions from career site URLs at scale. Designed as a
pi-agent skill - the LLM (you) orchestrates, helper scripts handle the mechanical work
(browser rendering, caching, validation).

**This file is a dispatch hub.** It is intentionally short. Load the reference file
that matches your task from the [Progressive disclosure](#progressive-disclosure-how-deep-to-go)
table or the [References](#references) list - do NOT read them all up front.

## Why this skill exists

Career sites come in dozens of shapes - Moka, Feishu, zhiye.com, custom React SPAs,
WeChat articles. Writing deterministic scrapers for each is brittle and high-maintenance.
Instead, this skill uses:

1. **Playwright** to render JS-heavy pages into plain, readable text (once per URL)
2. **Your LLM reasoning** to classify sites and extract structured JDs from that text
3. **Content-addressed caching** so no page is rendered or extracted twice

The result is a pipeline that adapts to new site types without new code - only new
instructions in `references/site-catalog.md`.

## Quick start

```bash
# 1. Ensure dependencies
pip install playwright && playwright install chromium

# 2. Single URL (L2) - the common case. Follow references/single-url-extraction.md:
#    Planner -> Executor -> Verifier. Browse with parallel-fetch first.
python scripts/browse.py "https://xiaopeng.jobs.feishu.cn/s/Pycfxid-fok" \
  --mode parallel-fetch --max-pages 20 --out output/evidence

# 3. Read the workflow doc, then extract + validate (you - the LLM - do this step)
#    see references/single-url-extraction.md and references/schema.md

# 4. Validate the extracted candidates
python scripts/validate.py output/candidates/<hash>.json

# 5. Batch (L3) - read URLs from Tencent Smartsheet, dedup across runs:
#    see references/smartsheet-sources.md and references/incremental-persistence.md
```

## Full workflow

There are six phases. The single-URL path (L2) covers Phases 2-4 with `browse.py` +
LLM extract + `validate.py`; the batch path (L3) adds Phases 1, 5, 6 with `state.json`
incremental logic.

### Phase 1 - INGEST: Collect URLs (L3 only)

Read career URLs from the Tencent Smartsheets. Sheet IDs, field mappings, and the
ingest commands live in **`references/smartsheet-sources.md`**.

### Phase 2 - CLASSIFY: Determine site type and extraction strategy

For each URL, do a lightweight probe before committing to a full browser render:

```bash
# Fetch just the first 4KB of HTML
curl -sL --max-time 10 "<url>" | head -c 4096 > /tmp/preview.txt
```

Read `/tmp/preview.txt` and classify:

| Signal | Likely site type | Recommended approach |
|--------|-----------------|---------------------|
| `mp.weixin.qq.com` in URL | WeChat article | `browse.py --mode detail` -> check text_length -> if image-heavy, OCR -> channel triage -> recursive browse (see `wechat-image-handling.md`, 6-level pipeline) |
| Multi-page listing (mokahr / bytedance / Mioffice / any paginated) | URL-keyed SPA | `browse.py --mode parallel-fetch` (v1.6 default; auto-falls back to `click` for load-more sites) |
| `jobs.feishu.cn` in URL | Feishu/Lark | `parallel-fetch`, retry `search-interact` if thin |
| `zhiye.com` in URL | zhiye.com platform | `browse.py --mode search-interact` (search box usually available) |
| `<script>` with `__NEXT_DATA__` | Next.js/Nuxt SPA | `browse.py --mode search` or `list` |
| Login wall / 403 / captcha | Blocked | Skip, mark as `needs_manual_review` |
| Plain HTML with listings in first 4KB | Static site | `curl` full page OR `browse.py --mode list` |

Record your classification decision and proceed to Phase 3. **Why classify first?**
`browse.py` takes 15-30 seconds per URL; skipping blocked URLs and routing WeChat
through ReadGZH saves significant time at scale.

### Phase 3 - EXTRACT: Render page text

Run `scripts/browse.py` with the appropriate mode. The full mode reference (what each
mode does, search-strategy options, output format) lives in **`references/browse-modes.md`**.
For the single-URL *workflow* (planner -> executor -> verifier), read
**`references/single-url-extraction.md`**.

Condensed mode list:

| Mode | One-liner |
|------|-----------|
| `parallel-fetch` | v1.6 default for URL-keyed paginated sites; pre-computes page URLs, fetches concurrently via thread pool; auto-falls back to `click` |
| `search-interact` | Moka/zhiye/Feishu: search-filter then click each card for full JDs |
| `list` / `detail` / `interact` / `search` / `click` | See `references/browse-modes.md` |

### Phase 4 - STRUCTURE: LLM extracts normalized JDs

This is your core contribution as the LLM orchestrator. Read the page text and
extract every job posting into the `NormalizedJobCandidate` schema.

1. Read `output/evidence/<content_hash>.txt`.
2. Consult **`references/extraction-guide.md`** for site-specific tips and
   **`references/schema.md`** for the full schema.
3. Extract all positions into a JSON array; save to `output/candidates/<hash>.json`.
4. Validate: `python scripts/validate.py output/candidates/<hash>.json --package --verify`.

**`confidence` calibration by `evidence_type`** (this is what schema.md Phase 4 refers to):
`browsed_detail_page` -> 0.88-0.95; `ocr_full_jd_text` -> 0.60-0.75;
`ocr_poster_keyword` -> 0.40-0.55; a poster with only "AI应用" and no JD body stays
at 0.45 max regardless of OCR accuracy.

### Phase 5 - NORMALIZE & DEDUPLICATE (L3)

**L1 (lightweight):** `python scripts/normalize.py --title "..."` for a single title
or `core_hash`. **L3 (full batch):**
`python scripts/deduplicate.py output/candidates/*.json --out output/merged_final.json`
(normalize, semantic-dedup, idempotency keys, quality checks, merge). Full details in
**`references/incremental-persistence.md`**.

### Phase 6 - PERSIST: Collect and report (L3)

Review `merged_final.json` and update `state.json`. Full details in
**`references/incremental-persistence.md`**.

## Error handling guide

| Situation | Action |
|-----------|--------|
| URL returns 403 / login wall | Skip, record in `output/errors.jsonl` |
| Page renders but has no job listings | Mark as `empty`, record screenshot path |
| Page has >100 positions (estimate) | Process first 3 pages only, note in summary |
| LLM extraction produces invalid JSON | Re-read the text and try again with stricter prompt |
| Playwright times out (30s+) | Retry once with `--wait 5000`, then skip |
| WeChat article has images (any) | ALWAYS attempt OCR - see `references/wechat-image-handling.md` for the full decision tree (6 levels) |
| WeChat article: OCR done but only keywords, no JD body | Classify channel -> if URL found, recursively browse career site (Level 6) |
| Recursive browse of career URL returns only navigation (SPA) | Mark `needs_deep_crawl`, save OCR JDs, append to errors.jsonl (Level 6 Step 4) |
| Recursive browse succeeds with full JDs | Replace OCR extraction with browsed JDs, confidence 0.85+ (Level 6 Step 2B) |
| Search mode: no search box found | Auto-fallback to `list` mode (with `--fallback full`) |
| Search mode: keyword returns 0 results | Try next keyword (first_match) or fallback to full list |
| Search mode: post-search count == pre-count | Warning logged - possible client-side fake filter; results may be incomplete |

## References

Load these as needed during processing:

- `references/single-url-extraction.md` - **L2 workflow**: Planner -> Executor -> Verifier for one career URL (parallel-fetch first)
- `references/browse-modes.md` - Full `browse.py` mode reference (list/detail/interact/search/search-interact/parallel-fetch/click)
- `references/site-catalog.md` - Known career site patterns, selectors, and quirks
- `references/extraction-guide.md` - Detailed JD extraction rules with examples
- `references/schema.md` - Full NormalizedJobCandidate JSON schema
- `references/wechat-image-handling.md` - WeChat article full pipeline: OCR strategy (5 levels) + channel triage & recursive browsing (Level 6)
- `references/smartsheet-sources.md` - **L3**: Sheet A/B IDs, field mappings, Phase 1 INGEST
- `references/incremental-persistence.md` - **L3**: state.json, three-tier change detection, Phase 5/6 dedup/persist, resumability

## Progressive disclosure: how deep to go

This skill is designed with usage levels. Start shallow; go deeper only when needed.

| Level | What you load | When to use |
|-------|--------------|-------------|
| **L1: Quick normalize** | `scripts/normalize.py --title "..."` | Comparing two job titles, computing a `core_hash` for identity |
| **L2: Single URL** | `SKILL.md` + `references/single-url-extraction.md` (+ `browse-modes.md`, `schema.md`, `extraction-guide.md` as needed) | Processing one career page end-to-end |
| **L2w: WeChat article** | L2 + `references/wechat-image-handling.md` (6-level pipeline) | Processing a WeChat article with channel triage + recursive browsing |
| **L3: Batch pipeline** | L2 + `references/smartsheet-sources.md` + `references/incremental-persistence.md` + `state.py` + `deduplicate.py` | Processing dozens of URLs from Smartsheet |

Each script works standalone at its level. `deduplicate.py` contains its own embedded
normalizer so L3 doesn't require running L1 first - but L1 is available as a lighter
tool when you just need to normalize one title for comparison.

## Scripts

- `scripts/normalize.py` - **L1**: Standalone title/company/JD-body normalization + `core_hash`
- `scripts/browse.py` - **L2**: Playwright-based page renderer and text extractor
- `scripts/validate.py` - Schema validator + packaging keys + evidence quality checks
- `scripts/deduplicate.py` - **L3**: Normalize, semantic-dedup, and merge candidates across batches
- `scripts/state.py` - **L3**: Incremental state manager: init, check (skip/extract), mark, diff
- `scripts/ocr_image.py` - Multi-backend OCR (vision -> PaddleOCR -> Tesseract) for image extraction
