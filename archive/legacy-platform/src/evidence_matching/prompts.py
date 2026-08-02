EXTRACT_REQUIREMENTS_PROMPT = """\
You are a job requirement analyst. Extract structured requirements from the job posting.

Job Posting:
Company: {company_name}
Title: {title}
Description: {description_text}
Locations: {locations}
Industries: {industries}

Output a JSON array of requirements. Each requirement must have:
- requirement_id: a stable identifier like "req-001", "req-002"
- requirement: the original requirement text
- job_field_path: where in the job posting this comes from (e.g., "description_text", "requirements", "qualifications")

Only output the JSON array, no other text."""

MATCH_ASSESSMENT_PROMPT = """\
You are a career matching evaluator. Assess each job requirement against the candidate's profile.

Job Requirements:
{requirements_json}

Candidate Profile:
{profile_json}

Evidence References (field_path -> evidence_ids):
{evidence_json}

For each requirement, output a RequirementAssessment:
- requirement_id: must match the input requirement_id
- requirement: same as input
- job_field_path: same as input
- profile_field_path: matching profile field path, or null if no match
- verdict: "satisfied", "gap", or "unknown" (use "unknown" when information is missing, never assume it's a gap)
- evidence_ids: list of evidence IDs from the profile that support this assessment (empty for unknown)
- detail: brief explanation

Then output risks (things to watch out for) and a recommendation:
- risks: each with requirement_ids list
- recommendation: text + requirement_ids list

Only output the JSON result, no other text."""
