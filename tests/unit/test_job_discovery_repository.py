from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import (
    DiscoveredJobCandidateStatus,
    DiscoveryBlockReason,
    JobDiscoveryTaskStatus,
    JobPosting,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.api.routes.job_discovery import approve_job_discovery_candidate
from backend.app.repositories import job_discovery


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def create_source(db: Session, **overrides: Any) -> JobSource:
    values = {
        "source_key": "test-source",
        "provider": JobSourceProvider.USER_SUBMISSION,
        "name": "Test Source",
        "file_id": "f1",
        "sheet_id": "s1",
        "mapper_version": "v1",
        **overrides,
    }
    source = JobSource(**values)
    db.add(source)
    db.flush()
    return source


def create_raw_record(
    db: Session, source_id: str, **overrides: Any
) -> RawJobRecord:
    values = {
        "source_id": source_id,
        "external_record_id": "ext-1",
        "payload_hash": "a" * 64,
        "raw_fields": [],
        **overrides,
    }
    record = RawJobRecord(**values)
    db.add(record)
    db.flush()
    return record


def default_task_kwargs(
    *, source_id: str, raw_record_id: str
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "raw_record_id": raw_record_id,
        "external_record_id": "ext-1",
        "source_key": "test-source",
        "source_url": "https://example.com/jobs/1",
        "url_hash": "abc123",
        "payload_hash": "a" * 64,
        "idempotency_key": "idem-1",
        "agent_version": "1.0.0",
    }


class TestCreateOrGetTask:
    def test_creates_new_task(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        kwargs = default_task_kwargs(source_id=source.id, raw_record_id=record.id)

        task, created = job_discovery.create_or_get_task(db, **kwargs)

        assert created is True
        assert task.id is not None
        assert task.status is JobDiscoveryTaskStatus.queued
        assert task.idempotency_key == "idem-1"
        assert task.agent_version == "1.0.0"

    def test_idempotent_same_params_returns_existing(
        self, db: Session
    ) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        kwargs = default_task_kwargs(source_id=source.id, raw_record_id=record.id)

        first, created_first = job_discovery.create_or_get_task(db, **kwargs)
        second, created_second = job_discovery.create_or_get_task(db, **kwargs)

        assert first.id == second.id
        assert (created_first, created_second) == (True, False)

    def test_different_source_record_creates_separate_task(
        self, db: Session
    ) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        base = default_task_kwargs(source_id=source.id, raw_record_id=record.id)

        first, _ = job_discovery.create_or_get_task(db, **base)
        second, created = job_discovery.create_or_get_task(
            db,
            **{
                **base,
                "external_record_id": "ext-2",
                "url_hash": "def456",
                "payload_hash": "b" * 64,
                "idempotency_key": "idem-2",
            },
        )

        assert first.id != second.id
        assert created is True

    def test_repeated_sync_does_not_create_duplicate_tasks(
        self, db: Session
    ) -> None:
        """Simulate repeated sync runs: same params = same task."""
        source = create_source(db)
        record = create_raw_record(db, source.id)
        kwargs = default_task_kwargs(source_id=source.id, raw_record_id=record.id)

        tasks_created: list[bool] = []
        for _ in range(3):
            _, created = job_discovery.create_or_get_task(db, **kwargs)
            tasks_created.append(created)

        assert tasks_created == [True, False, False]


class TestClaimNextTask:
    def test_claim_returns_queued_task(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )

        claimed = job_discovery.claim_next_task(
            db, worker_id="worker-1", lease_seconds=30
        )

        assert claimed is not None
        assert claimed.id == task.id
        assert claimed.status is JobDiscoveryTaskStatus.running
        assert claimed.lease_owner == "worker-1"

    def test_nothing_to_claim_returns_none(self, db: Session) -> None:
        claimed = job_discovery.claim_next_task(
            db, worker_id="worker-1", lease_seconds=30
        )
        assert claimed is None

    def test_lease_prevents_second_worker_from_claiming(
        self, db: Session
    ) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )

        first = job_discovery.claim_next_task(
            db, worker_id="worker-1", lease_seconds=300
        )
        second = job_discovery.claim_next_task(
            db, worker_id="worker-2", lease_seconds=30
        )

        assert first is not None
        assert second is None

    def test_expired_lease_can_be_reclaimed(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )

        first = job_discovery.claim_next_task(
            db, worker_id="worker-1", lease_seconds=-1
        )
        assert first is not None

        second = job_discovery.claim_next_task(
            db, worker_id="worker-2", lease_seconds=300
        )

        assert second is not None
        assert second.id == first.id
        assert second.lease_owner == "worker-2"

    def test_maxed_out_attempts_not_claimable(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )
        task.attempt_count = task.max_attempts
        db.flush()

        claimed = job_discovery.claim_next_task(
            db, worker_id="worker-1", lease_seconds=30
        )
        assert claimed is None


class TestMarkTask:
    def test_mark_running(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )

        job_discovery.mark_task_running(db, task)

        assert task.status is JobDiscoveryTaskStatus.running
        assert task.started_at is not None

    def test_mark_succeeded(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )

        job_discovery.mark_task_succeeded(
            db, task, result_summary_json={"matched": 5}
        )

        assert task.status is JobDiscoveryTaskStatus.succeeded
        assert task.finished_at is not None
        assert task.result_summary_json == {"matched": 5}

    def test_mark_partial_success(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )

        job_discovery.mark_task_partial_success(
            db, task, result_summary_json={"matched": 2, "skipped": 3}
        )

        assert task.status is JobDiscoveryTaskStatus.partial_success
        assert task.result_summary_json == {"matched": 2, "skipped": 3}

    def test_mark_needs_manual_review(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )

        job_discovery.mark_task_needs_manual_review(
            db, task, block_reason=DiscoveryBlockReason.captcha
        )

        assert task.status is JobDiscoveryTaskStatus.needs_manual_review
        assert task.block_reason is DiscoveryBlockReason.captcha

    def test_mark_failed_increments_attempt_count(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )
        assert task.attempt_count == 0

        job_discovery.mark_task_failed(db, task, last_error="connection lost")

        assert task.status is JobDiscoveryTaskStatus.failed
        assert task.last_error == "connection lost"
        assert task.attempt_count == 1


class TestUpsertEvidence:
    def test_create_evidence(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )

        evidence = job_discovery.upsert_evidence(
            db,
            task_id=task.id,
            evidence_type="screenshot",
            url="https://example.com/screen1.png",
            title="岗位页面截图",
            content_hash="sha256hash1",
            text_excerpt="岗位描述详情...",
            storage_uri="s3://bucket/screen1.png",
            metadata_json={"resolution": "1920x1080"},
        )

        assert evidence.id is not None
        assert evidence.evidence_type == "screenshot"
        assert evidence.content_hash == "sha256hash1"

    def test_upsert_deduplicates_by_hash(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )

        first = job_discovery.upsert_evidence(
            db,
            task_id=task.id,
            evidence_type="screenshot",
            url="https://example.com/screen1.png",
            title="岗位页面截图",
            content_hash="sha256hash1",
            text_excerpt="岗位描述详情...",
            storage_uri="s3://bucket/screen1.png",
            metadata_json=None,
        )
        second = job_discovery.upsert_evidence(
            db,
            task_id=task.id,
            evidence_type="screenshot",
            url="https://example.com/screen2.png",
            title="更新的截图",
            content_hash="sha256hash1",
            text_excerpt="相同内容...",
            storage_uri="s3://bucket/screen2.png",
            metadata_json=None,
        )

        assert first.id == second.id


class TestUpsertCandidate:
    def test_create_candidate(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )

        candidate = job_discovery.upsert_candidate(
            db,
            task_id=task.id,
            source_id=source.id,
            raw_record_id=record.id,
            external_record_id="ext-1",
            idempotency_key="cand-1",
            similarity_group_key="group-a",
            title="高级工程师",
            company_name="示例公司",
            department="技术部",
            description_text="岗位描述",
            responsibilities="负责开发",
            requirements="5年经验",
            locations_json=["北京"],
            recruitment_types_json=["全职"],
            industries_json=["互联网"],
            apply_url="https://example.com/apply",
            application_channel_json={"channel": "官网"},
            deadline_text="2026-08-01",
            referral_code="REF001",
            confidence=0.95,
            evidence_refs_json=[{"type": "screenshot", "hash": "abc"}],
            normalization_warnings_json=["薪资未填写"],
        )

        assert candidate.id is not None
        assert candidate.company_name == "示例公司"
        assert candidate.confidence == 0.95

    def test_upsert_deduplicates_by_idempotency_key(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )
        base_kwargs = {
            "task_id": task.id,
            "source_id": source.id,
            "raw_record_id": record.id,
            "external_record_id": "ext-1",
            "idempotency_key": "cand-1",
            "similarity_group_key": "group-a",
            "title": "高级工程师",
            "company_name": "示例公司",
            "department": None,
            "description_text": None,
            "responsibilities": None,
            "requirements": None,
            "locations_json": None,
            "recruitment_types_json": None,
            "industries_json": None,
            "apply_url": None,
            "application_channel_json": None,
            "deadline_text": None,
            "referral_code": None,
            "confidence": None,
            "evidence_refs_json": None,
            "normalization_warnings_json": None,
        }

        first = job_discovery.upsert_candidate(db, **base_kwargs)
        second = job_discovery.upsert_candidate(
            db, **{**base_kwargs, "title": "高级工程师(修改版)"}
        )

        assert first.id == second.id
        assert first.title == "高级工程师"


class TestListReviewGroups:
    def test_returns_pending_candidates_grouped(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )

        job_discovery.upsert_candidate(
            db,
            task_id=task.id,
            source_id=source.id,
            raw_record_id=record.id,
            external_record_id="ext-1",
            idempotency_key="cand-a1",
            similarity_group_key="group-a",
            title="岗位A1",
            company_name=None,
            department=None,
            description_text=None,
            responsibilities=None,
            requirements=None,
            locations_json=None,
            recruitment_types_json=None,
            industries_json=None,
            apply_url=None,
            application_channel_json=None,
            deadline_text=None,
            referral_code=None,
            confidence=None,
            evidence_refs_json=None,
            normalization_warnings_json=None,
        )
        job_discovery.upsert_candidate(
            db,
            task_id=task.id,
            source_id=source.id,
            raw_record_id=record.id,
            external_record_id="ext-2",
            idempotency_key="cand-a2",
            similarity_group_key="group-a",
            title="岗位A2",
            company_name=None,
            department=None,
            description_text=None,
            responsibilities=None,
            requirements=None,
            locations_json=None,
            recruitment_types_json=None,
            industries_json=None,
            apply_url=None,
            application_channel_json=None,
            deadline_text=None,
            referral_code=None,
            confidence=None,
            evidence_refs_json=None,
            normalization_warnings_json=None,
        )
        job_discovery.upsert_candidate(
            db,
            task_id=task.id,
            source_id=source.id,
            raw_record_id=record.id,
            external_record_id="ext-3",
            idempotency_key="cand-b1",
            similarity_group_key="group-b",
            title="岗位B1",
            company_name=None,
            department=None,
            description_text=None,
            responsibilities=None,
            requirements=None,
            locations_json=None,
            recruitment_types_json=None,
            industries_json=None,
            apply_url=None,
            application_channel_json=None,
            deadline_text=None,
            referral_code=None,
            confidence=None,
            evidence_refs_json=None,
            normalization_warnings_json=None,
        )

        groups = job_discovery.list_review_groups(db)

        assert len(groups) == 2
        group_map = {g["similarity_group_key"]: g["candidates"] for g in groups}
        assert len(group_map["group-a"]) == 2
        assert len(group_map["group-b"]) == 1

    def test_excludes_non_pending_review(self, db: Session) -> None:
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )

        job_discovery.upsert_candidate(
            db,
            task_id=task.id,
            source_id=source.id,
            raw_record_id=record.id,
            external_record_id="ext-1",
            idempotency_key="cand-1",
            similarity_group_key="group-a",
            title=None,
            company_name=None,
            department=None,
            description_text=None,
            responsibilities=None,
            requirements=None,
            locations_json=None,
            recruitment_types_json=None,
            industries_json=None,
            apply_url=None,
            application_channel_json=None,
            deadline_text=None,
            referral_code=None,
            confidence=None,
            evidence_refs_json=None,
            normalization_warnings_json=None,
        )
        approved = job_discovery.upsert_candidate(
            db,
            task_id=task.id,
            source_id=source.id,
            raw_record_id=record.id,
            external_record_id="ext-2",
            idempotency_key="cand-2",
            similarity_group_key="group-b",
            title=None,
            company_name=None,
            department=None,
            description_text=None,
            responsibilities=None,
            requirements=None,
            locations_json=None,
            recruitment_types_json=None,
            industries_json=None,
            apply_url=None,
            application_channel_json=None,
            deadline_text=None,
            referral_code=None,
            confidence=None,
            evidence_refs_json=None,
            normalization_warnings_json=None,
        )
        approved.status = DiscoveredJobCandidateStatus.approved
        db.flush()

        groups = job_discovery.list_review_groups(db)

        assert len(groups) == 1
        assert groups[0]["similarity_group_key"] == "group-a"


class TestApproveDiscoveryCandidate:
    def test_same_raw_record_multiple_candidates_create_distinct_postings(
        self, db: Session
    ) -> None:
        admin = User(
            id="admin",
            account="admin",
            nickname="Admin",
            password_hash="unused",
            role=UserRole.ADMIN,
        )
        db.add(admin)
        source = create_source(db)
        record = create_raw_record(db, source.id)
        task, _ = job_discovery.create_or_get_task(
            db, **default_task_kwargs(source_id=source.id, raw_record_id=record.id)
        )

        first = job_discovery.upsert_candidate(
            db,
            task_id=task.id,
            source_id=source.id,
            raw_record_id=record.id,
            external_record_id="same-record",
            idempotency_key="candidate-one-key",
            similarity_group_key="group-a",
            title="后端开发工程师",
            company_name="示例公司",
            department=None,
            description_text="后端 JD",
            responsibilities=None,
            requirements=None,
            locations_json=["北京"],
            recruitment_types_json=["实习"],
            industries_json=None,
            apply_url="https://example.com/jobs/backend",
            application_channel_json=None,
            deadline_text=None,
            referral_code=None,
            confidence=0.9,
            evidence_refs_json=None,
            normalization_warnings_json=None,
        )
        second = job_discovery.upsert_candidate(
            db,
            task_id=task.id,
            source_id=source.id,
            raw_record_id=record.id,
            external_record_id="same-record",
            idempotency_key="candidate-two-key",
            similarity_group_key="group-a",
            title="前端开发工程师",
            company_name="示例公司",
            department=None,
            description_text="前端 JD",
            responsibilities=None,
            requirements=None,
            locations_json=["上海"],
            recruitment_types_json=["实习"],
            industries_json=None,
            apply_url="https://example.com/jobs/frontend",
            application_channel_json=None,
            deadline_text=None,
            referral_code=None,
            confidence=0.9,
            evidence_refs_json=None,
            normalization_warnings_json=None,
        )
        db.commit()

        approve_job_discovery_candidate(first.id, admin, db)
        approve_job_discovery_candidate(second.id, admin, db)

        postings = db.query(JobPosting).order_by(JobPosting.title).all()
        assert len(postings) == 2
        assert {posting.title for posting in postings} == {
            "前端开发工程师",
            "后端开发工程师",
        }
        assert len({posting.external_record_id for posting in postings}) == 2
