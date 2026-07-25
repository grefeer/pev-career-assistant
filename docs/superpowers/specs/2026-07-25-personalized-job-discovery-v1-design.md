# Personalized Job Discovery v1 Design

## Decision and scope

This is the first personalized job-discovery delivery of the personal
recruitment assistant. It uses the user's explicit product decision **B**:
automatically discovered, pre-review candidates may be recommended directly to
their owner without an administrator approval step.

That is a deliberate change from the shared `JobPosting` workflow, where the
student API exposes only `verified` postings. It does **not** change that
existing workflow or make pre-review candidates globally visible. A
personalized recommendation is clearly labelled as automatically discovered
and advises the user to confirm the source before applying.

v1 does not add shared crawl caching, scheduled refresh, query coalescing,
push notifications, or automatic application/form filling. A user-triggered
personalization run evaluates already discovered candidates; it does not start
a new Playwright/LLM crawl per user.

## Existing components to extend

This is an extension of the personal-assistant subsystem delivered on
2026-07-22, not a second implementation of it:

- `UserPreference` remains the single user-owned preference model and its
  optimistic `version` remains the invalidation boundary;
- `preferences_service` and `repositories/preferences.py` remain the only
  preference write/read path;
- `RelevanceRanker` remains the only semantic scorer, returning a `0–100`
  score, reason, and matched signals;
- `RecommendationService` remains the rank/filter orchestration pattern; and
- `JobRelevanceScore` remains unchanged for **verified `JobPosting`**
  recommendations.

`JobRelevanceScore.job_id` has a foreign key to `job_postings.id`, so it cannot
represent pre-review `DiscoveredJobCandidate` records. v1 therefore adds a
separate user-owned delivery record rather than weakening that foreign key or
misusing the verified-job cache.

## User-visible behavior

Each authenticated user owns exactly one editable preference profile. v1
extends `UserPreference` with these persisted fields:

- `role_synonyms: list[str] | None`;
- `excluded_roles: list[str] | None`; and
- `personalized_discovery_min_score: float | None`.

`desired_roles` remains the canonical target-role field. All role lists are
trimmed, bounded in count and length, case-insensitively deduplicated, and
reject blank items. The threshold is validated in the `0–100` range. Updating
any of these fields increments `UserPreference.version` through the existing
preference service.

A user explicitly chooses “find jobs for me”. v1 selects candidates from
completed shared discovery tasks; it never launches a user-specific crawl. A
candidate is eligible for direct recommendation only when all of the following
are true:

1. its originating PEV task has a complete `CrawlCoverage` proof;
2. it has evidence-supported candidate fields and a safe `http` or `https`
   application URL;
3. it survives canonical identity deduplication; and
4. it passes the user's relevance threshold after exclusions are applied.

Legacy PATH C results without coverage proof and candidates from incomplete,
blocked, or manual-review tasks are never represented as direct job
recommendations.

The recommendation card displays its relevance score, short reason, matched
signals, evidence link(s), and the label “自动发现，建议自行确认”. It is visible
only to the owning user. It neither changes the status nor the student-facing
visibility of the shared `JobPosting` record.

## Two-stage relevance pipeline

The user requested high recall without flooding recommendations with unrelated
roles. The pipeline therefore has two different filters:

```text
shared completed PEV candidates
  -> broad deterministic title/category/synonym recall
  -> use already captured detail JD
  -> evidence verification + canonical deduplication
  -> RelevanceRanker score (0–100) against profile + preferences
  -> exclusions and user threshold
  -> user-owned pre-review recommendation
```

The broad first phase deliberately keeps plausible and ambiguous AI/Agent
roles. It exists to avoid false negatives before considering JD text. It does
not authorize output. The semantic decision occurs only after the candidate is
evidence-supported and deduplicated. `RelevanceRanker` is reused directly; no
new three-state semantic classifier or competing scorer is introduced.

If the ranker is unavailable or returns an invalid result, its score is zero.
The candidate is not recommended unless a future, separately approved fallback
policy says otherwise. This favors user safety over an unsupported positive
recommendation.

## Persistence and ownership

v1 adds two owner-scoped records:

- `PersonalizedDiscoveryRun`: records an explicit user request, the preference
  version used, start/finish time, and a summary; and
- `PersonalizedDiscoveryRecommendation`: links the owning user and run to one
  `DiscoveredJobCandidate`, stores the preference version, score, reason,
  matched signals, and presentation state.

These are delivery/audit records, **not** a shared crawl cache or a replacement
for `JobRelevanceScore`. They give the user a stable personalized result while
keeping the original candidate and its evidence traceable. A uniqueness
constraint prevents duplicate delivery of the same candidate to one user for
the same preference version/run.

The recommendation service may call `RelevanceRanker` again for a new v1 run;
cross-run score caching is explicitly deferred. The existing
`JobRelevanceScore` cache remains valid for the verified-JobPosting product
path and its existing invalidation semantics must not regress.

All preference, run, recommendation, and read APIs enforce `current_user.id`.
No endpoint accepts a free-form user ID for these resources.

## Source-status output

Every personalization run also produces owner-scoped source-status items for
the selected sources that cannot be safely recommended. A status is one of the
closed reason-code taxonomy:

- `login_required`;
- `captcha`;
- `anti_bot`;
- `authentication_required`;
- `coverage_incomplete`; or
- `needs_manual_review`.

The display message is generated from that closed code, never copied from a
wall page or raw error. A status contains only source identity, safe URL,
reason code, display message, and retry/confirmation guidance. It never
contains cookies, authorization data, request body, full response content, or
anti-bot implementation details.

Blocked sources are not retried through a bypass path. Coverage-incomplete
sources never claim completeness. Users can inspect them manually outside the
system but they are not job recommendations.

## Integration with PEV and existing review flows

Personalization consumes the output of the PEV post-crawl pipeline after
`CoverageVerifier`, evidence verification, and canonical deduplication. It is
not a new PATH C candidate generator and cannot override PEV coverage. The
existing administrator review queue and `JobPosting` verification state machine
remain unchanged for shared student job-center APIs.

The PEV migration's blocked/manual result taxonomy is mapped into the closed
source-status taxonomy above. New reason mappings must be explicit and tested;
unknown raw errors become `needs_manual_review`, never free-form text.

## API boundary

Add authenticated, owner-scoped APIs for:

- reading/updating/resetting the caller's extended `UserPreference`;
- creating a personalization run from the completed shared discovery pool;
- listing the caller's recommendations and source-status items; and
- recording user presentation interactions such as viewed, saved, dismissed,
  and apply-clicked.

Creating a run has a per-user request limit and no URL/crawl parameters. This
prevents the v1 endpoint from becoming a user-controlled crawler. The API
returns an explicit in-progress state while ranking and then a final result;
it does not expose raw discovery task payloads.

## Safety policy

- Direct recommendations require PEV coverage completeness, evidence support,
  canonical deduplication, safe application URLs, exclusions, and the user
  threshold. Evidence support is not represented as human verification.
- Login, captcha, QR-login, authentication, and anti-bot signals never become
  recommendations and never trigger a bypass or legacy-navigation fallback.
- Relevance ranking narrows the already verified evidence set; it never creates
  candidates, mutates JD content, or asserts crawl completeness.
- Missing JD or ranker failure cannot create a positive recommendation.
- Existing shared `JobPosting` APIs retain their `verified`-only visibility
  rule. Pre-review personalized results are exposed only through the new
  owner-scoped recommendation API.

## Test strategy

Unit tests cover preference extension normalization, ownership, version bumps,
exclusion precedence, broad synonym recall, `RelevanceRanker` thresholding,
and ranker-failure rejection. Repository/service tests cover recommendation
ownership, unique delivery, and no cross-user reads.

Pipeline tests prove that only coverage-complete, evidence-backed PEV
candidates can enter personalization; legacy/uncovered candidates and blocked
tasks become source statuses. API tests prove a user cannot read or modify
another user's profile, runs, recommendations, or statuses. Migration tests
cover the new preference columns and user-owned records.

Regression tests retain `JobRelevanceScore` behavior and cache invalidation for
verified JobPostings, current evidence/coverage/deduplication behavior, and the
no-bypass policy for walls.
