# Personalized Job Discovery v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an authenticated user configure role preferences and receive owner-only, evidence-backed pre-review job recommendations from completed shared discovery tasks, while safely explaining sources that cannot be recommended.

**Architecture:** Extend the existing `UserPreference` and `RelevanceRanker` stack. A new service selects retained shared tasks, applies completeness/evidence/URL/dedup/relevance gates, and persists separate user-scoped delivery records. It never changes `JobPosting`, `JobRelevanceScore`, or the verified-only `/jobs` path.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, MySQL, pytest, Ruff.

## Global Constraints

- Recommendation requires evidence, full PEV coverage or registered `single_source_complete`, URL safety, canonical de-duplication, role exclusions, and score threshold.
- Legacy PATH C is never complete by implication. Do not bypass login, QR login, captcha, authentication, or anti-bot walls.
- Initial v1 direct-recommendation coverage is limited to the four migrated complete-crawl adapters (Moka, Feishu, Inovance, Xiaohongshu). No `single_source_complete` contract is initially registered; WeChat articles, PDD, SnapshotExecutor, Alibaba SPA, and all legacy/PATH C results produce only an owner-scoped status when in the retained source pool.
- Application URLs: at most 2048 chars; only HTTP(S); no credentials, literal IP, loopback, link-local, private, multicast, or reserved host; exact match to source/adapter application-host allowlist.
- `/jobs` and `/jobs/{id}` remain verified-only. New tables never mutate `review_version` or a `JobPosting` status.
- Reads/writes use `current_user.id`; no DTO may expose raw payloads, cookies, tokens, or wall text.
- Runs process existing retained shared tasks only: no URL/site/adapter/crawl-plan request fields, shared cache, scheduled refresh, query coalescing, or automatic application.
- Before Task 1, create a clean isolated worktree and feature branch from this plan commit. The main worktree's user-owned changes to canonical de-duplication and `result_contract.py` must remain untouched; no implementation task stashes, commits, or stages them.

---

## File structure

| File | Responsibility |
| --- | --- |
| `backend/app/domain/personalized_discovery.py` | Closed status/state enums, role normalization/recall, URL validation, safe display copy. |
| `backend/app/services/job_discovery/single_source_proof.py` | Registered single-resource contract and deterministic proof verifier. |
| `backend/app/services/job_discovery/deduplication/canonical_job_deduplicator.py` | Exposes a canonical-key function with the same `_identity_key` semantics as within-run de-duplication. |
| `backend/app/services/job_discovery/worker.py` | Persists completeness provenance in task summaries. |
| `backend/app/db/models.py` | New preference columns and owner-scoped run/delivery/status models. |
| `backend/app/repositories/personalized_discovery.py` | SQL-only task selection and owner-scoped upserts/listing. |
| `backend/app/services/personalized_discovery.py` | Gates, ranking, rate limit, and delivery lifecycle. |
| `backend/app/api/personalized_discovery_schemas.py` | Strict presentation DTOs. |
| `backend/app/api/routes/personalized_discovery.py` | Authenticated HTTP handlers only. |

### Task 1: Add pure safety and relevance contracts

**Files:**
- Create: `backend/app/domain/personalized_discovery.py`
- Create: `tests/unit/test_personalized_discovery_domain.py`

**Interfaces:**
- Produces `SourceStatusReason`, `RecommendationPresentationState`, `normalize_role_terms`, `title_matches_role_recall`, `validate_application_url`, and `source_status_copy`.
- `validate_application_url(raw_url: str | None, allowed_hosts: set[str]) -> ValidatedApplicationUrl | UrlValidationFailure`.

- [ ] **Step 1: Create the isolated implementation worktree.**

Run: `git worktree add -b codex/personalized-discovery-v1 ../personalized-discovery-v1 HEAD`

Expected: a clean sibling worktree on `codex/personalized-discovery-v1`. Perform every remaining task there; do not stash, stage, or commit the main worktree's existing changes.

- [ ] **Step 2: Write failing tests.**

```python
def test_role_terms_are_trimmed_deduplicated_and_nonblank() -> None:
    assert normalize_role_terms([" AI应用开发 ", "ai应用开发", "Agent开发"]) == [
        "AI应用开发", "Agent开发"
    ]
    with pytest.raises(ValueError, match="blank"):
        normalize_role_terms([" "])


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "mailto:a@example.com", "https://u:p@jobs.example.com/a",
    "https://127.0.0.1/a", "https://10.0.0.1/a",
])
def test_url_validator_rejects_unsafe_urls(url: str) -> None:
    assert isinstance(validate_application_url(url, {"jobs.example.com"}), UrlValidationFailure)


def test_broad_recall_keeps_synonym_but_exclusion_wins() -> None:
    assert title_matches_role_recall("LLM Agent Engineer", ["AI应用开发"], ["agent"], [])
    assert not title_matches_role_recall("Agent Engineer", ["AI应用开发"], ["agent"], ["agent"])
```

- [ ] **Step 3: Run the test to verify failure.**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_personalized_discovery_domain.py -q`

Expected: FAIL; module/functions do not exist.

- [ ] **Step 4: Implement the minimal deterministic module.**

```python
class SourceStatusReason(StrEnum):
    LOGIN_REQUIRED = "login_required"
    CAPTCHA = "captcha"
    ANTI_BOT = "anti_bot"
    AUTHENTICATION_REQUIRED = "authentication_required"
    COVERAGE_INCOMPLETE = "coverage_incomplete"
    URL_UNSAFE = "url_unsafe"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"


class RecommendationPresentationState(StrEnum):
    NEW = "new"
    VIEWED = "viewed"
    SAVED = "saved"
    DISMISSED = "dismissed"
    APPLY_CLICKED = "apply_clicked"
```

Use `urllib.parse.urlsplit` and `ipaddress.ip_address`; do not resolve DNS. `source_status_copy` maps only the enum to fixed Chinese text and fixed guidance, never accepts raw upstream text. Cap role lists at 100 terms and each term at 128 characters.

- [ ] **Step 5: Verify.**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_personalized_discovery_domain.py -q; ./.venv/Scripts/python.exe -m ruff check backend/app/domain/personalized_discovery.py tests/unit/test_personalized_discovery_domain.py`

Expected: PASS.

- [ ] **Step 6: Commit.**

```powershell
git add backend/app/domain/personalized_discovery.py tests/unit/test_personalized_discovery_domain.py
git commit -m "feat: add personalized discovery safety contracts"
```

### Task 2: Add models and a reversible migration

**Files:**
- Modify: `backend/app/db/models.py:1207-1290`
- Create: `alembic/versions/<revision>_personalized_job_discovery_v1.py`
- Create: `tests/unit/test_personalized_discovery_models.py`

**Interfaces:**
- `UserPreference` gains JSON `role_synonyms`, JSON `excluded_roles`, and nullable float `personalized_discovery_min_score`.
- Produces `PersonalizedDiscoveryRun`, `PersonalizedDiscoveryRecommendation`, and `UserDiscoverySourceStatus`.

- [ ] **Step 1: Write failing ORM constraint tests.**

```python
def test_canonical_job_key_is_unique_per_user(db_session) -> None:
    _recommendation(db_session, user_id=user.id, canonical_job_key="company:title:url")
    _recommendation(db_session, user_id=user.id, canonical_job_key="company:title:url")
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_canonical_key_is_allowed_for_another_user(db_session) -> None:
    _recommendation(db_session, user_id=user_a.id, canonical_job_key="k")
    _recommendation(db_session, user_id=user_b.id, canonical_job_key="k")
    db_session.flush()
```

- [ ] **Step 2: Run and confirm failure.**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_personalized_discovery_models.py -q`

Expected: FAIL; models/tables do not exist.

- [ ] **Step 3: Implement additive schema.**

```python
class PersonalizedDiscoveryRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    user_id: Mapped[str]  # users.id CASCADE
    preference_version: Mapped[int]
    status: Mapped[str]  # running | succeeded | failed
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    summary_json: Mapped[dict[str, Any] | None]

class PersonalizedDiscoveryRecommendation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    user_id: Mapped[str]; candidate_id: Mapped[str]; task_id: Mapped[str]
    last_run_id: Mapped[str]; canonical_job_key: Mapped[str]
    preference_version: Mapped[int]; relevance_score: Mapped[float]
    relevance_reason: Mapped[str]; matched_signals_json: Mapped[list[str] | None]
    presentation_state: Mapped[RecommendationPresentationState]

class UserDiscoverySourceStatus(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    user_id: Mapped[str]; run_id: Mapped[str]; task_id: Mapped[str]
    source_key: Mapped[str]; safe_source_url: Mapped[str]
    reason_code: Mapped[SourceStatusReason]; display_text: Mapped[str]
    retry_guidance: Mapped[str]
```

Foreign keys: recommendation candidate/task use `RESTRICT`; run/user and all owner-scoped rows use `CASCADE`. `RESTRICT` is intentional: a recommendation must retain its evidence/task trace while it exists. Retention cleanup therefore deletes personalized recommendations/statuses/runs before deleting a source task or candidate. Add unique `(user_id, canonical_job_key)`, unique `(user_id, run_id, task_id, reason_code)`, and user-created listing indexes. Use explicit named enums. `presentation_state` is the latest presentation event, not an append-only interaction history; do not reuse `UserJobInteraction` because its `job_id` foreign key requires a verified `JobPosting`. Downgrade drops new indexes/tables before preference columns.

- [ ] **Step 4: Round-trip the migration.**

Run: `./.venv/Scripts/alembic.exe upgrade head; ./.venv/Scripts/alembic.exe downgrade -1; ./.venv/Scripts/alembic.exe upgrade head`

Expected: all commands succeed.

- [ ] **Step 5: Verify and commit.**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_personalized_discovery_models.py -q`

```powershell
git add backend/app/db/models.py alembic/versions/<revision>_personalized_job_discovery_v1.py tests/unit/test_personalized_discovery_models.py
git commit -m "feat: persist personalized discovery delivery"
```

### Task 3: Extend the existing preference repository/service

**Files:**
- Modify: `backend/app/repositories/preferences.py:18-101`
- Modify: `backend/app/services/preferences_service.py:18-34`
- Create: `tests/unit/test_preferences_service.py`
- Create: `tests/unit/test_relevance_ranker.py`
- Create: `tests/unit/test_recommendation_service.py`

**Interfaces:**
- `set_preferences` accepts and validates the three new fields.
- `get_preferences_summary` returns `role_synonyms`, `excluded_roles`, and `personalized_discovery_min_score` with safe defaults.

- [ ] **Step 1: Write failing preference tests.**

```python
def test_extended_preferences_normalize_and_bump_version(db_session) -> None:
    first = set_preferences(db_session, user.id, desired_roles=["AI应用开发"])
    second = set_preferences(
        db_session, user.id, role_synonyms=["Agent开发", "agent开发"],
        excluded_roles=["销售"], personalized_discovery_min_score=72,
    )
    assert second.version == first.version + 1
    assert get_preferences_summary(db_session, user.id)["role_synonyms"] == ["Agent开发"]

@pytest.mark.parametrize("score", [-1, 100.1])
def test_score_threshold_is_bounded(db_session, score: float) -> None:
    with pytest.raises(ValueError, match="0.*100"):
        set_preferences(db_session, user.id, personalized_discovery_min_score=score)

```

- [ ] **Step 2: Run and confirm failure.**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_preferences_service.py -q`

Expected: FAIL; repository allowlist/summary lacks fields.

- [ ] **Step 3: Implement the extension.**

Normalize lists through Task 1 before repository access. Add the three names to `_PREFERENCE_COLUMNS` and `to_summary`. A profile and `JobRelevanceScore` remain unchanged; the existing relevance ranker simply receives the augmented preferences dictionary.

- [ ] **Step 4: Add characterization tests for existing ranker behavior.**

```python
def test_ranker_failure_returns_zero_score_for_every_candidate() -> None:
    ranked = RelevanceRanker(FailingLLM()).rank([candidate], profile_summary={}, preferences={})
    assert ranked[0].score == 0.0


def test_recommendation_rank_does_not_truncate_candidates() -> None:
    ranked = RecommendationService(FakeRanker()).rank(
        candidates_21, profile_summary={}, preferences={},
    )
    assert len(ranked) == 21
```

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_relevance_ranker.py tests/unit/test_recommendation_service.py -q`

Expected: PASS immediately; these characterize existing behavior and guard the later personalized service from accidentally using `filter_and_sort`.

- [ ] **Step 5: Verify and commit.**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_preferences_service.py tests/unit/test_relevance_ranker.py tests/unit/test_recommendation_service.py -q`

```powershell
git add backend/app/repositories/preferences.py backend/app/services/preferences_service.py tests/unit/test_preferences_service.py tests/unit/test_relevance_ranker.py tests/unit/test_recommendation_service.py
git commit -m "feat: extend user role preferences"
```

### Task 4: Persist both allowed completeness proofs

**Files:**
- Create: `backend/app/services/job_discovery/single_source_proof.py`
- Modify: `backend/app/services/job_discovery/worker.py` (result-summary persistence block)
- Create: `tests/unit/job_discovery/test_single_source_proof.py`
- Modify: `tests/unit/test_job_discovery_worker.py`

**Interfaces:**
- `evaluate_single_source_proof(task, result, executor_type, *, registry: SingleSourceProofRegistry = PRODUCTION_REGISTRY) -> SingleSourceProof | None`.
- Worker summary has `single_source_complete: {"contract_id": str, "evidence_hash": str, "terminal_signal": str, "application_hosts": list[str]} | None`.

- [ ] **Step 1: Write failing proof tests.**

```python
def test_registered_public_single_detail_requires_every_positive_signal() -> None:
    proof = evaluate_single_source_proof(
        task, public_one_detail_result, "adapter", registry=fixture_registry,
    )
    assert proof and proof.terminal_signal == "public_single_detail_complete"

def test_legacy_supervisor_never_gets_single_source_proof() -> None:
    assert evaluate_single_source_proof(
        task, public_one_detail_result, "supervisor", registry=fixture_registry,
    ) is None

def test_worker_serializes_proof(worker, db_session) -> None:
    worker.single_source_proof_registry = fixture_registry
    worker.run_once()
    assert task.result_summary_json["single_source_complete"]["contract_id"] == "public-single-detail-v1"
```

- [ ] **Step 2: Run and confirm failure.**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/job_discovery/test_single_source_proof.py tests/unit/test_job_discovery_worker.py -q`

Expected: FAIL; no registry/provenance exists.

- [ ] **Step 3: Implement the closed contract registry and serialization.**

A registry entry declares adapter id, source URL pattern, exactly one public resource, exact terminal signal, and allowed application hosts. It requires non-empty JD text, one `PageEvidence` SHA-256 hash, no execution error/block/wall, and no pagination continuation. The production registry is empty in v1. Unit tests inject a fixture-only registry to prove the admission mechanism; this does not enable a production source. Register no generic WeChat, PDD, SnapshotExecutor, or PATH C category. Compute/serialize only after current coverage and invariant processing. Keep existing `coverage_verified` semantics untouched.

- [ ] **Step 4: Verify and commit.**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/job_discovery/test_single_source_proof.py tests/unit/test_job_discovery_worker.py tests/unit/job_discovery/test_coverage.py -q`

```powershell
git add backend/app/services/job_discovery/single_source_proof.py backend/app/services/job_discovery/worker.py tests/unit/job_discovery/test_single_source_proof.py tests/unit/test_job_discovery_worker.py
git commit -m "feat: record personalized completeness proofs"
```

### Task 5: Add SQL-only personalized discovery repositories

**Files:**
- Create: `backend/app/repositories/personalized_discovery.py`
- Modify: `backend/app/services/job_discovery/deduplication/canonical_job_deduplicator.py`
- Create: `tests/unit/test_personalized_discovery_repository.py`
- Modify: `tests/unit/job_discovery/test_canonical_job_deduplicator.py`

**Interfaces:**
- `list_latest_retained_tasks(db, *, now, retention_days) -> list[JobDiscoveryTask]`
- `upsert_recommendation(db, *, user_id, candidate_id, task_id, last_run_id, canonical_job_key, preference_version, relevance_score, relevance_reason, matched_signals, presentation_state)` updates the row's candidate/task/run/score fields on its `(user_id, canonical_job_key)` conflict; `upsert_source_status(db, *, user_id, run_id, task_id, source_key, safe_source_url, reason_code)` is idempotent per run/task/reason.
- `list_recommendations_for_user(db, user_id, *, limit, offset)` and `list_statuses_for_user(db, user_id, *, run_id, limit, offset)`.
- `count_runs_for_user_in_window(db, *, user_id, started_at, ended_at) -> int`.
- `canonical_job_key(candidate: NormalizedJobCandidate) -> str` returns `v1:` plus the SHA-256 of canonical JSON for the exact private `_identity_key(candidate)` tuple. It is called only after the service rejects title-only/missing-JD candidates.

- [ ] **Step 1: Write failing repository tests.**

```python
def test_latest_selection_skips_expired_and_superseded_tasks(db_session) -> None:
    assert [t.id for t in list_latest_retained_tasks(db_session, now=NOW, retention_days=30)] == [newest.id]

def test_delivery_upsert_reuses_user_canonical_key(db_session) -> None:
    first = upsert_recommendation(
        db_session, user_id=user.id, candidate_id=candidate.id, task_id=task.id,
        last_run_id=run.id, canonical_job_key="k", preference_version=1,
        relevance_score=80.0, relevance_reason="role match", matched_signals=["AI"],
        presentation_state=RecommendationPresentationState.VIEWED,
    )
    second = upsert_recommendation(
        db_session, user_id=user.id, candidate_id=candidate.id, task_id=task.id,
        last_run_id=run.id, canonical_job_key="k", preference_version=2,
        relevance_score=90.0, relevance_reason="stronger match", matched_signals=["Agent"],
        presentation_state=RecommendationPresentationState.SAVED,
    )
    assert second.id == first.id


def test_cross_source_conflict_repoints_delivery_to_selected_representative(db_session) -> None:
    original = upsert_recommendation(
        db_session, user_id=user.id, candidate_id=older_candidate.id, task_id=older_task.id,
        last_run_id=run.id, canonical_job_key="k", preference_version=1,
        relevance_score=80.0, relevance_reason="match", matched_signals=["AI"],
        presentation_state=RecommendationPresentationState.NEW,
    )
    updated = upsert_recommendation(
        db_session, user_id=user.id, candidate_id=newer_candidate.id, task_id=newer_task.id,
        last_run_id=run.id, canonical_job_key="k", preference_version=1,
        relevance_score=80.0, relevance_reason="match", matched_signals=["AI"],
        presentation_state=RecommendationPresentationState.NEW,
    )
    assert updated.id == original.id
    assert (updated.candidate_id, updated.task_id) == (newer_candidate.id, newer_task.id)

def test_owner_list_excludes_another_users_records(db_session) -> None:
    assert list_recommendations_for_user(db_session, user_a.id, limit=50, offset=0) == [user_a_row]

def test_daily_run_count_is_scoped_to_user_and_window(db_session) -> None:
    assert count_runs_for_user_in_window(
        db_session, user_id=user.id, started_at=TODAY_START, ended_at=TOMORROW_START,
    ) == 2

def test_public_canonical_key_matches_existing_identity_semantics() -> None:
    first = _full_jd("AI工程师", "某公司", "职责", "要求", "上海、深圳")
    second = _full_jd("AI工程师", "某公司", "职责", "要求", "深圳、上海")
    assert canonical_job_key(first) == canonical_job_key(second)
```

- [ ] **Step 2: Run and confirm failure.**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_personalized_discovery_repository.py -q`

Expected: FAIL; repository is absent.

- [ ] **Step 3: Implement SQL only.**

Select latest terminal task inside retention with `row_number() over (partition by source_id, external_record_id order by finished_at desc, created_at desc)`. Count daily runs with `started_at >= local-day UTC start AND started_at < next local-day UTC start` and the supplied `user_id`. On recommendation conflict, update candidate/task/last_run/preference version/score/reason/signals but preserve an existing `dismissed` presentation state; otherwise use the incoming presentation state. The repository does not decide the representative, wall mapping, coverage, URL safety, relevance, or canonical-key construction. Every list/update predicate includes `user_id`.

Expose the identity key in the existing deduplicator rather than reproducing its private location normalization in the new service:

```python
def canonical_job_key(candidate: NormalizedJobCandidate) -> str:
    payload = json.dumps(_identity_key(candidate), ensure_ascii=False, separators=(",", ":"))
    return f"v1:{hashlib.sha256(payload.encode("utf-8")).hexdigest()}"
```

Export it from `deduplication/__init__.py`. The isolated worktree is clean, so add the function/import/export/test as one normal focused commit. The main worktree's current `_loc_key` and title-echo edits are out of scope and remain untouched.

- [ ] **Step 4: Verify and commit.**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_personalized_discovery_repository.py -q`

```powershell
git add backend/app/repositories/personalized_discovery.py tests/unit/test_personalized_discovery_repository.py backend/app/services/job_discovery/deduplication/canonical_job_deduplicator.py backend/app/services/job_discovery/deduplication/__init__.py tests/unit/job_discovery/test_canonical_job_deduplicator.py
git commit -m "feat: add personalized discovery repositories"
```

### Task 6: Implement eligibility, ranking, and delivery service

**Files:**
- Create: `backend/app/services/personalized_discovery.py`
- Modify: `backend/app/config.py`
- Create: `tests/unit/test_personalized_discovery_service.py`

**Interfaces:**
- `PersonalizedDiscoveryService.run(db, *, user_id: str, now: datetime) -> PersonalizedDiscoveryRun`
- Config: `personalized_discovery_retention_days=30 (1..365)`, `personalized_discovery_runs_per_day=5 (1..50)`.

- [ ] **Step 1: Write failing gate tests.**

```python
def test_only_complete_evidenced_safe_candidate_is_delivered(db_session, ranker, user) -> None:
    service(ranker).run(db_session, user_id=user.id, now=NOW)
    assert _titles(db_session, user.id) == ["AI Agent 应用开发工程师"]

def test_wall_and_incomplete_task_become_status_not_recommendation(db_session, ranker, user) -> None:
    service(ranker).run(db_session, user_id=user.id, now=NOW)
    assert _titles(db_session, user.id) == []
    assert _status_codes(db_session, user.id) == ["login_required", "coverage_incomplete"]

def test_ranker_failure_missing_jd_and_unsafe_url_never_deliver(db_session, failing_ranker, user) -> None:
    service(failing_ranker).run(db_session, user_id=user.id, now=NOW)
    assert _titles(db_session, user.id) == []

def test_run_has_no_implicit_top_twenty_cap(db_session, ranker, user) -> None:
    service(ranker).run(db_session, user_id=user.id, now=NOW)
    assert len(_recommendations(db_session, user.id)) == 21


def test_cross_source_duplicates_rank_once_and_select_newest_task_candidate(db_session, ranker, user) -> None:
    service(ranker).run(db_session, user_id=user.id, now=NOW)
    assert ranker.received_titles.count("AI Agent 应用开发工程师") == 1
    row = _recommendations(db_session, user.id)[0]
    assert (row.candidate_id, row.task_id) == (newer_candidate.id, newer_task.id)
```

- [ ] **Step 2: Run and confirm failure.**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_personalized_discovery_service.py -q`

Expected: FAIL; service is absent.

- [ ] **Step 3: Implement the exact service pipeline.**

```text
latest retained task per source resource
 -> terminal/wall mapping or full-coverage/registered-single-source proof
 -> require candidate JD + evidence; URL validation against proof hosts
 -> `canonical_job_key` from the existing CanonicalJobDeduplicator public API
 -> group by canonical key; select one deterministic representative
 -> broad title/category/synonym recall (excluded role wins)
 -> RecommendationService.rank / RelevanceRanker
 -> score >= user threshold
 -> owner-scoped recommendation upsert
```

Keep canonical de-duplication before broad recall: this is the approved gate order and ensures one canonical candidate receives one score/delivery decision. For each key, choose the representative with the newest non-null task `finished_at`; break an equal timestamp by candidate `created_at` descending, then candidate UUID ascending. Rank only these representatives. The repository upsert receives that selected candidate/task and updates the existing delivery row to it; it is never allowed to choose a last writer independently. Do not call `RecommendationService.filter_and_sort` because its default is `top_n=20`. Ranker error or malformed score is `0.0`, never a positive delivery. Map actual `DiscoveryBlockReason` members as follows: `login_required -> login_required`, `captcha -> captcha`, `anti_bot -> anti_bot`, `permission_denied -> authentication_required`, `invalid_url -> url_unsafe`, `wechat_unavailable|timeout|budget_exceeded|parse_failed|unknown -> needs_manual_review`; a task without a more-specific reason and without either proof is `coverage_incomplete`. Any future non-enum worker string also maps to `needs_manual_review`.

Before creating a running row, call `count_runs_for_user_in_window` with the caller's current China-local calendar-day UTC boundaries; reject when the count is `>= personalized_discovery_runs_per_day`. Then create a running row, rank, and finalize counts/timestamp. A failure marks only that run `failed` with a safe code and raises a typed service error.

- [ ] **Step 4: Verify and commit.**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_personalized_discovery_service.py tests/unit/test_relevance_ranker.py tests/unit/test_recommendation_service.py tests/unit/job_discovery -q`

```powershell
git add backend/app/services/personalized_discovery.py backend/app/config.py tests/unit/test_personalized_discovery_service.py
git commit -m "feat: run personalized job discovery"
```

### Task 7: Add strict owner-scoped APIs

**Files:**
- Create: `backend/app/api/personalized_discovery_schemas.py`
- Create: `backend/app/api/routes/personalized_discovery.py`
- Modify: `backend/app/api/router.py:23-37`
- Create: `tests/api/test_personalized_discovery.py`

**Interfaces:**
- `GET/PATCH/DELETE /personalized-discovery/preferences`
- `POST /personalized-discovery/runs`
- `GET /personalized-discovery/recommendations?limit=&offset=`
- `GET /personalized-discovery/source-statuses?run_id=&limit=&offset=`
- `POST /personalized-discovery/recommendations/{id}/interactions`

- [ ] **Step 1: Write failing endpoint tests.**

```python
def test_preferences_are_owned_and_extended(client, auth_headers) -> None:
    response = client.patch("/api/personalized-discovery/preferences", headers=auth_headers, json={
        "desired_roles": ["AI应用开发"], "role_synonyms": ["Agent开发"],
        "excluded_roles": ["销售"], "personalized_discovery_min_score": 70,
    })
    assert response.status_code == 200
    assert response.json()["role_synonyms"] == ["Agent开发"]

def test_other_user_cannot_change_delivery_state(client, user_b_headers, recommendation) -> None:
    response = client.post(
        f"/api/personalized-discovery/recommendations/{recommendation.id}/interactions",
        headers=user_b_headers, json={"state": "dismissed"},
    )
    assert response.status_code == 404

def test_run_rejects_crawler_inputs(client, auth_headers) -> None:
    assert client.post("/api/personalized-discovery/runs", headers=auth_headers,
                       json={"url": "https://example.com"}).status_code == 422
```

- [ ] **Step 2: Run and confirm failure.**

Run: `./.venv/Scripts/python.exe -m pytest tests/api/test_personalized_discovery.py -q`

Expected: FAIL; routes are absent.

- [ ] **Step 3: Implement schemas and routes.**

DTOs use `extra="forbid"`, role limits from Task 1, threshold 0..100, pagination `limit 1..100`/non-negative offset. Card response only includes title, company, locations, safe apply URL, score, reason, signals, evidence links, fixed label `自动发现，建议自行确认`, state, and timestamps. No JD/raw task/error/evidence excerpt output.

The run request is an empty model; routes call only the services, never SQL. Interaction accepts exactly `viewed|saved|dismissed|apply_clicked`. No route has user id input; missing/not-owned item is 404.

- [ ] **Step 4: Verify and commit.**

Run: `./.venv/Scripts/python.exe -m pytest tests/api/test_personalized_discovery.py -q`

```powershell
git add backend/app/api/personalized_discovery_schemas.py backend/app/api/routes/personalized_discovery.py backend/app/api/router.py tests/api/test_personalized_discovery.py
git commit -m "feat: expose personalized discovery APIs"
```

### Task 8: Lock down public-job isolation and operations

**Files:**
- Create: `tests/api/test_jobs.py`
- Create: `tests/integration/test_personalized_discovery_pipeline.py`
- Modify: `docs/job-discovery-agent-operations.md`

**Interfaces:**
- Regression coverage proves a pending-review candidate can be personalized but never appears in public jobs output.

- [ ] **Step 1: Write failing cross-boundary tests.**

```python
def test_pre_review_delivery_never_leaks_into_jobs(client, auth_headers, recommendation) -> None:
    assert recommendation.candidate.status.value == "pending_review"
    assert recommendation.candidate.title not in _titles(client.get("/api/jobs", headers=auth_headers))
    assert client.get(f"/api/jobs/{recommendation.candidate_id}", headers=auth_headers).status_code == 404

def test_source_status_has_closed_copy_not_raw_wall_data(client, auth_headers, wall_task) -> None:
    body = client.get("/api/personalized-discovery/source-statuses", headers=auth_headers).json()
    assert body["items"][0]["reason_code"] == "captcha"
    assert "cf-ray" not in str(body)
    assert "cookie" not in str(body).lower()
```

- [ ] **Step 2: Run and confirm failure before full wiring.**

Run: `./.venv/Scripts/python.exe -m pytest tests/integration/test_personalized_discovery_pipeline.py tests/api/test_jobs.py -q`

Expected: FAIL until API/service wiring is complete.

- [ ] **Step 3: Document operations.**

Document the initial four-adapter coverage limitation, the two proofs, the single-resource registration checklist (fixture test, evidence hash, exact terminal signal, ATS host allowlist), status codes, user daily limit, and rollback by disabling the personalized endpoint feature flag if introduced. Document the `RESTRICT` retention order: delete personalized delivery rows before deleting candidates/tasks. State that a user may inspect a blocked source manually but the worker never bypasses a wall.

- [ ] **Step 4: Run final verification.**

```powershell
./.venv/Scripts/python.exe -m pytest tests/unit/test_personalized_discovery_domain.py tests/unit/test_personalized_discovery_models.py tests/unit/test_preferences_service.py tests/unit/test_personalized_discovery_repository.py tests/unit/test_personalized_discovery_service.py tests/unit/job_discovery tests/api/test_personalized_discovery.py tests/api/test_jobs.py tests/integration/test_personalized_discovery_pipeline.py -q
./.venv/Scripts/python.exe -m ruff check backend/app/domain/personalized_discovery.py backend/app/services/personalized_discovery.py backend/app/services/job_discovery/single_source_proof.py backend/app/repositories/personalized_discovery.py backend/app/api/personalized_discovery_schemas.py backend/app/api/routes/personalized_discovery.py
```

Expected: all pass. Do not run live site tests unless their explicit environment gate is set.

- [ ] **Step 5: Commit final coverage and documentation.**

```powershell
git add tests/api/test_jobs.py tests/integration/test_personalized_discovery_pipeline.py docs/job-discovery-agent-operations.md
git commit -m "test: verify personalized discovery safety boundaries"
```

## Self-review

**Spec coverage:** Tasks 1/4/6 implement all gates and both approved completeness proofs. The initial source scope is intentionally the four migrated complete-crawl adapters; Task 4 provides the separately tested expansion path, but registers no single-resource source in v1. Tasks 2/5/6 implement separate owner records, a public shared canonical key, deterministic cross-source representative selection before ranking, conflict repointing, and a user/day run-count query. Task 3 reuses the current preference stack. Task 7 supplies user-only APIs without user-controlled crawling. Task 8 proves verified-job isolation, no raw wall leakage, and the retention cleanup requirement.

**Placeholder scan:** The Alembic revision placeholder is the generated filename only; implementation must replace it with Alembic's generated revision id. Table names, fields, constraints, interfaces, tests, and commands are fixed. The only future product expansion deliberately not enabled in v1 is registration of a source-specific `single_source_complete` contract, whose admission criteria are fixed in Task 4.

**Type consistency:** Domain enums precede ORM use; worker writes the stable `single_source_complete` summary consumed by the service; pre-review delivery uses its own models and never `JobRelevanceScore`.
