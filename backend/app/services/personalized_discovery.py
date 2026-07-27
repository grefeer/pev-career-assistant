"""Personalized discovery v1 service: gates, ranking, rate limit, delivery.

Reads retained shared ``JobDiscoveryTask`` rows produced by the job-discovery
worker, applies the closed admission gates (wall mapping OR full-coverage /
registered single-source proof, then candidate JD + evidence + URL safety +
canonical de-dup + broad recall + score threshold), and persists
owner-scoped ``PersonalizedDiscoveryRecommendation`` rows. Sources that cannot
be recommended get an owner-scoped ``UserDiscoverySourceStatus`` with a closed
reason code and fixed display copy - never raw wall text.

This channel is PRE-REVIEW and entirely separate from the verified-only
``/jobs`` path: it never mutates ``JobPosting``, ``JobRelevanceScore``, or
``review_version``. Recommendations are labeled "auto-discovered, confirm
yourself" via the ``NEW`` presentation state.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.config import Settings, get_settings
from backend.app.db.models import (
    DiscoveredJobCandidate,
    DiscoveredJobCandidateStatus,
    DiscoveryBlockReason,
    JobDiscoveryEvidence,
    JobDiscoveryTask,
    PersonalizedDiscoveryRun,
)
from backend.app.domain.personalized_discovery import (
    RecommendationPresentationState,
    SourceStatusReason,
    UrlValidationFailure,
    ValidatedApplicationUrl,
    normalize_role_terms,
    title_matches_role_recall,
    validate_application_url,
)
from backend.app.repositories import personalized_discovery as discovery_repo
from backend.app.repositories import preferences as preferences_repo
from backend.app.services.job_discovery.deduplication import canonical_job_key
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate
from backend.app.services.recommendation_service import RecommendationService
from backend.app.services.relevance.relevance_ranker import (
    RelevanceRanker,
    build_profile_summary,
)

logger = logging.getLogger(__name__)

# China Standard Time offset (UTC+8). Daily-run accounting uses the China
# calendar day expressed as UTC boundaries so the window is unambiguous.
_CST_OFFSET = timezone(timedelta(hours=8))

# Wall / non-completion mapping from the discovery block reason to the closed
# owner-facing status code. Any future non-enum worker string falls through to
# NEEDS_MANUAL_REVIEW (see :func:`_block_reason_to_status`).
_BLOCK_REASON_MAP: dict[DiscoveryBlockReason, SourceStatusReason] = {
    DiscoveryBlockReason.login_required: SourceStatusReason.LOGIN_REQUIRED,
    DiscoveryBlockReason.captcha: SourceStatusReason.CAPTCHA,
    DiscoveryBlockReason.anti_bot: SourceStatusReason.ANTI_BOT,
    DiscoveryBlockReason.permission_denied: SourceStatusReason.AUTHENTICATION_REQUIRED,
    DiscoveryBlockReason.invalid_url: SourceStatusReason.URL_UNSAFE,
    DiscoveryBlockReason.wechat_unavailable: SourceStatusReason.NEEDS_MANUAL_REVIEW,
    DiscoveryBlockReason.timeout: SourceStatusReason.NEEDS_MANUAL_REVIEW,
    DiscoveryBlockReason.budget_exceeded: SourceStatusReason.NEEDS_MANUAL_REVIEW,
    DiscoveryBlockReason.parse_failed: SourceStatusReason.NEEDS_MANUAL_REVIEW,
    DiscoveryBlockReason.unknown: SourceStatusReason.NEEDS_MANUAL_REVIEW,
}

# Candidate statuses that may surface as pre-review recommendations. Rejected /
# merged candidates are excluded (a rejected candidate is not deliverable).
_DELIVERABLE_CANDIDATE_STATUSES = frozenset(
    {
        DiscoveredJobCandidateStatus.pending_review,
        DiscoveredJobCandidateStatus.approved,
    }
)

_SAFE_SOURCE_PLACEHOLDER = "（来源链接不可用）"


class PersonalizedDiscoveryError(Exception):
    """Raised when a personalized discovery run fails."""


class PersonalizedDiscoveryRateLimitError(PersonalizedDiscoveryError):
    """Raised when the user has exceeded the daily run limit."""


@dataclass
class _Representative:
    """One candidate that passed every admission gate, awaiting ranking."""

    candidate: DiscoveredJobCandidate
    task: JobDiscoveryTask
    canonical_key: str
    validated_url: ValidatedApplicationUrl


@dataclass
class _RunCounts:
    task_count: int = 0
    status_count: int = 0
    candidate_pool: int = 0
    recommendation_count: int = 0
    statuses: list[SourceStatusReason] = field(default_factory=list)


@dataclass(frozen=True)
class PreferencesView:
    """DTO-ready view of a user's personalized-discovery preference slice."""

    desired_roles: list[str]
    role_synonyms: list[str]
    excluded_roles: list[str]
    personalized_discovery_min_score: float | None
    version: int


@dataclass(frozen=True)
class EvidenceLink:
    """A single evidence reference on a recommendation card (URL + type only).

    Never carries raw page text, OCR output, or a content hash to the API.
    """

    url: str | None
    evidence_type: str | None


@dataclass(frozen=True)
class RecommendationView:
    """DTO-ready view of one delivered recommendation (owner-scoped)."""

    id: str
    candidate_id: str
    task_id: str
    last_run_id: str
    title: str | None
    company_name: str | None
    locations: list[str]
    apply_url: str | None
    relevance_score: float
    relevance_reason: str | None
    matched_signals: list[str]
    presentation_state: RecommendationPresentationState
    created_at: datetime
    updated_at: datetime
    evidence_links: list[EvidenceLink]


@dataclass(frozen=True)
class SourceStatusView:
    """DTO-ready view of one source that could not be recommended."""

    id: str
    run_id: str
    task_id: str
    source_key: str | None
    safe_source_url: str
    reason_code: SourceStatusReason
    display_text: str
    retry_guidance: str
    created_at: datetime


def _block_reason_to_status(reason: object) -> SourceStatusReason:
    """Map a (possibly unknown) block reason to a closed status code."""
    if isinstance(reason, DiscoveryBlockReason):
        return _BLOCK_REASON_MAP.get(reason, SourceStatusReason.NEEDS_MANUAL_REVIEW)
    return SourceStatusReason.NEEDS_MANUAL_REVIEW


def _china_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` boundaries of the China calendar day containing ``now``."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_cst = now.astimezone(_CST_OFFSET)
    day_start_cst = now_cst.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_cst = day_start_cst + timedelta(days=1)
    return (
        day_start_cst.astimezone(timezone.utc),
        day_end_cst.astimezone(timezone.utc),
    )


def _to_normalized(candidate: DiscoveredJobCandidate) -> NormalizedJobCandidate:
    """Map a persisted candidate row to the dataclass the ranker/deduper use."""
    return NormalizedJobCandidate(
        title=candidate.title,
        company_name=candidate.company_name,
        department=candidate.department,
        description_text=candidate.description_text or "",
        responsibilities=candidate.responsibilities or "",
        requirements=candidate.requirements or "",
        locations=list(candidate.locations_json or []),
        recruitment_types=list(candidate.recruitment_types_json or []),
        industries=list(candidate.industries_json or []),
        apply_url=candidate.apply_url,
        application_channel_json=candidate.application_channel_json,
        deadline_text=candidate.deadline_text,
        referral_code=candidate.referral_code,
        confidence=candidate.confidence or 0.0,
        evidence_refs=list(candidate.evidence_refs_json or []),
        normalization_warnings=list(candidate.normalization_warnings_json or []),
    )


def _has_jd_body(candidate: DiscoveredJobCandidate) -> bool:
    return bool(
        (candidate.responsibilities or "").strip()
        or (candidate.requirements or "").strip()
    )


def _allowed_hosts_for(task: JobDiscoveryTask, proof: dict | None) -> set[str]:
    """Application-host allowlist for URL validation = source host ∪ proof hosts."""
    hosts: set[str] = set()
    src_host = (urlsplit(task.source_url or "").hostname or "").lower()
    if src_host:
        hosts.add(src_host)
    if isinstance(proof, dict):
        for host in proof.get("application_hosts") or []:
            if isinstance(host, str) and host:
                hosts.add(host.lower())
    return hosts


def _safe_source_url(task: JobDiscoveryTask) -> str:
    """Display-safe source URL: validate structure, fall back to a placeholder."""
    host = (urlsplit(task.source_url or "").hostname or "").lower()
    result = validate_application_url(task.source_url, {host} if host else set())
    if isinstance(result, ValidatedApplicationUrl):
        return result.url
    return _SAFE_SOURCE_PLACEHOLDER


def _select_representative(members: list[_Representative]) -> _Representative:
    """Newest task ``finished_at``; tie: candidate ``created_at`` desc; then id asc.

    Applied as stable sorts from least- to most-significant key. Datetimes are
    normalized to naive-UTC before comparison so a tz-aware (Python default)
    value never collides with a tz-naive value refreshed from the DB - the two
    can coexist on SQLite (whose ``DateTime(timezone=True)`` is a storage no-op)
    and even on MySQL when worker-set ``finished_at`` mixes with DB ``created_at``.
    """
    by_id = sorted(members, key=lambda r: r.candidate.id)
    by_created_desc = sorted(
        by_id, key=lambda r: _as_naive_utc(r.candidate.created_at), reverse=True
    )
    by_finished_desc = sorted(
        by_created_desc, key=lambda r: _as_naive_utc(r.task.finished_at), reverse=True
    )
    return by_finished_desc[0]


def _as_naive_utc(dt: datetime | None) -> datetime:
    """Coerce to a naive-UTC datetime for safe comparison; ``None`` sorts oldest."""
    if dt is None:
        return datetime.min.replace(tzinfo=None)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class PersonalizedDiscoveryService:
    """Run personalized discovery for one user against retained shared tasks."""

    def __init__(
        self,
        ranker: RelevanceRanker,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.ranker = ranker
        self.settings = settings or get_settings()
        self.recommendation_service = RecommendationService(ranker)

    # ─── Preferences (API-facing) ─────────────────────────────────────────

    def get_preferences(self, db: Session, *, user_id: str) -> PreferencesView:
        pref = preferences_repo.get_for_user(db, user_id)
        return self._pref_view(pref)

    def update_preferences(
        self,
        db: Session,
        *,
        user_id: str,
        desired_roles: list[str] | None = None,
        role_synonyms: list[str] | None = None,
        excluded_roles: list[str] | None = None,
        personalized_discovery_min_score: float | None = None,
        clear_threshold: bool = False,
    ) -> PreferencesView:
        """Extend the preference slice; provided lists replace, ``None`` skips.

        Role terms are validated by the domain normalizer (blank -> ValueError,
        which the API maps to 422). ``personalized_discovery_min_score`` is set
        when a value is supplied; pass ``clear_threshold=True`` to null it.
        """
        changes: dict[str, Any] = {}
        if desired_roles is not None:
            changes["desired_roles"] = normalize_role_terms(desired_roles)
        if role_synonyms is not None:
            changes["role_synonyms"] = normalize_role_terms(role_synonyms)
        if excluded_roles is not None:
            changes["excluded_roles"] = normalize_role_terms(excluded_roles)
        if personalized_discovery_min_score is not None:
            changes["personalized_discovery_min_score"] = (
                personalized_discovery_min_score
            )
        elif clear_threshold:
            changes["personalized_discovery_min_score"] = None
        pref = preferences_repo.upsert(db, user_id, **changes)
        return self._pref_view(pref)

    def clear_preferences(self, db: Session, *, user_id: str) -> None:
        """Delete the preference row so the next read returns defaults."""
        preferences_repo.delete_for_user(db, user_id)

    @staticmethod
    def _pref_view(pref: object | None) -> PreferencesView:
        if pref is None:
            return PreferencesView(
                desired_roles=[],
                role_synonyms=[],
                excluded_roles=[],
                personalized_discovery_min_score=None,
                version=0,
            )
        return PreferencesView(
            desired_roles=list(pref.desired_roles or []),
            role_synonyms=list(pref.role_synonyms or []),
            excluded_roles=list(pref.excluded_roles or []),
            personalized_discovery_min_score=pref.personalized_discovery_min_score,
            version=int(pref.version or 0),
        )

    def run(
        self,
        db: Session,
        *,
        user_id: str,
        now: datetime,
        profile_summary: dict[str, Any] | None = None,
    ) -> PersonalizedDiscoveryRun:
        """Execute one personalized discovery run for ``user_id``.

        ``profile_summary`` is the ranker's view of the user's resume; when
        omitted a minimal summary is used so ranking degrades to
        preferences-only. Returns the persisted ``PersonalizedDiscoveryRun``.
        """
        pref = preferences_repo.get_for_user(db, user_id)
        prefs = preferences_repo.to_summary(pref)
        profile = profile_summary or build_profile_summary(None, [])
        counts = _RunCounts()

        # Rate limit: at most ``runs_per_day`` user-scoped runs per China day.
        day_start, day_end = _china_day_bounds(now)
        existing = discovery_repo.count_runs_for_user_in_window(
            db, user_id=user_id, started_at=day_start, ended_at=day_end
        )
        if existing >= self.settings.personalized_discovery_runs_per_day:
            raise PersonalizedDiscoveryRateLimitError(
                f"daily run limit reached for user {user_id}"
            )

        run = PersonalizedDiscoveryRun(
            user_id=user_id,
            preference_version=int(prefs.get("version") or 0),
            status="running",
            started_at=now,
        )
        db.add(run)
        db.flush()

        try:
            # SAVEPOINT: a ranker/pipeline error rolls back only the partial
            # recommendation/status inserts made during _execute, leaving the
            # run row (and the caller's prior transaction state) intact so it
            # can be marked ``failed``. Without this, a rollback() would nuke
            # the whole transaction and the run could not be persisted as failed.
            with db.begin_nested():
                self._execute(
                    db,
                    run=run,
                    user_id=user_id,
                    prefs=prefs,
                    profile=profile,
                    now=now,
                    counts=counts,
                )
            run.status = "succeeded"
            run.finished_at = now
            run.summary_json = self._summary(counts)
            db.flush()
            return run
        except Exception as exc:
            # begin_nested() already rolled back to the savepoint; the run row
            # and all prior transaction state survive. Mark the run failed.
            run.status = "failed"
            run.finished_at = now
            run.summary_json = {"error": "internal_error"}
            db.flush()
            logger.warning("personalized discovery run %s failed: %s", run.id, exc)
            raise PersonalizedDiscoveryError(str(exc)) from exc

    # ─── Pipeline ─────────────────────────────────────────────────────────

    def _execute(
        self,
        db: Session,
        *,
        run: PersonalizedDiscoveryRun,
        user_id: str,
        prefs: dict[str, Any],
        profile: dict[str, Any],
        now: datetime,
        counts: _RunCounts,
    ) -> None:
        tasks = discovery_repo.list_latest_retained_tasks(
            db,
            now=now,
            retention_days=self.settings.personalized_discovery_retention_days,
        )
        counts.task_count = len(tasks)
        if not tasks:
            return

        # Batch-load evidence presence per task (one query) so the per-candidate
        # evidence gate does not N+1.
        evidence_by_task = self._evidence_presence(db, [t.id for t in tasks])

        representatives: list[_Representative] = []
        for task in tasks:
            admitted = self._admit_task(task)
            if admitted is None:
                # Eligible: proceed to candidate gates.
                pass
            else:
                # Wall or no-proof -> owner-scoped status, no recommendations.
                self._record_status(
                    db, run=run, user_id=user_id, task=task, reason=admitted, counts=counts
                )
                continue

            task_has_evidence = evidence_by_task.get(task.id, False)
            candidates = self._load_candidates(db, task)
            for candidate in candidates:
                if not _has_jd_body(candidate):
                    continue  # title-only / missing-JD: never delivered
                if not (candidate.evidence_refs_json or task_has_evidence):
                    continue  # not evidence-backed
                proof = self._proof_dict(task)
                allowed = _allowed_hosts_for(task, proof)
                url_result = validate_application_url(candidate.apply_url, allowed)
                if isinstance(url_result, UrlValidationFailure):
                    continue  # URL unsafe: exclude candidate (no status)
                key = canonical_job_key(_to_normalized(candidate))
                representatives.append(
                    _Representative(
                        candidate=candidate,
                        task=task,
                        canonical_key=key,
                        validated_url=url_result,
                    )
                )

        counts.candidate_pool = len(representatives)
        if not representatives:
            return

        # Group by canonical key; select one deterministic representative.
        groups: dict[str, list[_Representative]] = {}
        for rep in representatives:
            groups.setdefault(rep.canonical_key, []).append(rep)
        selected = [_select_representative(members) for members in groups.values()]

        # Broad title/category/synonym recall (excluded role wins).
        desired = list(prefs.get("desired_roles") or [])
        synonyms = list(prefs.get("role_synonyms") or [])
        excluded = list(prefs.get("excluded_roles") or [])
        recalled = [
            rep
            for rep in selected
            if title_matches_role_recall(
                rep.candidate.title, desired, synonyms, excluded
            )
        ]
        if not recalled:
            return

        # Rank (no implicit top-N cap; filter_and_sort is intentionally unused).
        ranked = self.recommendation_service.rank(
            [_to_normalized(rep.candidate) for rep in recalled],
            profile_summary=profile,
            preferences=prefs,
        )
        by_index = {r.index: r for r in ranked}

        threshold = prefs.get("personalized_discovery_min_score")
        threshold = float(threshold) if threshold is not None else 0.0

        for i, rep in enumerate(recalled):
            scored = by_index.get(i)
            if scored is None:
                continue
            # Ranker error / malformed score -> 0.0, never a positive delivery.
            if scored.score <= 0.0:
                continue
            if scored.score < threshold:
                continue
            discovery_repo.upsert_recommendation(
                db,
                user_id=user_id,
                candidate_id=rep.candidate.id,
                task_id=rep.task.id,
                last_run_id=run.id,
                canonical_job_key=rep.canonical_key,
                preference_version=int(prefs.get("version") or 0),
                relevance_score=float(scored.score),
                relevance_reason=scored.reason,
                matched_signals=list(scored.matched_signals or []),
                presentation_state=RecommendationPresentationState.NEW,
            )
            counts.recommendation_count += 1

    # ─── Task admission ──────────────────────────────────────────────────

    def _admit_task(
        self, task: JobDiscoveryTask
    ) -> SourceStatusReason | None:
        """Return a status code to record, or ``None`` when the task is eligible.

        Eligible = no wall AND (full coverage OR registered single-source proof).

        v1.1 provisional tier: when ``personalized_discovery_allow_provisional``
        is enabled, a task that lacks coverage proof is still admitted as a
        *provisional* set (the caller labels its recommendations "覆盖未核验，
        建议自行确认") rather than hard-blocked. A wall (block_reason) always
        wins and is never admitted. Default off preserves the strict v1 gate.
        """
        if task.block_reason is not None:
            return _block_reason_to_status(task.block_reason)
        summary = task.result_summary_json or {}
        coverage_ok = bool(summary.get("coverage_verified"))
        proof = summary.get("single_source_complete")
        if coverage_ok or proof:
            return None  # eligible (coverage-verified)
        if self.settings.personalized_discovery_allow_provisional:
            return None  # eligible (provisional - coverage not verified)
        return SourceStatusReason.COVERAGE_INCOMPLETE

    def _proof_dict(self, task: JobDiscoveryTask) -> dict | None:
        summary = task.result_summary_json or {}
        proof = summary.get("single_source_complete")
        return proof if isinstance(proof, dict) else None

    # ─── DB helpers ───────────────────────────────────────────────────────

    def _evidence_presence(
        self, db: Session, task_ids: list[str]
    ) -> dict[str, bool]:
        if not task_ids:
            return {}
        stmt = (
            select(JobDiscoveryEvidence.task_id, func.count())
            .where(
                JobDiscoveryEvidence.task_id.in_(task_ids),
                JobDiscoveryEvidence.content_hash.is_not(None),
                JobDiscoveryEvidence.content_hash != "",
            )
            .group_by(JobDiscoveryEvidence.task_id)
        )
        present = {tid for tid, _ in db.execute(stmt).all()}
        return {tid: (tid in present) for tid in task_ids}

    def _load_candidates(
        self, db: Session, task: JobDiscoveryTask
    ) -> Sequence[DiscoveredJobCandidate]:
        stmt = (
            select(DiscoveredJobCandidate)
            .where(
                DiscoveredJobCandidate.task_id == task.id,
                DiscoveredJobCandidate.status.in_(_DELIVERABLE_CANDIDATE_STATUSES),
            )
            .order_by(DiscoveredJobCandidate.created_at)
        )
        return db.scalars(stmt).all()

    def _record_status(
        self,
        db: Session,
        *,
        run: PersonalizedDiscoveryRun,
        user_id: str,
        task: JobDiscoveryTask,
        reason: SourceStatusReason,
        counts: _RunCounts,
    ) -> None:
        discovery_repo.upsert_source_status(
            db,
            user_id=user_id,
            run_id=run.id,
            task_id=task.id,
            source_key=task.source_key,
            safe_source_url=_safe_source_url(task),
            reason_code=reason,
        )
        counts.status_count += 1
        counts.statuses.append(reason)

    def _summary(self, counts: _RunCounts) -> dict[str, Any]:
        return {
            "task_count": counts.task_count,
            "status_count": counts.status_count,
            "candidate_pool": counts.candidate_pool,
            "recommendation_count": counts.recommendation_count,
            "statuses": [s.value for s in counts.statuses],
        }

    # ─── Reads + interaction (API-facing) ─────────────────────────────────

    def list_recommendations(
        self,
        db: Session,
        *,
        user_id: str,
        limit: int,
        offset: int,
    ) -> list[RecommendationView]:
        """Owner-scoped recommendation cards, highest relevance first.

        Loads candidates/tasks in two batched queries (no N+1) and re-validates
        each stored apply URL against its source host before display.
        """
        rows = list(
            discovery_repo.list_recommendations_for_user(
                db, user_id, limit=limit, offset=offset
            )
        )
        return self._build_recommendation_views(db, rows)

    def list_source_statuses(
        self,
        db: Session,
        *,
        user_id: str,
        run_id: str,
        limit: int,
        offset: int,
    ) -> list[SourceStatusView]:
        """Owner-scoped source statuses for one run."""
        rows = discovery_repo.list_statuses_for_user(
            db, user_id, run_id=run_id, limit=limit, offset=offset
        )
        return [
            SourceStatusView(
                id=r.id,
                run_id=r.run_id,
                task_id=r.task_id,
                source_key=r.source_key,
                safe_source_url=r.safe_source_url or _SAFE_SOURCE_PLACEHOLDER,
                reason_code=SourceStatusReason(r.reason_code),
                display_text=r.display_text,
                retry_guidance=r.retry_guidance,
                created_at=r.created_at,
            )
            for r in rows
        ]

    def record_interaction(
        self,
        db: Session,
        *,
        user_id: str,
        recommendation_id: str,
        state: RecommendationPresentationState,
    ) -> RecommendationView | None:
        """Set the presentation state on an owned recommendation.

        Returns ``None`` when the recommendation is missing or not owned by
        ``user_id`` so the API can answer 404 without leaking existence.
        """
        row = discovery_repo.set_recommendation_state(
            db,
            user_id=user_id,
            recommendation_id=recommendation_id,
            state=state,
        )
        if row is None:
            return None
        return self._build_recommendation_views(db, [row])[0]

    def _build_recommendation_views(
        self,
        db: Session,
        rows: list,
    ) -> list[RecommendationView]:
        if not rows:
            return []
        cand_map = discovery_repo.fetch_candidates_by_id(
            db, [r.candidate_id for r in rows]
        )
        task_map = discovery_repo.fetch_tasks_by_id(db, [r.task_id for r in rows])
        views: list[RecommendationView] = []
        for r in rows:
            cand = cand_map.get(r.candidate_id)
            task = task_map.get(r.task_id)
            # Re-validate the stored apply URL against its source host before
            # display; a host that drifted off the allowlist is hidden (None).
            allowed = _allowed_hosts_for(task, None) if task else set()
            apply_url: str | None = None
            if cand and cand.apply_url:
                result = validate_application_url(cand.apply_url, allowed)
                if isinstance(result, ValidatedApplicationUrl):
                    apply_url = result.url
            links: list[EvidenceLink] = []
            if cand:
                for ref in cand.evidence_refs_json or []:
                    if isinstance(ref, dict):
                        links.append(
                            EvidenceLink(
                                url=ref.get("url"),
                                evidence_type=ref.get("evidence_type"),
                            )
                        )
            views.append(
                RecommendationView(
                    id=r.id,
                    candidate_id=r.candidate_id,
                    task_id=r.task_id,
                    last_run_id=r.last_run_id,
                    title=cand.title if cand else None,
                    company_name=cand.company_name if cand else None,
                    locations=list(cand.locations_json or []) if cand else [],
                    apply_url=apply_url,
                    relevance_score=float(r.relevance_score or 0.0),
                    relevance_reason=r.relevance_reason,
                    matched_signals=list(r.matched_signals_json or []),
                    presentation_state=RecommendationPresentationState(
                        r.presentation_state
                    ),
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                    evidence_links=links,
                )
            )
        return views
