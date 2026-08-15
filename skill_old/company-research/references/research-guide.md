# Company Research Guide

A one-page research brief from a single public careers/about URL. Unlike
job-discovery this skill does **not** paginate, preference-filter, or run a
coverage gate - it fetches one page, parses observed openings
deterministically, and assembles a company profile.

## When to use

The user wants to investigate one company before applying: survey its open
positions, capture a description, and collect apply links. Trigger phrases:
「公司调研」「公司岗位调研」「查一下这家公司」「这家公司在招什么」.

## Workflow

1. **Browse (deterministic).** The runtime runs `scripts/browse.py <url>` once.
   The script renders the page with headless Chromium, writes
   `output/evidence/pages/page_001.txt`, and emits a `browse_metadata.json`
   whose `status` is one of `ok` / `blocked` / `empty` / `error`.
2. **Classify the metadata.**
   - `blocked` -> the report lands in `needs_manual_review` with a
     `block_reason` (`anti_bot` / `login_required` / `captcha`). **Never
     bypass** a verification wall (security gate #2).
   - `error` -> `failed` with `last_error`.
   - `ok` but no page file -> `needs_manual_review` (`no_evidence`).
   - `ok` with a page file -> parse openings.
3. **Parse openings (deterministic, no LLM).** The runtime reads the page text
   and recovers openings from three evidence shapes the browser may have
   produced (in priority order): `=== PUBLIC JOB N ===` JSON blocks,
   `=== DETAIL ===` evidence blocks, and public search-card text. See
   `schema.md` for the opening fields.
4. **Assemble profile.** `company_name` (from the request), `description`
   (first 1000 chars of page text), `locations` (union of opening locations),
   `opening_count`.

## Output contract

The runtime returns a `CompanyResearchResult` (see `schema.md`):
`status` (`succeeded` / `needs_manual_review` / `failed`), `profile`,
`openings`, `evidence_refs`, and a `summary`. Zero openings is still
`succeeded` - an empty careers page is a valid research result, not a failure.

## What this skill is NOT

- Not a full-site crawl. One URL, one page.
- Not an auto-submit. The report is read-only research output - it is never a
  `JobPosting` and never submits anything on the user's behalf (security gate
  #1).
- Not LLM-driven. All extraction is deterministic Python over the rendered text;
  the only external dependency is Playwright for rendering.
