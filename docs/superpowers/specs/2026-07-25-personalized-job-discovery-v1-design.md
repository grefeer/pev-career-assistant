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

As of 2026-07-29, personalized discovery v1 is the **discovery candidate
delivery path**: discovery candidates no longer go through admin
approve/reject -> `JobPosting(verified)`; they reach users via personalized
discovery (pre-review, owner-scoped, card labelled 「自动发现，建议自行确认」).
The shared `JobPosting` workflow (WP2 manual import/completion -> `verified` ->
`/api/jobs`) remains unchanged and is the sole feed for the verified-only job
center. Pre-review delivery stays independent of `/api/jobs`, never writes
`review_version`, and never transitions a `JobPosting`. (Code-side discovery
candidate admin approve/reject still exists; migration is tracked separately.)

v1 does not add shared crawl caching, scheduled refresh, query coalescing,
push notifications, or automatic application/form filling. A user-triggered
personalization run evaluates already discovered candidates; it does not start
a new Playwright/LLM crawl per user.

v1 supports two, and only two, completeness gates for direct recommendation:
full PEV `CrawlCoverage`, or an explicit `single_source_complete` proof for a
registered single-resource source. A legacy supervisor result is never assumed
complete merely because it returned one candidate.

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

1. its originating task has either complete `CrawlCoverage` or the explicit
   single-resource proof defined below;
2. it has evidence-supported candidate fields and a safe `http` or `https`
   application URL;
3. it survives canonical identity deduplication; and
4. it passes the user's relevance threshold after exclusions are applied.

Candidates from incomplete, blocked, or manual-review tasks are never
represented as direct job recommendations. A PATH C/legacy source is eligible
only after it has an adapter-specific single-resource contract; otherwise it
is surfaced as a source-status item.

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

v1 adds three owner-scoped records:

- `PersonalizedDiscoveryRun`: records an explicit user request, the preference
  version used, start/finish time, and a summary; and
- `PersonalizedDiscoveryRecommendation`: links the owning user and run to one
  `DiscoveredJobCandidate`, stores the preference version, score, reason,
  matched signals, presentation state, and canonical job key; and
- `UserDiscoverySourceStatus`: links the owning user and run to a source task
  that is blocked, incomplete, or unsafe for direct recommendation.

These are delivery/audit records, **not** a shared crawl cache or a replacement
for `JobRelevanceScore`. They give the user a stable personalized result while
keeping the original candidate and its evidence traceable. Recommendations
carry a `canonical_job_key` and have a unique `(user_id, canonical_job_key)`
constraint so the same job discovered by two source tasks does not reappear
twice for one user. A later run updates the existing recommendation's latest
source/task, score, explanation, and `last_run_id`; it does not insert a
duplicate.

`UserDiscoverySourceStatus` is independently persisted rather than inferred
from an omitted recommendation. It stores `user_id`, `run_id`, `task_id`, safe
source identity, closed `reason_code`, generated display text, retry guidance,
and timestamps. Its uniqueness constraint is `(user_id, run_id, task_id,
reason_code)`.

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
- `url_unsafe`; or
- `needs_manual_review`.

The display message is generated from that closed code, never copied from a
wall page or raw error. A status contains only source identity, safe URL,
reason code, display message, and retry/confirmation guidance. It never
contains cookies, authorization data, request body, full response content, or
anti-bot implementation details.

Blocked sources are not retried through a bypass path. Coverage-incomplete
sources never claim completeness. Users can inspect them manually outside the
system but they are not job recommendations.

## Completeness and URL-safety gates

### Full crawl proof

For a migrated PEV adapter, `verify_coverage(coverage).complete` is the
complete proof. It remains the preferred path and applies to the existing
Moka, Feishu, Inovance, and Xiaohongshu-style complete-crawl integrations.

### Single-resource proof

A source may use `single_source_complete` only when its registered adapter and
source contract explicitly declare it. The adapter must prove all of the
following in one deterministic execution:

1. exactly one declared public resource was requested, with no hidden listing
   pagination, cursor, or continuation;
2. the resource produced non-empty JD text and a `PageEvidence` content hash;
3. the adapter emitted a positive terminal signal specific to that source;
4. no login, QR login, captcha, authentication, anti-bot, timeout, or
   incomplete error occurred; and
5. the candidate passed the same evidence, canonical-deduplication, and URL
   gates as full-crawl candidates.

This path is intended for sources such as a public single-job or single-article
resource. It does **not** authorize all WeChat, PDD, SnapshotExecutor, or PATH
C supervisor results by type. Each source must receive a fixture-backed adapter
contract and tests before it is enabled. Until then it appears only as a source
status.

### Application URL proof

Before a pre-review recommendation is persisted, its `apply_url` is validated
by a dedicated deterministic validator. It must:

- parse successfully and be no longer than 2048 characters;
- use only `http` or `https` (reject `javascript:`, `data:`, `file:`,
  `mailto:`, and any unknown scheme);
- contain no username or password, and no loopback, link-local, private,
  multicast, reserved, or literal-IP host;
- have a hostname in the source origin's declared application-host allowlist.

The allowlist may include an adapter-declared third-party ATS host, so it is
not limited to the source's registrable domain. A validation failure creates a
`url_unsafe` source status and never a direct recommendation.

## Integration with PEV and existing review flows

Personalization consumes the output of the PEV post-crawl pipeline after
`CoverageVerifier`, evidence verification, and canonical deduplication. It is
not a new PATH C candidate generator and cannot override PEV coverage. The
existing administrator review queue and `JobPosting` verification state machine
remain unchanged for shared student job-center APIs.

The PEV migration's blocked/manual result taxonomy is mapped into the closed
source-status taxonomy above. New reason mappings must be explicit and tested;
unknown raw errors become `needs_manual_review`, never free-form text.

For v1, the source pool is the latest completed eligible task for each source
resource in the configured discovery retention window. The personalization API
has no arbitrary URL, site, adapter, or crawl-plan parameters. It can only
evaluate that shared pool and record its owner-scoped result.

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

Recommendation listing is paginated. It may return as many eligible canonical
jobs as the user has; v1 does not silently apply the existing generic
`top_n=20` recommendation helper default.

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
- The new pre-review tables are never joined by `list_public_postings`,
  `/jobs`, or `/jobs/{id}`; they never write `review_version`, and they never
  transition a `JobPosting` through its review state machine.

## Test strategy

Unit tests cover preference extension normalization, ownership, version bumps,
exclusion precedence, broad synonym recall, `RelevanceRanker` thresholding,
and ranker-failure rejection. Repository/service tests cover recommendation
ownership, unique delivery, and no cross-user reads.

Pipeline tests prove that only coverage-complete, evidence-backed PEV
candidates or fixture-backed single-resource candidates can enter
personalization; legacy/uncovered candidates and blocked tasks become source
statuses. URL tests cover rejected schemes, credentials, private/IP hosts,
overlong URLs, and adapter-declared third-party ATS hosts. API tests prove a
user cannot read or modify
another user's profile, runs, recommendations, or statuses. Migration tests
cover the new preference columns, user-owned records, canonical-key uniqueness,
and strict `/jobs` isolation.

Regression tests retain `JobRelevanceScore` behavior and cache invalidation for
verified JobPostings, current evidence/coverage/deduplication behavior, and the
no-bypass policy for walls.
