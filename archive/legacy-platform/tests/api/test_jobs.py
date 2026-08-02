"""Public-job isolation boundary tests (Task 8).

A pending-review discovery candidate can be personalized (surfaced via the
pre-review channel) but must NEVER appear in the verified-only ``/api/jobs``
output. These tests pin that boundary at the HTTP layer so a future wiring
mistake cannot leak pre-review data into the public job center.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.app.db.models import (
    DiscoveredJobCandidate,
    DiscoveredJobCandidateStatus,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
)
from backend.app.services.personalized_discovery import PersonalizedDiscoveryService

pytestmark = pytest.mark.asyncio


class _NoopRanker:
    def rank(self, candidates, *, profile_summary, preferences):  # noqa: ARG002
        return []


@pytest.fixture
def pd_service(app):
    svc = PersonalizedDiscoveryService(_NoopRanker())
    app.state.personalized_discovery_service = svc
    return svc


@pytest.fixture
def recommendation(db_session, test_user):
    """A pending-review candidate + matching pre-review recommendation.

    The candidate is ``pending_review`` (never ``verified``); the public
    ``/api/jobs`` path must not surface it.
    """
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
    )
    db_session.add_all([src, raw, task, cand])
    db_session.commit()
    return SimpleNamespace(candidate=cand, candidate_id=cand.id, task=task)


class TestPublicJobIsolation:
    async def test_pre_review_delivery_never_leaks_into_jobs(
        self, client, auth_headers, recommendation
    ) -> None:
        # The personalized candidate is pending_review, not verified.
        assert recommendation.candidate.status.value == "pending_review"

        resp = await client.get("/api/jobs", headers=auth_headers)
        assert resp.status_code == 200
        titles = [j["title"] for j in resp.json()["jobs"]]
        assert recommendation.candidate.title not in titles

        # Direct access by candidate id is also 404 (not a verified posting).
        detail = await client.get(
            f"/api/jobs/{recommendation.candidate_id}", headers=auth_headers
        )
        assert detail.status_code == 404

    async def test_jobs_list_requires_auth(self, client, recommendation) -> None:
        resp = await client.get("/api/jobs")
        assert resp.status_code == 401
