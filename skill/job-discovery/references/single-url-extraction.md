# Single-URL Extraction Workflow (Planner -> Executor -> Verifier)

Load this document on demand when you are given ONE career-site URL and must
return the structured JDs for that company. It is the per-URL counterpart to
`SKILL.md` (which documents the SmartSheet batch workflow - you do NOT need
`SKILL.md` here).

The design solves two failure modes that a single-pass extractor hits:

1. **Output-cap loss.** One LLM generation can emit only ~8192 tokens, so a site
   with 151 jobs (e.g. Mioffice/xiaomi) loses ~90% of its listings if one agent
   tries to emit them all at once. **Fix:** extract per page, one small
   generation per page, persisted to disk - never emit the full set in one
   message.
2. **Context bloat.** Holding 16 pages of rendered text in one agent's context
   wastes tokens and slows it. **Fix:** stash each page's text on disk
   (`browse.py` writes `output/evidence/pages/page_NN.txt`) and let a
   per-page sub-agent read only its own page.

## Roles

- **You (planner/verifier).** Browse once, fan out one `jd_extractor` sub-agent
  per page, then merge with `deduplicate.py`. You never hold all the JDs - you
  hold only page-file paths and short write confirmations.
- **`jd_extractor` sub-agent.** Reads ONE page file, extracts that page's JDs
  as a JSON array, and persists them to `output/candidates/page_NN.json` via
  `write_candidates`. One sub-agent per page. Sub-agents do NOT dispatch
  further sub-agents (max depth 2).

## Step-by-step

### 1. Load the schema (once)
```
read_file(file_path="/job-discovery/references/schema.md", limit=1000)
```
Do NOT read `SKILL.md` - it is large and documents the SmartSheet flow.

### 2. Render + paginate (planner)
```
run_skill_script(script="browse", cli_args="<URL> --mode list --max-pages 3 --out output/evidence")
```
The result JSON now carries **`page_files`** (a list of `output/evidence/pages/page_NN.txt`
paths) and **`page_count`**, in addition to the inlined `[PAGE_TEXT]`.

- If `[PAGE_TEXT]` is missing / `< ~500 chars` (common on Moka/feishu/zhiye
  SPAs), retry ONCE with `--mode search-interact`. If still empty, the page is
  an SPA shell / dead URL - emit `{"status":"blocked","reason":"page did not
  render job content"}` and stop.
- **Paginate** if the text signals more jobs than one page holds: a total count
  (`共151` / `(151)` / `151 职位` / `151 results`) larger than what you see, OR
  a paginator control (`下一页` / `加载更多` / `查看更多` / page numbers
  `1 2 3 ... 16` / a next arrow):
  ```
  run_skill_script(script="browse", cli_args="<URL> --mode click --click-auto --click-count 15 --out output/evidence")
  ```
  `--click-auto` re-detects the next-page arrow each click (icon-only arrows on
  Mioffice/atsx sites like xiaomi). If the paginator is a text button use
  `--click-text "下一页"` / `--click-text "加载更多"` / `--click-text "查看更多"`
  instead. Set `--click-count` high (e.g. 15); it stops early when exhausted
  (`end_reached: true`). Skip pagination if `[PAGE_TEXT]` already shows all the
  jobs and no paginator/total-larger-than-visible is present.

**HARD LIMITS - do not flail:**
- At most ONE list browse, ONE click-paginate, and ONE search-interact retry per
  URL. If a click-paginate does not grow the page text (`pages_collected` stays
  1, or `[PAGE_TEXT]` is unchanged), the SPA's load-more is not something
  browse can drive. STOP paginating and proceed to step 3 with whatever pages
  you have - extracting the visible jobs correctly is far better than retrying
  browse until the run crashes. Do NOT loop on browse variants.
- NEVER `read_file` / `ls` / `glob` anything under `output/evidence/` - and
  especially never read a `.png`/`.jpg` screenshot. The evidence dir holds the
  content-addressed cache (often 0-byte text files or PNG screenshots); reading
  them returns empty/image bytes that the API rejects (400 crash). The page
  text you need is ONLY ever under the browse result's `[PAGE_TEXT]` marker, or
  via `read_evidence` on `output/evidence/pages/page_NN.txt` (which is a
  script, not `read_file`, and returns clean text).

After this step you have `page_files = [page_01.txt, ..., page_NN.txt]`.

### 3. Fan out one `jd_extractor` per page (executor, PARALLEL)
In your **next single message**, emit one `task` tool call per page file - all
in that one message so they run in parallel:

```
task(subagent_type="jd_extractor",
     description="Page file: output/evidence/pages/page_01.txt. Company: <COMPANY>.
                  Write your extracted candidates to output/candidates/page_01.json.")
task(subagent_type="jd_extractor",
     description="Page file: output/evidence/pages/page_02.txt. Company: <COMPANY>.
                  Write your extracted candidates to output/candidates/page_02.json.")
... one per page ...
```

Each sub-agent reads its own page file (it does NOT receive the page text from
you - this keeps your context lean) and writes its own output file. The
`task` result you get back is a short write confirmation, NOT the candidates.

If `page_count == 1` you still dispatch one `jd_extractor` (consistency).

### 4. Merge + verify (verifier)
Once all sub-agents return, merge the per-page files into one deduplicated,
packaged, verified result:
```
run_skill_script(script="deduplicate",
                 cli_args="output/candidates/*.json --out output/candidates_merged.json")
```
`deduplicate.py` normalizes, drops title-only echoes of full JDs, adds
idempotency/similarity keys, and runs evidence-quality checks. Its stdout
summary reports `input_count` / `output_count` / `duplicates_removed`.

### 5. Final message (short - do NOT re-emit the candidates)
Your final message must be ONLY a small JSON summary, e.g.:
```
{"status":"done","pages":16,"candidates_file":"output/candidates_merged.json","merged_count":151}
```
The harness reads `output/candidates_merged.json` off disk - re-emitting the
candidates here would just re-hit the output cap that this design exists to
avoid. If the page was a login/captcha/anti-bot wall, emit instead
`{"status":"blocked","reason":"<one short line>"}` and stop.

## Constraints
- Tool budget <= 14 (schema read + browse + maybe click + N task calls + 1
  deduplicate). The `task` calls count toward budget but run in parallel.
- Run helper scripts ONLY via `run_skill_script`. Allowed: browse, validate,
  normalize, deduplicate, ocr_image, state, read_evidence, write_candidates.
- Never bypass login / captcha / anti-bot. If blocked, emit the blocked JSON.
- Use the company name you are given for `company_name`.
- Campus / 提前批 / 校招 is the default `recruitment_type` unless the page says
  otherwise (社招 / 实习).
