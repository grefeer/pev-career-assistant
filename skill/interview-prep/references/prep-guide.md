# Interview Prep - Guide

When to use, the workflow, the prompt, and the tolerant JSON parse for the
interview-prep skill. Read this for the L2 single-job path.

## When to use

Use this skill when you (the LLM orchestrator) need to prepare a candidate for a
specific interview: the human has a target job and a confirmed profile, and
wants a structured, role-tailored study kit.

Do NOT use this skill to:

- Tailor a resume (that is the `resume-tailoring` skill)
- Track application status (that is the `application-tracking` skill)
- Research a company's openings (that is the `company-research` skill)
- Auto-submit or auto-fill any form (this skill is read-only study material)

## Workflow: generate -> review

### 1. Assemble input

`generate.py` reads a JSON object with four fields (only `job_snapshot` is
required; see `schema.md`):

```json
{
  "job_snapshot": { "title": "...", "requirements": ["..."], "responsibilities": ["..."] },
  "profile_facts": { "exp_api": { "role": "...", "summary": "..." } },
  "preferences": { "desired_roles": ["Backend Engineer"] },
  "match_analysis": { "strengths": [{"area": "..."}], "gaps": [{"area": "..."}] }
}
```

Unlike resume-tailoring, `profile_facts` keys are not constrained to a
`valid_fact_refs` allowlist - they are passed through to the LLM to ground the
talking points and topics to review.

### 2. Generate

```bash
python scripts/generate.py --input output/input.json --out output/prep_kit.json
```

The script writes the full result to `--out` and prints a one-line summary to
stdout:

```
{"status": "ok", "code": null, "section_count": 12, "agent_version": "1.0.0", "out": "output/prep_kit.json"}
```

`section_count` is the total number of strings across all five sections.

### 3. Review (human-controlled)

The human reviews `output/prep_kit.json` and studies the kit before the
interview. This skill does not write to the interview-prep store.

## The prompt

The System prompt (embedded in `generate.py`, mirrored from the backend
`LLMInterviewPrepGenerator`) instructs the LLM to:

1. Emit a JSON object only - no prose, no markdown fences, no commentary.
2. Include exactly these five keys, each a list of concise strings:
   - `technical_questions` - likely technical questions for this role
   - `behavioral_questions` - likely behavioral/situational questions
   - `talking_points` - strengths/stories to emphasize, grounded in the profile
   - `topics_to_review` - concepts/skills to brush up on before the interview
   - `questions_to_ask` - thoughtful questions to ask the interviewer
3. Tailor every section to the target job.

The Human message is the serialized input payload (job_snapshot + profile_facts
+ preferences + match_analysis).

## The tolerant JSON parse

LLMs do not always return clean JSON. `generate.py` imports the shared
`skill/_common/llm_json.py` helpers and tries, in order:

1. A fenced ```json ... ``` block (regex search).
2. The whole content.
3. A bracket slice (`{...}` or `[...]` between the first open and last close).

The first value that parses is used. Then `coerce_content` normalizes the five
sections: every `CONTENT_KEYS` entry becomes a list (non-list values -> `[]`,
non-string items dropped, unknown keys ignored). If the parsed payload is not a
dict, or all five sections end up empty, the script returns `status=failed` with
`code=interview_prep_parse_error` / `interview_prep_empty_content` (exit 0).

## Credential resolution

`generate.py` resolves the API key in this order (mirrors `src/utils.py`):

1. `DEEPSEEK_API_KEY` in the process env
2. `OPENAI_API_KEY` in the process env
3. `DEEPSEEK_API_KEY` in Windows CURRENT_USER\Environment (User scope)
4. `OPENAI_API_KEY` in Windows User scope

The base URL defaults to `https://api.deepseek.com` (override with
`OPENAI_BASE_URL`). The model defaults to `deepseek-v4-flash` (override with
`--model` or `OPENAI_MODEL`). For `deepseek-v4` models on a deepseek base URL,
the script disables the interleaved `thinking` mode so JSON parses cleanly
(mirrors `backend.app.services.interview_prep.llm_factory`).

A missing key returns `status=failed`, `code=missing_api_key` (exit 0) - it does
not crash.
