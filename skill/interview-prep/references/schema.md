# Interview Prep - Schema

Field tables for the input, the five content sections, and the output. Read
this when you need exact shapes.

## Input to `generate.py`

`--input PATH` or stdin must be a JSON object:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `job_snapshot` | object | yes | The target job: `title`, `requirements` (list), optional `responsibilities` |
| `profile_facts` | object | no | Candidate's confirmed facts; passed through to ground talking points |
| `preferences` | object | no | e.g. `desired_roles`, `target_cities`; passed through to the LLM |
| `match_analysis` | object | no | `strengths`/`gaps` lists; guides emphasis |

## The five content sections

Every section is guaranteed to be a list of strings (non-string items dropped,
missing/non-list values default to `[]`):

| Key | Content |
|-----|---------|
| `technical_questions` | Likely technical questions for this role |
| `behavioral_questions` | Likely behavioral/situational questions |
| `talking_points` | Strengths and stories to emphasize, grounded in the profile where possible |
| `topics_to_review` | Concepts/skills to brush up on before the interview |
| `questions_to_ask` | Thoughtful questions the candidate can ask the interviewer |

## `generate.py` output (`--out`)

On success:

```json
{
  "status": "ok",
  "content": {
    "technical_questions": ["...", "..."],
    "behavioral_questions": ["..."],
    "talking_points": ["..."],
    "topics_to_review": ["..."],
    "questions_to_ask": ["..."]
  },
  "agent_version": "1.0.0"
}
```

On failure (exit 0, never a crash):

```json
{ "status": "failed", "code": "<code>", "last_error": "...", "agent_version": "1.0.0" }
```

Failure `code` values:

| code | meaning |
|------|---------|
| `missing_api_key` | No `DEEPSEEK_API_KEY`/`OPENAI_API_KEY` in env or Windows User scope |
| `interview_prep_interrupted` | LLM call raised (network/auth/timeout); `last_error` is the exception text |
| `interview_prep_parse_error` | LLM response had no parseable JSON, or the JSON was not an object |
| `interview_prep_empty_content` | JSON parsed but all five sections came back empty |
| `bad_input` | Input file unreadable or not a JSON object |

`generate.py` stdout is always a one-line summary JSON with `status`, `code`,
`section_count` (total strings across all sections, 0 on failure),
`agent_version`, and `out`.
