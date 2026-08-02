"""Personalized discovery pipeline boundary tests (Task 8).

Service-level (sync) integration tests that pin three safety boundaries the
HTTP tests cannot reach on their own:

1. A wall task produces a source status with CLOSED copy only - the raw wall
   HTML / cookies / anti-bot detail in the task summary never reach the
   owner-scoped status row.
2. A complete evidenced candidate becomes a pre-review recommendation but is
   NEVER promoted to a verified ``JobPosting`` (the pre-review channel is
   separate from the verified-only ``/jobs`` path).
3. The ``ON DELETE RESTRICT`` FK on recommendation.candidate_id enforces the
   retention order: personalized delivery rows must be deleted before the
   candidates/tasks they reference.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from backend.app.db.models import (
    DiscoveredJobCandidate,
    DiscoveredJobCandidateStatus,
    DiscoveryBlockReason,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    JobPosting,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
    UserPreference,
)
from backend.app.domain.personalized_discovery import (
    RecommendationPresentationState,
    SourceStatusReason,
)
from backend.app.repositories import personalized_discovery as discovery_repo
from backend.app.services.personalized_discovery import PersonalizedDiscoveryService
from backend.app.services.relevance.relevance_ranker import RankedCandidate

NOW = datetime(2026, 7, 25, 10, 0, 0, tzinfo=timezone.utc)


class _ScoringRanker:
    """Returns a fixed positive score for every candidate (fake LLM)."""

    def __init__(self, score: float = 85.0) -> None:
        self.score = score

    def rank(self, candidates, *, profile_summary, preferences):  # noqa: ARG002
        return [
            RankedCandidate(
                index=i,
                title=getattr(c, "title", None),
                score=self.score,
                reason="role match",
                matched_signals=["AI应用开发"],
            )
            for i, c in enumerate(candidates)
        ]


def _service(ranker=None) -> PersonalizedDiscoveryService:
    return PersonalizedDiscoveryService(ranker or _ScoringRanker())


def _source(db, key="dji", host="app.mokahr.com") -> JobSource:
    s = JobSource(
        id=f"src-{uuid.uuid4().hex[:8]}",
        source_key=key,
        provider=JobSourceProvider.USER_SUBMISSION,
        name=key,
        file_id=f"file-{uuid.uuid4().hex[:8]}",
        sheet_id=f"sheet-{uuid.uuid4().hex[:8]}",
        mapper_version="v1",
    )
    db.add(s)
    db.flush()
    s._host = host  # type: ignore[attr-defined]
    return s


def _raw(db, source, ext="rec1") -> RawJobRecord:
    r = RawJobRecord(
        id=f"raw-{uuid.uuid4().hex[:8]}",
        source_id=source.id,
        external_record_id=ext,
        payload_hash="a" * 64,
        raw_fields=[],
    )
    db.add(r)
    db.flush()
    return r


def _task(
    db,
    source,
    raw,
    *,
    ext="rec1",
    idem="t1",
    status=JobDiscoveryTaskStatus.succeeded,
    block_reason=None,
    coverage_verified=True,
    finished_at=NOW,
    extra_summary=None,
) -> JobDiscoveryTask:
    summary: dict = {"coverage_verified": coverage_verified}
    if extra_summary:
        summary.update(extra_summary)
    host = getattr(source, "_host", "app.mokahr.com")  # type: ignore[attr-defined]
    t = JobDiscoveryTask(
        id=f"task-{uuid.uuid4().hex[:8]}",
        source_id=source.id,
        raw_record_id=raw.id,
        external_record_id=ext,
        source_key=source.source_key,
        source_url=f"https://{host}/{ext}",
        url_hash=f"h-{idem}",
        payload_hash="a" * 64,
        idempotency_key=idem,
        agent_version="1.0.0",
        status=status,
        block_reason=block_reason,
        finished_at=finished_at,
        result_summary_json=summary,
    )
    db.add(t)
    db.flush()
    return t


def _candidate(
    db, task, source, raw, *, cid="c1", title="AI应用开发工程师"
) -> DiscoveredJobCandidate:
    c = DiscoveredJobCandidate(
        id=f"cand-{uuid.uuid4().hex[:8]}",
        task_id=task.id,
        source_id=source.id,
        raw_record_id=raw.id,
        external_record_id=task.external_record_id,
        idempotency_key=f"cand-{cid}",
        similarity_group_key=f"sim-{cid}",
        status=DiscoveredJobCandidateStatus.pending_review,
        title=title,
        company_name="某公司",
        responsibilities="负责 AI 应用架构设计",
        requirements="Python；LangChain",
        locations_json=["上海"],
        apply_url="https://app.mokahr.com/apply/abc",
        evidence_refs_json=[
            {"url": "https://app.mokahr.com/jobs/1", "content_hash": "h1", "evidence_type": "page_text"}
        ],
    )
    db.add(c)
    db.flush()
    return c


def _prefs(db, user, desired_roles=None) -> None:
    db.add(
        UserPreference(
            user_id=user.id,
            desired_roles=desired_roles or ["AI应用开发"],
            role_synonyms=[],
            excluded_roles=[],
        )
    )
    db.flush()


# ─── 1. Wall -> closed status, no raw wall data ──────────────────────────────


def test_wall_task_produces_closed_status_not_raw_wall_data(db_session, test_user):
    # A captcha task whose summary carries RAW wall data (cookies, cf-ray,
    # anti-bot HTML) that must never be copied into the owner-scoped status.
    raw_wall = {
        "wall_html": "<form>完成验证后即可继续访问</form>",
        "set_cookie": "cf_clearance=xxx; Path=/",
        "cf_ray": "89abcdef0-Shanghai",
        "anti_bot_detail": "Cloudflare challenge JS challenge",
    }
    source = _source(db_session)
    raw = _raw(db_session, source)
    _task(
        db_session, source, raw,
        status=JobDiscoveryTaskStatus.needs_manual_review,
        block_reason=DiscoveryBlockReason.captcha,
        coverage_verified=False,
        extra_summary=raw_wall,
    )
    _prefs(db_session, test_user)

    run = _service(_ScoringRanker()).run(db_session, user_id=test_user.id, now=NOW)

    statuses = _service().list_source_statuses(
        db_session, user_id=test_user.id, run_id=run.id, limit=100, offset=0
    )
    assert len(statuses) == 1
    st = statuses[0]
    assert st.reason_code == SourceStatusReason.CAPTCHA
    assert st.display_text == "该来源出现验证码拦截，自动发现已停止。"
    assert st.retry_guidance == "请在浏览器中手动完成验证后查看；系统不会绕过验证码。"

    # The closed-copy surface carries NONE of the raw wall data. The fixed
    # copy legitimately contains the generic phrase "完成验证", so we assert
    # on the raw-specific markers (cookies, cf-ray, Cloudflare, the full wall
    # phrase, and the summary dict keys) - none of which may appear.
    blob = f"{st.display_text} {st.retry_guidance} {st.safe_source_url} {st.source_key}"
    blob_low = blob.lower()
    raw_markers = [
        "cf-ray", "cf_ray", "cf_clearance", "cookie", "set_cookie",
        "cloudflare", "89abcdef0", "wall_html", "anti_bot_detail",
        "完成验证后即可继续访问", "<form>",
    ]
    for marker in raw_markers:
        assert marker.lower() not in blob_low, f"raw wall marker leaked: {marker!r}"

    # And the persisted row itself stores only closed fields (no summary copy).
    rows = discovery_repo.list_statuses_for_user(
        db_session, test_user.id, run_id=run.id, limit=100, offset=0
    )
    assert len(rows) == 1
    persisted = rows[0]
    assert persisted.reason_code == SourceStatusReason.CAPTCHA
    assert persisted.display_text == st.display_text


# ─── 2. Pre-review delivery never promotes to a verified JobPosting ──────────


def test_pre_review_candidate_becomes_recommendation_but_not_verified(
    db_session, test_user,
):
    source = _source(db_session)
    raw = _raw(db_session, source)
    task = _task(db_session, source, raw, coverage_verified=True)
    cand = _candidate(db_session, task, source, raw)
    _prefs(db_session, test_user)

    run = _service(_ScoringRanker(score=85.0)).run(
        db_session, user_id=test_user.id, now=NOW
    )
    assert run.status == "succeeded"

    recs = discovery_repo.list_recommendations_for_user(
        db_session, test_user.id, limit=100, offset=0
    )
    assert len(recs) == 1
    assert recs[0].candidate_id == cand.id
    assert recs[0].relevance_score == 85.0
    assert recs[0].presentation_state == RecommendationPresentationState.NEW

    # The candidate remains pending_review: it was NOT auto-verified.
    db_session.refresh(cand)
    assert cand.status == DiscoveredJobCandidateStatus.pending_review

    # And no JobPosting was created: the pre-review channel is separate from
    # the verified-only /jobs path.
    postings = db_session.query(JobPosting).all()
    assert postings == []


# ─── 3. RESTRICT retention order ────────────────────────────────────────────


def test_retention_delete_order_restricts_candidate_before_recommendation(
    db_session, test_user,
):
    # SQLite does not enforce FKs unless the pragma is on; enable it for this
    # boundary test so RESTRICT is exercised.
    db_session.execute(text("PRAGMA foreign_keys=ON"))

    source = _source(db_session)
    raw = _raw(db_session, source)
    task = _task(db_session, source, raw, coverage_verified=True)
    cand = _candidate(db_session, task, source, raw)
    _prefs(db_session, test_user)
    _service(_ScoringRanker()).run(db_session, user_id=test_user.id, now=NOW)

    rec = discovery_repo.list_recommendations_for_user(
        db_session, test_user.id, limit=100, offset=0
    )[0]
    assert rec.candidate_id == cand.id
    rec_id, cand_id = rec.id, cand.id
    # Clear the identity map so the ORM will not try to refresh these objects
    # after the raw-SQL DELETEs below (which the ORM is not informed about).
    db_session.expunge_all()

    # Deleting the candidate while a recommendation references it is BLOCKED
    # by ON DELETE RESTRICT - the personalized delivery row must go first.
    # Raw SQL DELETE hits the FK directly (no ORM relationship cascade).
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        db_session.execute(
            text("DELETE FROM discovered_job_candidates WHERE id = :id"),
            {"id": cand_id},
        )
        db_session.flush()
    db_session.rollback()

    # Correct order: delete the recommendation first, then the candidate
    # succeeds (no remaining reference).
    db_session.execute(
        text("DELETE FROM personalized_discovery_recommendations WHERE id = :id"),
        {"id": rec_id},
    )
    db_session.execute(
        text("DELETE FROM discovered_job_candidates WHERE id = :id"),
        {"id": cand_id},
    )
    db_session.flush()  # no IntegrityError: recommendation gone first

