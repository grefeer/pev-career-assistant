---
name: company-research
description: >
  Research a single company from its public careers or about page. Browses one
  URL with headless Chromium, captures the rendered page text, and extracts a
  structured company profile (description, locations, open-position list with
  titles and apply links). Use when the user wants to investigate a company,
  survey a company's open positions, or build a company brief before applying.
  Also use when the user mentions "公司调研", "公司岗位调研", "查一下这家公司",
  "这家公司在招什么", or similar Chinese phrases.
compatibility: requires Python 3.10+, playwright (pip) with a chromium browser installed
---

# Company Research Skill

Produce a structured company-research report from one public careers/about URL.
The runtime is a deterministic orchestrator: it fetches a single page, parses
the observed text for openings, and assembles a company profile. There is no
multi-page pagination, no preference filtering, and no coverage gate - this is
a one-page research brief, not a full-site crawl.

**Security gate.** A login, captcha, or anti-bot verification wall surfaces
as `status=blocked` and is never bypassed. The report lands in
`needs_manual_review` with a `block_reason` so a human can decide.

## Scripts

| Script | Purpose |
|--------|---------|
| `browse` | Fetch one URL with headless Chromium; write `output/evidence/pages/page_001.txt` and emit a `browse_metadata` summary whose `status` is `ok` / `blocked` / `empty` / `error`. |

## Output contract

After `browse`, the runtime reads `output/evidence/browse_metadata.json` and the
`page_*.txt` files, parses openings deterministically, and returns a
`CompanyResearchResult` (profile + openings + evidence refs). The report is
read-only research output - it is never an auto-submit and never a JobPosting.
