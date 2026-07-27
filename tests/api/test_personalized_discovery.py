"""Personalized discovery v1 API tests (Task 7).

The routes are thin: these tests pin owner-scoping, the extra="forbid" crawler
rejection, the fixed auto-discovery label, the closed reason codes, and that a
not-owned recommendation is 404 without leaking existence.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from backend.app.db.models import (
    DiscoveredJobCandidate,
    DiscoveredJobCandidateStatus,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    JobSource,
    JobSourceProvider,
    PersonalizedDiscoveryRecommendation,
    PersonalizedDiscoveryRun,
    RawJobRecord,
)
from backend.app.domain.personalized_discovery import (
    RecommendationPresentationState,
)
from backend.app.services.personalized_discovery import (
    PersonalizedDiscoveryError,
    PersonalizedDiscoveryRateLimitError,
    PersonalizedDiscoveryService,
)

pytestmark = pytest.mark.asyncio


# ─── Fake ranker (never delivers, but lets the real service run end-to-end) ──


class _NoopRanker:
    def rank(self, candidates, *, profile_summary, preferences):  # noqa: ARG002
        return []


def _real_service() -> PersonalizedDiscoveryService:
    return PersonalizedDiscoveryService(_NoopRanker())


@pytest.fixture
def pd_service(app):
    """Inject a real service with a noop ranker (ranker unused for reads)."""
    svc = _real_service()
    app.state.personalized_discovery_service = svc
    return svc


@pytest.fixture
def pd_service_fake(app):
    """Inject a MagicMock service for error-mapping tests."""
    svc = MagicMock()
    app.state.personalized_discovery_service = svc
    return svc


# ─── Seeding ─────────────────────────────────────────────────────────────────


def _seed_recommendation(db, user_id, *, state=RecommendationPresentationState.NEW):
    """Create the full source->raw->task->candidate->recommendation chain."""
    src = JobSource(
        id=f"src-{uuid.uuid4().hex[:8]}",
        source_key="dji",
        provider=JobSourceProvider.USER_SUBMISSION,
        name="dji",
        file_id=f"file-{uuid.uuid4().hex[:8]}",
        sheet_id=f"sheet-{uuid.uuid4().hex[:8]}",
        mapper_version="v1",
    )
    raw = RawJobRecord(
        id=f"raw-{uuid.uuid4().hex[:8]}",
        source_id=src.id,
        external_record_id="rec1",
        payload_hash="a" * 64,
        raw_fields=[],
    )
    task = JobDiscoveryTask(
        id=f"task-{uuid.uuid4().hex[:8]}",
        source_id=src.id,
        raw_record_id=raw.id,
        external_record_id="rec1",
        source_key="dji",
        source_url="https://app.mokahr.com/rec1",
        url_hash="h1",
        payload_hash="a" * 64,
        idempotency_key="t1",
        agent_version="1.0.0",
        status=JobDiscoveryTaskStatus.succeeded,
        finished_at=datetime.now(timezone.utc),
        result_summary_json={"coverage_verified": True},
    )
    cand = DiscoveredJobCandidate(
        id=f"cand-{uuid.uuid4().hex[:8]}",
        task_id=task.id,
        source_id=src.id,
        raw_record_id=raw.id,
        external_record_id="rec1",
        idempotency_key="c1",
        similarity_group_key="s1",
        status=DiscoveredJobCandidateStatus.pending_review,
        title="AI应用开发工程师",
        company_name="某公司",
        responsibilities="职责",
        requirements="要求",
        locations_json=["上海"],
        apply_url="https://app.mokahr.com/apply/abc",
        evidence_refs_json=[
            {"url": "https://app.mokahr.com/jobs/1", "content_hash": "h1", "evidence_type": "page_text"}
        ],
    )
    rec = PersonalizedDiscoveryRecommendation(
        id=f"rec-{uuid.uuid4().hex[:8]}",
        user_id=user_id,
        candidate_id=cand.id,
        task_id=task.id,
        last_run_id="run-1",
        canonical_job_key="v1:abc",
        preference_version=1,
        relevance_score=85.0,
        relevance_reason="role match",
        matched_signals_json=["AI应用开发"],
        presentation_state=state,
    )
    db.add_all([src, raw, task, cand, rec])
    db.commit()
    return rec


# ─── Preferences ────────────────────────────────────────────────────────────


class TestPreferencesAPI:
    async def test_get_returns_defaults_when_absent(self, client, auth_headers, pd_service):
        resp = await client.get("/api/personalized-discovery/preferences", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["desired_roles"] == []
        assert body["role_synonyms"] == []
        assert body["excluded_roles"] == []
        assert body["personalized_discovery_min_score"] is None
        assert body["version"] == 0

    async def test_patch_extends_and_persists(self, client, auth_headers, pd_service):
        resp = await client.patch(
            "/api/personalized-discovery/preferences",
            headers=auth_headers,
            json={
                "desired_roles": ["AI应用开发"],
                "role_synonyms": ["Agent开发"],
                "excluded_roles": ["销售"],
                "personalized_discovery_min_score": 70,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["desired_roles"] == ["AI应用开发"]
        assert body["role_synonyms"] == ["Agent开发"]
        assert body["excluded_roles"] == ["销售"]
        assert body["personalized_discovery_min_score"] == 70
        assert body["version"] >= 1

        # A second GET reflects the persisted row (owner-scoped).
        got = await client.get(
            "/api/personalized-discovery/preferences", headers=auth_headers
        )
        assert got.json()["desired_roles"] == ["AI应用开发"]

    async def test_patch_rejects_blank_role_term(self, client, auth_headers, pd_service):
        resp = await client.patch(
            "/api/personalized-discovery/preferences",
            headers=auth_headers,
            json={"desired_roles": ["AI应用开发", "   "]},
        )
        assert resp.status_code == 422

    async def test_delete_clears_preferences(self, client, auth_headers, pd_service):
        await client.patch(
            "/api/personalized-discovery/preferences",
            headers=auth_headers,
            json={"desired_roles": ["AI应用开发"]},
        )
        resp = await client.delete(
            "/api/personalized-discovery/preferences", headers=auth_headers
        )
        assert resp.status_code == 204
        got = await client.get(
            "/api/personalized-discovery/preferences", headers=auth_headers
        )
        assert got.json()["desired_roles"] == []
        assert got.json()["version"] == 0


# ─── Runs ───────────────────────────────────────────────────────────────────


class TestRunsAPI:
    async def test_run_rejects_crawler_inputs(self, client, auth_headers, pd_service):
        # Any body field (url/site/adapter) is forbidden -> 422.
        resp = await client.post(
            "/api/personalized-discovery/runs",
            headers=auth_headers,
            json={"url": "https://example.com"},
        )
        assert resp.status_code == 422

    async def test_run_rejects_unauthenticated(self, client, pd_service):
        resp = await client.post(
            "/api/personalized-discovery/runs", json={}
        )
        assert resp.status_code == 401

    async def test_run_maps_rate_limit_to_429(self, client, auth_headers, pd_service_fake):
        pd_service_fake.run.side_effect = PersonalizedDiscoveryRateLimitError("limit")
        resp = await client.post(
            "/api/personalized-discovery/runs", headers=auth_headers, json={}
        )
        assert resp.status_code == 429
        assert resp.json()["detail"]["code"] == "rate_limited"

    async def test_run_maps_internal_error_to_500(self, client, auth_headers, pd_service_fake):
        pd_service_fake.run.side_effect = PersonalizedDiscoveryError("boom")
        resp = await client.post(
            "/api/personalized-discovery/runs", headers=auth_headers, json={}
        )
        assert resp.status_code == 500


# ─── Recommendations ─────────────────────────────────────────────────────────


class TestRecommendationsAPI:
    async def test_list_returns_cards_with_fixed_label(
        self, client, auth_headers, pd_service, test_user, db_session
    ):
        rec = _seed_recommendation(db_session, test_user.id)
        resp = await client.get(
            "/api/personalized-discovery/recommendations", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        card = body["items"][0]
        assert card["id"] == rec.id
        assert card["title"] == "AI应用开发工程师"
        assert card["company"] == "某公司"
        assert card["locations"] == ["上海"]
        assert card["apply_url"] == "https://app.mokahr.com/apply/abc"
        assert card["score"] == 85.0
        assert card["signals"] == ["AI应用开发"]
        assert card["label"] == "自动发现，建议自行确认"
        assert card["state"] == "new"
        assert card["evidence_links"][0]["url"] == "https://app.mokahr.com/jobs/1"
        # No JD body / raw task / error leakage.
        assert "responsibilities" not in card
        assert "requirements" not in card
        assert "description_text" not in card

    async def test_card_hides_apply_url_when_host_drifts(
        self, client, auth_headers, pd_service, test_user, db_session
    ):
        rec = _seed_recommendation(db_session, test_user.id)
        # Tamper: apply URL host no longer matches the source host.
        from backend.app.db.models import DiscoveredJobCandidate as _Cand

        cand = db_session.get(_Cand, rec.candidate_id)
        cand.apply_url = "https://evil.example.com/apply"
        db_session.commit()

        resp = await client.get(
            "/api/personalized-discovery/recommendations", headers=auth_headers
        )
        card = resp.json()["items"][0]
        assert card["apply_url"] is None

    async def test_other_user_cannot_change_delivery_state(
        self, client, auth_headers, other_user_headers, pd_service, test_user, db_session
    ):
        rec = _seed_recommendation(db_session, test_user.id)
        # Owner can dismiss.
        resp = await client.post(
            f"/api/personalized-discovery/recommendations/{rec.id}/interactions",
            headers=auth_headers,
            json={"state": "dismissed"},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "dismissed"
        # A different user gets 404 (no existence leak).
        other = await client.post(
            f"/api/personalized-discovery/recommendations/{rec.id}/interactions",
            headers=other_user_headers,
            json={"state": "dismissed"},
        )
        assert other.status_code == 404

    async def test_interaction_rejects_new_state(
        self, client, auth_headers, pd_service, test_user, db_session
    ):
        rec = _seed_recommendation(db_session, test_user.id)
        resp = await client.post(
            f"/api/personalized-discovery/recommendations/{rec.id}/interactions",
            headers=auth_headers,
            json={"state": "new"},
        )
        # ``new`` is not a user interaction, only viewed|saved|dismissed|apply_clicked.
        assert resp.status_code == 422

    async def test_missing_recommendation_is_404(self, client, auth_headers, pd_service):
        resp = await client.post(
            "/api/personalized-discovery/recommendations/does-not-exist/interactions",
            headers=auth_headers,
            json={"state": "viewed"},
        )
        assert resp.status_code == 404


# ─── Source statuses ─────────────────────────────────────────────────────────


class TestSourceStatusesAPI:
    async def test_list_requires_run_id_query(
        self, client, auth_headers, pd_service
    ):
        # run_id is a required query param.
        resp = await client.get(
            "/api/personalized-discovery/source-statuses", headers=auth_headers
        )
        assert resp.status_code == 422

    async def test_list_returns_fixed_copy_only(
        self, client, auth_headers, pd_service, test_user, db_session
    ):
        from backend.app.db.models import UserDiscoverySourceStatus
        from backend.app.domain.personalized_discovery import SourceStatusReason

        run = PersonalizedDiscoveryRun(
            id=f"run-{uuid.uuid4().hex[:8]}",
            user_id=test_user.id,
            preference_version=1,
            status="succeeded",
            started_at=datetime.now(timezone.utc),
        )
        status_row = UserDiscoverySourceStatus(
            id=f"st-{uuid.uuid4().hex[:8]}",
            user_id=test_user.id,
            run_id=run.id,
            task_id="task-x",
            source_key="dji",
            safe_source_url="https://app.mokahr.com/rec1",
            reason_code=SourceStatusReason.LOGIN_REQUIRED,
            display_text="该来源需要登录后才能查看完整职位，自动发现已停止。",
            retry_guidance="请自行登录官方招聘页确认；系统不会代为登录。",
        )
        db_session.add_all([run, status_row])
        db_session.commit()

        resp = await client.get(
            f"/api/personalized-discovery/source-statuses?run_id={run.id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["reason_code"] == "login_required"
        assert item["display_text"] == "该来源需要登录后才能查看完整职位，自动发现已停止。"
        # No raw wall text / cookies / auth detail beyond the fixed copy.
        assert "cookies" not in item
        assert "auth" not in item
