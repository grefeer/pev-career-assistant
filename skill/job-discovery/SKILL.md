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
pi-agent skill — the LLM (you) orchestrates, helper scripts handle the mechanical work
(browser rendering, caching, validation).

## Source SmartSheets (default)

This skill reads career URLs from two Tencent Smartsheet files. These are the
**canonical data sources** — always start by scanning them for new/updated records.

### Sheet A: 27届提前批秋招信息汇总（持续更新）

- **URL**: `https://docs.qq.com/smartsheet/DZkdPVGtGb1ZvaG5R?tab=t00i2h`
- **File ID**: `fGOTkFoVohnQ`
- **Title**: 27届提前批秋招信息汇总（持续更新）

| Sheet ID | Name | Visible | Records | Used for |
|----------|------|---------|---------|----------|
| `t00i2h` | 27届内推信息【重要】 | ✅ | ~780 | **Primary**: 内推 links + codes |
| `tbVCvT` | 27届招聘推文校招信息 | ❌ | ~639 | **Secondary**: 招聘推文 links |

**t00i2h field mapping** (fields relevant for extraction):

| Field | Type | Role in extraction |
|-------|------|--------------------|
| `企业名称` | text | → `company_name` (primary source) |
| `内推链接` | url | → `apply_url` (entry point for browsing) |
| `整体文案` | text | → prior metadata for JD extraction context |
| `内推码(区分大小写)` | text | → `referral_code` |
| `招聘类型` | select | → `recruitment_types` hint |
| `行业类型` | select | → `industries` hint |
| `工作地点` | select | → `locations` hint |
| `答疑链接` | url | → supplementary link (Q&A) |
| `更新时间` | dateTime | → **change detection key** (millisecond timestamp) |

**tbVCvT field mapping**:

| Field | Type | Role in extraction |
|-------|------|--------------------|
| `企业名称` | text | → `company_name` |
| `招聘链接` | url | → `apply_url` |
| `整体文案` | text | → prior metadata |
| `内推码` | text | → `referral_code` |
| `更新日期` | dateTime | → change detection key |

### Sheet B: 27届校招秋招实习内推合集（欢迎大家分享！）

- **URL**: `https://docs.qq.com/smartsheet/DY3pHYkNvb0ZRSHdi?tab=BB08J2`
- **File ID**: `czGbCooFQHwb`
- **Title**: 27届校招秋招实习内推合集（欢迎大家分享！）

| Sheet ID | Name | Records |
|----------|------|---------|
| `tZW9Ng` | 每日更新 | ~1079 |
| `BB08J2` | 实习内推汇总 | — |

**tZW9Ng field mapping**:

| Field | Type | Role in extraction |
|-------|------|--------------------|
| `公司名称` | text | → `company_name` |
| `投递链接` | url | → `apply_url` (entry point) |
| `招聘岗位` | text | → title hint (vague in this sheet) |
| `整体文案` | text | → prior metadata |
| `工作地点` | text | → location hint |
| `招聘类型` | select | → `recruitment_types` hint |
| `截止日期` | text | → `deadline_text` |
| `更新时间` | dateTime | → change detection key |

> **Note:** `BB08J2` (实习内推汇总) is listed as user reference but `tZW9Ng` (每日更新)
> is the primary daily-update sheet. The user mentioned `tab=BB08J2` in the URL
> but `BB08J2` maps to "实习内推汇总" — `tZW9Ng` is the one named "每日更新". Use
> both as needed.

## Incremental persistence logic (核心存量逻辑)

### The question: re-extract or skip?

Each SmartSheet record has a URL and an `更新时间` (last-update timestamp). When
you re-scan the sheets, you face three scenarios:

| Scenario | Detection | Action |
|----------|-----------|--------|
| New URL (never seen before) | URL not found in `output/state.json` | Full pipeline: browse → LLM extract → save |
| URL already processed, update_time **unchanged** | URL + update_time match in `output/state.json` | **Skip entirely** — nothing changed |
| URL already processed, update_time **changed** | URL matches but update_time differs | Re-browse, but check content_hash first |

### Save units (what gets persisted to disk)

```
output/
├── state.json                          ← Master index (URL → hash → candidates)
├── evidence/
│   ├── sha256_<content_hash>.txt       ← Raw page text (immutable, content-addressed)
│   └── sha256_<content_hash>.png       ← Screenshot
├── candidates/
│   └── sha256_<content_hash>.json      ← Extracted candidates (immutable, content-addressed)
├── merged_final.json                   ← Latest merged+dropped output (overwritten each run)
└── errors.jsonl                        ← URLs that failed (append-only log)
```

**Key insight**: Candidates are keyed by **page content_hash**, not by URL. If two
different SmartSheet records point to the same career page (same content), they
produce the same content_hash and share one `candidates/*.json` file.

### state.json format

```json
{
  "source_sheets": {
    "fGOTkFoVohnQ": {
      "title": "27届提前批秋招信息汇总",
      "sheets": ["t00i2h", "tbVCvT"],
      "last_scanned": "2026-07-19T12:00:00"
    },
    "czGbCooFQHwb": {
      "title": "27届校招秋招实习内推合集",
      "sheets": ["tZW9Ng"],
      "last_scanned": "2026-07-19T12:00:00"
    }
  },
  "processed": {
    "<content_hash>": {
      "url": "https://xiaopeng.jobs.feishu.cn/s/Pycfxid-fok",
      "source_file_id": "fGOTkFoVohnQ",
      "source_sheet_id": "t00i2h",
      "record_ids": ["rec_abc", "rec_def"],
      "last_update_time": "1720000000000",
      "company": "小鹏集团",
      "extracted_at": "2026-07-19T12:05:00",
      "candidates_count": 9
    }
  }
}
```

### Full incremental workflow

```
Phase 1: Scan smartsheets for changes
  For each sheet (t00i2h, tbVCvT, tZW9Ng):
    smartsheet.list_records → get all {record_id, url, company, update_time, ...}
    Cross-reference with state.json
    Build three lists:
      - SKIP:     URL + update_time unchanged
      - RENDER:   URL changed OR update_time changed
      - DEAD:     Record deleted from sheet (keep its candidates, just note it)

Phase 2: Browse (for RENDER list only)
  python scripts/browse.py <url> --mode list --out output/evidence
  → If content_hash already in state.json → evidence unchanged, SKIP LLM extraction
  → If content_hash is NEW → Go to Phase 3

Phase 3: LLM Extract (for new content_hashes only)
  Read evidence/<hash>.txt
  Extract JDs per schema
  Save to candidates/<hash>.json
  validate.py --package --verify

Phase 4: Merge & Deduplicate (every run)
  python scripts/deduplicate.py output/candidates/*.json --out output/merged_final.json
  → Takes ALL candidates (old + new), normalizes, dedups, merges
  → output/merged_final.json is the COMPLETE cumulative history — NOT a current
    snapshot. It includes deleted/offline jobs and old JD versions.
    For a current-only view, filter by latest content_hash per active SmartSheet record.

> **Key distinction**:
> - `merged_final.json` = historical audit trail (cumulative, prefers old content)
> - Current snapshot = filter merged_final to candidates whose evidence_refs
>   include the latest content_hash from active records, deduplicated by
>   `job_identity_key`.

Phase 5: Update state.json
  Write all processed entries back
```

### Q&A: Your three questions, answered

**Q1: URL + 更新时间 怎么判断当前url是新的？是需要更新的？**

实际是**三级防线**，不是单次比较：

```
SmartSheet记录.更新时间 (毫秒时间戳，如 "1720000000000")
         │
         ▼
┌─ 第一级：state.json 里查这个 URL ─────────────────────────┐
│  遍历 state.json['processed'] 找到 url 匹配的条目            │
│  ├── 找不到 → 新 URL → 【需要提取】                          │
│  └── 找到了 → 比较 last_update_time                          │
│       ├── 相同 → 记录没被编辑过 → 【跳过】（exit 0）          │
│       └── 不同 → 记录被编辑过 → 进入第二级                    │
└────────────────────────────────────────────────────────────┘
         │ (update_time 不同)
         ▼
┌─ 第二级：browse.py 渲染页面 → 计算 content_hash ───────────┐
│  ├── content_hash 命中缓存（页面没变）→ 【跳过 LLM 提取】     │
│  └── content_hash 是新值（页面真变了）→ 进入第三级            │
└────────────────────────────────────────────────────────────┘
         │ (content_hash 是新的)
         ▼
┌─ 第三级：LLM 全量提取 → validate → dedup 合并 ─────────────┐
│  新 candidates/<new_hash>.json + 旧 candidates/<old_hash>.json │
│  → deduplicate.py 按 canonical identity 合并                  │
└────────────────────────────────────────────────────────────┘
```

**核心理解**: `更新时间` 是 Smartsheet 记录编辑时间（可能只是改了个错别字），不是招聘页面更新时间。它只是**触发检查**的信号。真正判断页面是否变了的是 `content_hash`（页面文本的 SHA-256）。

**举例**:
- 有人在 SmartSheet 里给公司A记录加了一条"备注"→ `更新时间` 变了
- `state.py check` 返回 "update_time changed" → 触发第二级
- `browse.py` 渲染页面 → 页面内容没变 → `content_hash` 命中缓存 → **浏览器都没启动就跳过了**
- 结果：零消耗（一个 shell 调用而已）

**Q2: 公司A原来10个岗位，现在新增到12个，旧的10个怎么办？要全量更新吗？**

**A: 全量重提取 + dedup 合并，旧的不丢失也不重复。** 一般情况下岗位 JD 不会变，这个假设是对的。但当前架构无法做"增量提取"——因为 `browse.py` 输出的是一个完整的页面文本 blob，LLM 无法只提取"新增的2个"而不看全部12个。

实际发生的过程：

```
旧 run:   candidates/hash_old.json  ─── 10 个岗位
                │
新 run:   页面新增2个岗位 → content_hash 变了 → browse.py → 新页面文本
                │
          LLM 从新文本中提取出 12 个岗位 → candidates/hash_new.json
                │
          deduplicate.py(hash_old.json + hash_new.json):
            ├── 岗位1-10: canonical identity 相同 → _merge()
            │     • evidence_refs 追加新 hash（证明两次 run 都看到了）
            │     • 其他字段：旧值保留，新值仅在旧值为空时补充
            │     • 不会产生 20 条，只输出 1 条
            └── 岗位11-12: 新 identity → 直接加入
                │
          最终: 12 条（不是 22 条）
```

**关键**: `_merge()` 是**保守合并**——只追加 evidence_refs，不覆盖已有字段。所以如果岗位1的 JD 在新页面里变了（虽然你说一般不会），旧值保留，新 evidence_ref 被追加用于审计追溯。

**能否优化（不做全量提取）？** 可以作为未来改进：在 LLM prompt 里传入上一轮的10个岗位 title 列表，指示"只提取不在这10个里的新岗位"。但这需要 LLM 有精确的匹配能力，且风险是漏掉 JD 内容微调后的岗位。当前的保守策略（全量 + dedup）更安全。

**Q3: 本 skill 的保存逻辑是什么？**

**A: 内容寻址 + 累积追加。** 详见上方的「Save units」和「state.json format」章节。核心三条：

1. **不可变存储**: evidence 和 candidates 文件以 content_hash 命名，一旦写入永远不变。同一个 hash 不会产生两份文件。
2. **累积不覆盖**: 每次 run 只新增 `candidates/<new_hash>.json`，永不动旧文件。`merged_final.json` 从全部历史 candidates 重新生成。不可能因重新运行而丢数据。
3. **state.json 是唯一可变文件**: 只追加/更新条目，不删除（除非手动清理）。它是下一轮增量对比的基线。

## Why this skill exists

Career sites come in dozens of shapes — Moka, Feishu, zhiye.com, custom React SPAs,
WeChat articles. Writing deterministic scrapers for each is brittle and high-maintenance.
Instead, this skill uses:

1. **Playwright** to render JS-heavy pages into plain, readable text (once per URL)
2. **Your LLM reasoning** to classify sites and extract structured JDs from that text
3. **Content-addressed caching** so no page is rendered or extracted twice

The result is a pipeline that adapts to new site types without new code — only new
instructions in `references/site-catalog.md`.

## Quick start

```bash
# 1. Ensure dependencies
pip install playwright && playwright install chromium

# 2. Read URLs from Tencent Smartsheet (via tencent-docs skill)
#    Use smartsheet.list_tables then smartsheet.list_records to collect URLs.
#    Save to: output/tasks.jsonl (one JSON object per line)

# 3. Process one URL end-to-end
python scripts/browse.py "https://xiaopeng.jobs.feishu.cn/s/Pycfxid-fok" \
  --mode list --out output/evidence

# 4. Read the output text, classify the site, and extract JDs
#    (you — the LLM — do this step)

# 5. Validate the extracted candidates
python scripts/validate.py output/candidates/<hash>.json
```

## Full workflow

There are five phases. You can run them end-to-end for batch processing, or
step through individually for debugging a single URL.

### Phase 1 — INGEST: Collect URLs

Use the **tencent-docs skill** to read source data:

```bash
# List all tables in the smartsheet file
# (via mcporter: smartsheet.list_tables)

# Read records from the target sheet (e.g., "每日更新")
# (via mcporter: smartsheet.list_records with sheet_id and pagination)

# Save each record as a JSON line in output/tasks.jsonl:
# {"url": "...", "company": "...", "location": "...", ...record_fields}
```

The `record_fields` from Smartsheet are valuable prior metadata — they may contain
company names, locations, referral codes, and deadlines that the career page itself
doesn't display. Always carry them forward into the final candidate.

### Phase 2 — CLASSIFY: Determine site type and extraction strategy

For each URL, do a lightweight probe before committing to a full browser render:

```bash
# Fetch just the first 4KB of HTML
curl -sL --max-time 10 "<url>" | head -c 4096 > /tmp/preview.txt
```

Read `/tmp/preview.txt` and classify:

| Signal | Likely site type | Recommended approach |
|--------|-----------------|---------------------|
| `mp.weixin.qq.com` in URL | WeChat article | browse.py detail → check text_length → if image-heavy, OCR → channel triage → recursive browse career URL (see wechat-image-handling.md, full 6-level pipeline) |
| `mokahr.com` in URL | Moka career site | `browse.py --mode search-interact` (search box usually available, fast filter + card click) |
| `jobs.feishu.cn` in URL | Feishu/Lark career site | `browse.py --mode search-interact` (try search first; fall back to list if no search box) |
| `zhiye.com` in URL | zhiye.com platform | `browse.py --mode search-interact` (search box usually available) |
| `<script>` with `__NEXT_DATA__` or similar | Next.js/Nuxt SPA | `browse.py --mode search` or `--mode list` (search availability varies) |
| Login wall / 403 / captcha | Blocked | Skip, mark as `needs_manual_review` |
| Plain HTML with job listings visible in first 4KB | Static site | `curl` full page OR `browse.py` |

**Search-first optimization**: When the career site has paginated listings with many pages
(e.g. 50+ pages on Moka), use `--mode search-interact` to:
1. Find the search box on the page
2. Enter keywords (default: "AI,人工智能,Agent,大模型,算法") to filter results
3. Click through each filtered card to extract full JDs

This can reduce 50 pages → 1-2 pages (from ~12 minutes to ~30 seconds).

Record your classification decision and proceed accordingly.

**Why classify first?** `browse.py` takes 15-30 seconds per URL (browser launch + render).
Skipping blocked URLs and routing WeChat articles through the faster ReadGZH proxy
saves significant time at scale.

### Phase 3 — EXTRACT: Render page text

Run `scripts/browse.py` with the appropriate mode:

```bash
python scripts/browse.py "<url>" \
  --mode list|detail|interact|search|search-interact \
  --out output/evidence \
  --max-pages 5 \
  --wait 3000
```

**Modes:**
- `list` — For listing/search pages where JDs are visible inline. Waits for render,
  scrolls to load lazy content, detects pagination, and collects text from all visible
  content across up to `--max-pages` pages. **Best for**: static sites, simple career pages.
- `detail` — For single job detail pages. Opens the URL, waits for render, returns
  the full `body.innerText`.
- `interact` — For sites where JDs are hidden behind click interactions (Moka, some SPAs).
  Expands category/section headers, then uses JS-based element discovery to find and
  click job cards one by one. Captures expanded text from each. **Note**: This mode
  has a 2-minute time budget and works best when cards reveal content inline (rather
  than in pure-SPA drawers).
- `search` — **Search-first mode**. Finds a search box on the page, enters keywords,
  then browses only the filtered results (with pagination). Falls back to full `list`
  mode if search is unavailable or produces zero results (with `--fallback full`).
  **Best for**: high-page-count career sites (Moka, zhiye.com, Feishu) where you want
  to narrow down results before extracting.
- `search-interact` — **Optimal mode for Moka/zhiye.com/Feishu**. Combines `search` +
  `interact`: first filters by keyword, then clicks through each filtered card to
  capture expanded full JDs. Falls back to `search` mode if no clickable cards are
  found, and to `list` mode if search itself is unavailable. This is the recommended
  mode for most career platforms.

**Search mode keywords:**

```bash
# Default keywords (broad coverage):
python scripts/browse.py "<url>" --mode search-interact

# Custom keywords for specific roles:
python scripts/browse.py "<url>" --mode search-interact \
  --search-terms "AI,Agent,大模型,LLM,人工智能,深度学习"

# Strategy: first_match (stop at first keyword with results — fastest)
python scripts/browse.py "<url>" --mode search-interact \
  --search-strategy first_match

# Strategy: each (try all keywords, merge & deduplicate — most thorough)
python scripts/browse.py "<url>" --mode search-interact \
  --search-strategy each

# Strategy: broad (use only the first keyword — e.g. a wide term like "AI")
python scripts/browse.py "<url>" --mode search-interact \
  --search-strategy broad \
  --search-terms "AI"

# No fallback — fail explicitly if search unavailable:
python scripts/browse.py "<url>" --mode search --fallback none
```

**When to use which mode:**

| Scenario | Mode | Why |
|----------|------|-----|
| Moka, 50+ page listings | `search-interact` | Search → filter to 1-2 pages → click each card → full JDs |
| Moka, small site (< 10 pages) | `interact` | Skip search overhead, just click-through |
| zhiye.com, many pages | `search-interact` | Search box usually available |
| Feishu, many pages | `search-interact` | Search box sometimes available; auto-fallback if not |
| WeChat article | `detail` → OCR pipeline | No search box; static content |
| Custom SPA without search | `interact` or `list` | No search box available |
| Single detail page | `detail` | One page, no search needed |

**What it does:**
1. Launches headless Chromium
2. Navigates to the URL, waits for `networkidle`
3. Dismisses common consent/GDPR dialogs automatically
4. Scrolls to trigger lazy-loaded content
5. In `list` mode: finds "next page" buttons and paginates (up to `--max-pages`)
6. Saves: `output/evidence/<content_hash>.txt` (full page text) and `.png` (screenshot)
7. Outputs a JSON result to stdout with status, text preview, and content hash

**Output format (stdout):**
```json
{
  "status": "ok",
  "url": "https://...",
  "title": "Page title",
  "content_hash": "sha256_abc123...",
  "text_path": "output/evidence/sha256_abc123.txt",
  "screenshot_path": "output/evidence/sha256_abc123.png",
  "job_count_estimate": 42,
  "pagination": {"current": 1, "total": 3, "has_more": true}
}
```

If `status` is `"blocked"` or `"error"`, skip to the next URL and record the reason.

**Content-addressed caching:** The text file path is derived from `sha256(page_text)`.
If the file already exists, `browse.py` skips the browser and returns the cached path
immediately. This means re-running on the same URL costs nothing.

### Phase 4 — STRUCTURE: LLM extracts normalized JDs

This is your core contribution as the LLM orchestrator. Read the page text and
extract every job posting into the `NormalizedJobCandidate` schema.

1. **Read the page text:**
   ```
   read output/evidence/<content_hash>.txt
   ```

2. **Consult the extraction guide** for site-specific tips:
   ```
   read references/extraction-guide.md
   ```

3. **Extract all positions** in a single pass. For listing pages with multiple
   jobs, extract them all at once into a JSON array. Each element must conform to
   the schema in `references/schema.md`.

4. **Key extraction rules:**
   - A "job posting" is a distinct role with its own title, responsibilities, and
     requirements. Different locations of the same role are separate postings.
   - Use the Smartsheet `record_fields` as prior metadata — if the career page
     doesn't list a company name or location, fall back to the record fields.
   - **Responsibilities vs Requirements**: Responsibilities describe what the
     person will DO. Requirements describe what the person must HAVE (skills,
     degrees, experience). If the source text blends them together, separate them
     in your output but note this in `normalization_warnings`.
   - `recruitment_types` should use standard values: "校园招聘", "社会招聘",
     "实习", "博士专项", "提前批", "内推". Infer from context if not explicit.
   - `confidence` must reflect **data source quality**, not extraction effort.
     Calibrate by `evidence_type` tier (see wechat-image-handling.md Level 6 Step 3):
     - `browsed_detail_page` → 0.88–0.95
     - `ocr_full_jd_text` → 0.60–0.75
     - `ocr_poster_keyword` → 0.40–0.55
     - A poster that only says "AI应用" with no JD body stays at 0.45 max,
       regardless of OCR accuracy.

5. **Save the output** to `output/candidates/<content_hash>.json`.

6. **Validate** with the schema checker:
   ```bash
   python scripts/validate.py output/candidates/<content_hash>.json --package --verify
   ```
   Fix any validation errors and re-validate.

### Phase 5 — NORMALIZE & DEDUPLICATE: Merge and collapse

**L1 (lightweight):** Normalize individual titles or compute core hashes on demand —
no need to load all candidates:

```bash
# Quick title comparison
python scripts/normalize.py --title "AI Agent开发工程师【2027届】（深圳）"
# → "aiagent开发工程师"

# Compute body hash for identity dedup
python scripts/normalize.py --hash \
  --resp "设计并实现LLM Agent系统..." \
  --req "本科及以上，熟悉LangChain..."
# → core_hash: 3f8a2b...
```

**L3 (full pipeline):** After processing all URLs (or a batch), run the deterministic
post-processor:

```bash
# Single command: normalize, dedup, package keys, quality-check, merge
python scripts/deduplicate.py output/candidates/*.json --out output/merged_final.json
```

This handles capabilities the LLM cannot do:
- **NFKC normalization** of titles/companies (zero-width chars, full-width → half-width)
- **Trailing qualifier stripping** (算法工程师（上海）→ 算法工程师 for identity comparison)
- **Semantic deduplication** by canonical identity (JD-body hash for full-JD, normalized title for title-only)
- **Title-substring clustering** — same `core_hash` but different suffix (算法工程师 vs 算法工程师-应届) merges into one; genuinely different roles (算法工程师 vs 算法研究员) with shared JD body stay separate
- **Title-only echo dropping** — list-page titles that echo an already-captured full-JD detail page are removed
- **Idempotency keys** (SHA-256 of canonicalized fields — safe for database upsert)
- **Evidence quality checks** (staleness: pre-2024 dates, vagueness: < 50 chars, non-JD text)
- **Coverage completeness report** (unique URLs with candidates vs total unique evidence pages)

### Phase 6 — PERSIST: Collect and report

After dedup, review and save final output:

```bash
# Count candidates
python -c "import json; d=json.load(open('output/merged_final.json')); print(f'Total: {len(d)}')"

# View stats from dedup run (piped from deduplicate.py output)
```

## Error handling guide

| Situation | Action |
|-----------|--------|
| URL returns 403 / login wall | Skip, record in `output/errors.jsonl` |
| Page renders but has no job listings | Mark as `empty`, record screenshot path |
| Page has >100 positions (estimate) | Process first 3 pages only, note in summary |
| LLM extraction produces invalid JSON | Re-read the text and try again with stricter prompt |
| Playwright times out (30s+) | Retry once with `--wait 5000`, then skip |
| WeChat article has images (any) | ALWAYS attempt OCR — see `references/wechat-image-handling.md` for the full decision tree (6 levels) |
| WeChat article: OCR done but only keywords, no JD body | Classify channel → if URL found, recursively browse career site (Level 6) |
| Recursive browse of career URL returns only navigation (SPA) | Mark `needs_deep_crawl`, save OCR JDs, append to errors.jsonl (Level 6 Step 4) |
| Recursive browse succeeds with full JDs | Replace OCR extraction with browsed JDs, confidence 0.85+ (Level 6 Step 2B) |
| Search mode: no search box found | Auto-fallback to `list` mode (with `--fallback full`) |
| Search mode: keyword returns 0 results | Try next keyword (first_match) or fallback to full list |
| Search mode: post-search count == pre-count | Warning logged — possible client-side fake filter; results may be incomplete |

## State and resumability

The skill uses the filesystem as its state store with `output/state.json` as the
master index. You can stop at any point and resume later — the incremental logic
(see top of this file) ensures you only process what's changed.

```bash
# Check which URLs have been processed (fast, no browser)
python -c "import json; s=json.load(open('output/state.json')); print(f'Processed: {len(s[\"processed\"])} hashes')"

# List already-cached evidence files
ls output/evidence/  # each file is sha256_<content_hash>.txt

# List already-extracted candidates
ls output/candidates/  # each file is sha256_<content_hash>.json
```

## References

Load these as needed during processing:

- `references/site-catalog.md` — Known career site patterns, selectors, and quirks
- `references/extraction-guide.md` — Detailed JD extraction rules with examples
- `references/schema.md` — Full NormalizedJobCandidate JSON schema
- `references/wechat-image-handling.md` — WeChat article full pipeline: OCR strategy (5 levels) + channel triage & recursive browsing (Level 6)

## Progressive disclosure: how deep to go

This skill is designed with three usage levels. Start shallow; go deeper only when needed.

| Level | What you load | When to use |
|-------|--------------|-------------|
| **L1: Quick normalize** | `scripts/normalize.py --title "..."` | Comparing two job titles, computing a `core_hash` for identity |
| **L2: Single URL** | `browse.py` + LLM extract + `validate.py` | Processing one career page end-to-end |
| **L2w: WeChat article** | `browse.py` → OCR + `wechat-image-handling.md` (6-level pipeline) | Processing a WeChat article with channel triage + recursive browsing |
| **L3: Batch pipeline** | All scripts + `state.json` + `deduplicate.py` + incremental logic | Processing dozens of URLs from Smartsheet |

Each script works standalone at its level. `deduplicate.py` contains its own embedded
normalizer so L3 doesn't require running L1 first — but L1 is available as a lighter
tool when you just need to normalize one title for comparison.

## Scripts

- `scripts/normalize.py` — **L1**: Standalone title/company/JD-body normalization + `core_hash`
- `scripts/browse.py` — **L2**: Playwright-based page renderer and text extractor
- `scripts/validate.py` — Schema validator + packaging keys + evidence quality checks
- `scripts/deduplicate.py` — **L3**: Normalize, semantic-dedup, and merge candidates across batches
- `scripts/state.py` — **L3**: Incremental state manager: init, check (skip/extract), mark, diff
- `scripts/ocr_image.py` — Multi-backend OCR (vision → PaddleOCR → Tesseract) for image extraction
