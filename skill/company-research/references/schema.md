# Company Research Output Schema

The runtime assembles a `CompanyResearchResult` after one `browse.py` run.

## Top-level result

| Field | Type | Notes |
|-------|------|-------|
| `status` | `str` | `succeeded` / `needs_manual_review` / `failed` |
| `summary` | `str` | One-line human description, e.g. `researched Acme; found 3 opening(s)` |
| `profile` | `object \| null` | Populated on `succeeded` (and preserved on `needs_manual_review` when a partial parse exists) |
| `openings` | `array[object]` | Each opening (see below); `[]` when none found |
| `evidence_refs` | `array[object]` | One per page file written by `browse.py` |
| `block_reason` | `str \| null` | Set only on `needs_manual_review`: `anti_bot` / `login_required` / `captcha` / `no_evidence` / `artifact_error` |
| `last_error` | `str \| null` | Set only on `failed` |

## Profile object

| Field | Type | Notes |
|-------|------|-------|
| `company_name` | `str` | From the request (not parsed from the page) |
| `description` | `str \| null` | First 1000 chars of the rendered page text |
| `industries` | `array[str]` | Always `[]` in v1 (no industry classifier) |
| `locations` | `array[str]` | Sorted union of all opening `locations` |
| `opening_count` | `int` | `len(openings)` |

## Opening object

Recovered deterministically from the page text. Fields:

| Field | Type | Notes |
|-------|------|-------|
| `title` | `str` | Required; non-empty |
| `company_name` | `str` | Page value if present, else the request company name |
| `department` | `str \| null` | Only from `PUBLIC JOB` JSON blocks |
| `responsibilities` | `str` | Required; non-empty JD body |
| `locations` | `array[str]` | One or more; `[]` if none parsed |
| `recruitment_types` | `array[str]` | Defaults to `["校园招聘"]` |
| `evidence_refs` | `array[object]` | `evidence_type` = `public_json_job` / `detail_evidence` / `public_search_card`; `content_hash` = SHA-256 of the page file; `relative_path` = `output/evidence/pages/<name>` |

## Evidence reference object (page-level)

| Field | Type | Notes |
|-------|------|-------|
| `evidence_type` | `str` | `page_text` |
| `content_hash` | `str` | SHA-256 of the page file bytes |
| `relative_path` | `str` | `output/evidence/pages/<name>` |

## browse_metadata.json (written by `scripts/browse.py`)

| Field | Type | Notes |
|-------|------|-------|
| `status` | `str` | `ok` / `blocked` / `empty` / `error` |
| `url` | `str` | The requested URL |
| `title` | `str` | Page title (when rendered) |
| `block_reason` | `str` | `anti_bot` only (when `status=blocked`) |
| `error` | `str` | Truncated to 500 chars (when `status=error`) |
| `pages_collected` | `int` | `1` when `status=ok` |
