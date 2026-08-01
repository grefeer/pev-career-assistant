---
name: interview-prep
description: >
  Generate a structured interview-prep kit for a target job via an LLM. Given a
  job snapshot and the candidate's confirmed profile facts, preferences, and a
  match analysis (strengths/gaps), produce five sections - technical questions,
  behavioral questions, talking points, topics to review, and questions to ask
  the interviewer - each tailored to the role. Use when the user wants to 准备面试,
  生成面试题, 整理面试要点, or prep for an interview. Also use when the user mentions
  "面试准备", "面试题预测", "面试要点", "面试辅导", or similar Chinese phrases.
compatibility: requires Python 3.10+, langchain-openai (pip), a configured DEEPSEEK_API_KEY (or OPENAI_API_KEY)
---

# Interview Prep Agent

Produce a structured interview-prep kit tailored to a target job. Designed as a
pi-agent skill - the LLM (you) orchestrates, the helper script handles the
mechanical work (LLM call, JSON parsing, five-section normalization).

**This file is a dispatch hub.** It is intentionally short. Load the reference file
that matches your task from the [Progressive disclosure](#progressive-disclosure-how-deep-to-go)
table or the [References](#references) list - do NOT read them all up front.

## Why this skill exists

Interview prep is a judgment task: which technical questions to expect, which
stories to prepare, which weak spots to brush up. A hand-written checklist per
job does not scale and drifts. Instead, this skill uses:

1. **A bounded LLM call** that emits a JSON object with five section lists (one
   prompt, parsed tolerantly into JSON)
2. **Five normalized sections** - every section is guaranteed to be a list of
   strings (non-strings dropped), so downstream tooling has a stable shape
3. **Role-tailored content** - the prompt forces every section to fit the target
   job, grounded in the candidate's profile where possible

The result is a reviewable study kit a human reads before the interview - not a
free-text essay.

## Security boundary (HARD)

- This skill is **read-only study material**. It never writes to the
  candidate's interview-prep store and never auto-submits anything (no
  `task:submit` scope exists anywhere in this skill).
- A missing API key or unparseable LLM response surfaces as `status=failed`
  with a stable `code` (exit 0) - it never escalates past the human.
- No crawling, no anti-bot path, no `review_version`.

## Quick start

```bash
# 1. Ensure dependencies + credentials
pip install langchain-openai
export DEEPSEEK_API_KEY=...   # or OPENAI_API_KEY (Windows User scope also works)

# 2. Prepare input (job snapshot + confirmed facts + preferences + match analysis):
#    see references/prep-guide.md and references/schema.md
cat > output/input.json <<'JSON'
{
  "job_snapshot": {"title": "...", "requirements": [...]},
  "profile_facts": {"exp_api": {"role": "...", "summary": "..."}},
  "preferences": {"desired_roles": ["Backend Engineer"]},
  "match_analysis": {"strengths": [...], "gaps": [...]}
}
JSON

# 3. Generate the prep kit
python scripts/generate.py --input output/input.json --out output/prep_kit.json

# 4. Review output/prep_kit.json with the human.
```

## Full workflow

There are three phases. The single-job path (L2) covers Phases 2-3; the
differential path (L3) re-runs against an updated match analysis.

### Phase 1 - INPUT: Assemble the generation context (L3 only)

Collect the job snapshot, the candidate's confirmed profile facts, stated
preferences, and a match analysis (strengths/gaps). Field shapes live in
**`references/schema.md`**. Unlike resume-tailoring, profile facts here are not
constrained to a `valid_fact_refs` set - they are passed through to ground the
talking points.

### Phase 2 - GENERATE: Produce the prep kit

Run `scripts/generate.py`. It builds the System+Human prompt, calls the LLM, and
parses the response into the five normalized sections. The prompt, the tolerant
JSON parse, and the credential resolution are documented in
**`references/prep-guide.md`**.

```bash
python scripts/generate.py --input output/input.json --out output/prep_kit.json
```

### Phase 3 - REVIEW: Human-controlled, never auto

Review `output/prep_kit.json` with the human. The human studies the kit before
the interview. This skill does not write to the interview-prep store.

## Error handling guide

| Situation | Action |
|-----------|--------|
| `status=failed`, `code=missing_api_key` | Set `DEEPSEEK_API_KEY`/`OPENAI_API_KEY` in env or Windows User scope; rerun |
| `status=failed`, `code=interview_prep_parse_error` | LLM returned no JSON or a non-object; re-run once, then ask the human to narrow the job snapshot |
| `status=failed`, `code=interview_prep_empty_content` | All five sections came back empty; re-run with a more specific job snapshot |
| `status=failed`, `code=interview_prep_interrupted` | Transient LLM/network error; retry with backoff |
| `status=failed`, `code=bad_input` | Input JSON was malformed; fix the input file |

## References

Load these as needed during processing:

- `references/prep-guide.md` - When to use, the generate->review workflow, the prompt, and the tolerant JSON parse
- `references/schema.md` - Field tables for input, the five content sections, and the output

## Progressive disclosure: how deep to go

This skill is designed with usage levels. Start shallow; go deeper only when needed.

| Level | What you load | When to use |
|-------|---------------|-------------|
| **L1: Quick generate** | `SKILL.md` + `scripts/generate.py` | You just need the five-section kit for one job |
| **L2: Single job** | `SKILL.md` + `references/prep-guide.md` (+ `schema.md` as needed) | Prepare for one job end-to-end with full context |
| **L3: Differential re-prep** | L2 + updated `match_analysis` in input | Re-run generation against a refreshed match analysis without redoing input assembly |

## Scripts

- `scripts/generate.py` - **L1/L2**: LLM call -> tolerant JSON parse -> five normalized content sections
