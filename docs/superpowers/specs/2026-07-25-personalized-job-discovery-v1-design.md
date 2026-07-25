# Personalized Job Discovery v1 Design

## Goal

Evolve job discovery from a shared, administrator-gated review queue into a
personalized recruitment assistant. Each user controls their own target-role
preferences and receives directly usable, automatically evidence-verified job
recommendations. Sources that cannot be verified are visible with a clear
reason so the user can decide whether to inspect them manually.

This document defines the first delivery only. Shared crawl caches, scheduled
refresh, concurrent-refresh coalescing, and recommendation precomputation are
explicitly deferred.

## User-facing behavior

Each authenticated user owns exactly one editable discovery preference profile.
The profile contains:

- target roles, such as `AI 应用开发` and `Agent 开发`;
- optional synonyms/related terms;
- optional excluded roles/terms; and
- a semantic-match threshold with a conservative system default.

On a discovery run, the system first performs a high-recall title and metadata
screen. A listing remains eligible if its title, category, or configured
synonym suggests a target role. Ambiguous and weakly related listings are not
discarded at this phase: their detail page is fetched and its JD is evaluated
against the same profile. The second phase removes candidates that are clearly
unrelated while deliberately favoring recall for genuinely suitable roles.

Automatically evidence-verified candidates are returned directly to the owner
of the profile; no administrator approval is required for this personalized
output. This does not change evidence requirements or permit bypassing login,
captcha, QR login, authentication, or anti-bot controls.

For blocked or incomplete sources, the result also contains a source-status
item instead of silently omitting it. It identifies the source URL/site and a
safe reason code, for example `login_required`, `captcha`,
`authentication_required`, or `coverage_incomplete`, plus a message that the
user must confirm it independently.

## Architecture and ownership

```text
User preference profile
  -> task-scoped role filter
  -> broad listing recall
  -> fetch detail/JD for eligible or ambiguous listings
  -> semantic relevance decision
  -> evidence verification + canonical deduplication
  -> user-owned direct recommendation / source-status result
```

The preference profile is user-owned data. API reads and writes always enforce
the authenticated user ID; an administrator cannot accidentally use one
student's preferences for another student's run. A profile has no global
effect on shared strategies, crawl plans, source configuration, or another
user's recommendations.

The filtering logic is a deterministic service-layer component. It receives a
profile and `NormalizedJobCandidate` plus available JD text. Its decision
records a match category (`title_match`, `semantic_match`, or `rejected`) and
safe explanation terms for audit/debugging, without exposing raw LLM prompts or
private source data.

The semantic evaluator is used only after high-recall prefiltering. It must
return structured relevance output constrained to the configured target roles;
it never creates candidates, modifies JD content, or asserts crawl coverage.
When the evaluator is unavailable or returns invalid output, the candidate is
retained only if it had a strong deterministic title match; otherwise it is
reported as inconclusive rather than falsely relevant.

## Data and API boundaries

Add a user-scoped preference model/repository/service/API DTOs rather than
adding fields to global `Settings`. The API supports read-or-create, update,
and reset of the caller's own profile. Role lists are normalized, bounded in
count and length, deduplicated case-insensitively, and reject blank values.

Recommendation output is user-scoped. Existing administrator review data and
legacy global job-discovery task persistence remain intact during the
migration. Personalized direct recommendations must not change the visibility
or status of shared `JobPosting` records used by other product workflows.

Blocked/incomplete source status is a narrow DTO: source identity, reason
code, display text, and optional retry guidance. It contains no cookies,
authorization data, request body, full response payload, or anti-bot details.

## Failure and safety policy

- A login, captcha, QR login, authentication, or anti-bot signal is never
  retried through a bypass path and never presented as a job recommendation.
- Coverage-incomplete results never claim that all jobs were collected.
- A relevance filter does not override `CoverageVerifier`; it only narrows
  candidates already supported by evidence.
- Missing JD or semantic-evaluator failure cannot turn an unrelated listing
  into a positive recommendation.
- Direct output remains evidence-backed, deduplicated, and owner-scoped.

## Out of scope for v1

- Shared cache tables or TTLs;
- scheduled refresh jobs;
- collapsing N users by M queries into one crawl;
- push notifications;
- administrator review removal from existing shared job-posting workflows;
- automatic application/form filling.

Those become a later performance and product workflow phase after v1 relevance
quality is measured.

## Test strategy

Unit tests cover profile normalization/ownership, high-recall matching,
exclusion precedence, strong-title fallback when semantic evaluation is
unavailable, and rejection of clearly unrelated JD text. Service tests cover
per-user isolation and direct recommendation output. Worker/pipeline tests
cover the two-stage detail evaluation and blocked/incomplete source-status
items. API tests verify a user cannot read or update another user's profile.

Regression tests retain current evidence verification, coverage behavior,
canonical deduplication, and the no-bypass policy for walls.
