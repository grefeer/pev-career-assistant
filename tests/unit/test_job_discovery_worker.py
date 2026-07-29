"""Unit tests for the Job Discovery Worker.

Covers:
- Successful task processing (succeeded / partial_success)
- Manual-review task (needs_manual_review)
- Worker crash recovery (exception -> mark_task_failed)
- Empty queue (no tasks to claim)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.base import Base
from backend.app.db.models import (
    DiscoveredJobCandidate,
    DiscoveryBlockReason,
    JobDiscoveryEvidence,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    JobDiscoveryTrajectory,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
)
from backend.app.services.job_discovery.worker import (
    JobDiscoveryWorker,
    _build_worker_id,
    _fallback_with_record_fields_if_agent_missed_evidence,
    _parse_agent_result,
)
from backend.app.services.job_discovery.schemas import DiscoveryRunResult, DiscoveryTaskInput, NormalizedJobCandidate, PageEvidence
from backend.app.services.job_discovery.skill_runtime import SkillRuntimeResult
from backend.app.services.job_discovery.single_source_proof import SingleSourceProof


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Engine:
    """In-memory engine — function-scoped so each test gets a clean DB."""
    e = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


@pytest.fixture
def db_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a sessionmaker bound to the shared engine."""
    return sessionmaker(bind=engine)


@pytest.fixture
def db(engine: Engine) -> Session:
    """Setup session — data persisted here is visible to the worker."""
    with Session(engine) as session:
        yield session


@pytest.fixture
def settings() -> Any:
    """Minimal settings object for testing."""
    from tests.conftest import settings_override

    return settings_override(
        job_discovery_enabled=True,
        job_discovery_task_timeout_seconds=60,
        job_discovery_model="gpt-4o-mini",
        job_discovery_skill_runtime_enabled=False,
    )


@pytest.fixture
def source(db: Session) -> JobSource:
    s = JobSource(
        id="test-source-id",
        source_key="test-source",
        provider=JobSourceProvider.USER_SUBMISSION,
        name="Test Source",
        file_id="f1",
        sheet_id="s1",
        mapper_version="v1",
    )
    db.add(s)
    db.flush()
    return s


@pytest.fixture
def raw_record(db: Session, source: JobSource) -> RawJobRecord:
    r = RawJobRecord(
        id="test-raw-id",
        source_id=source.id,
        external_record_id="ext-1",
        payload_hash="a" * 64,
        raw_fields=[{"field": "测试字段", "value": "test"}],
    )
    db.add(r)
    db.flush()
    return r


@pytest.fixture
def queued_task(db: Session, source: JobSource, raw_record: RawJobRecord) -> JobDiscoveryTask:
    t = JobDiscoveryTask(
        source_id=source.id,
        raw_record_id=raw_record.id,
        external_record_id="ext-1",
        source_key="test-source",
        source_url="https://example.com/jobs/1",
        url_hash="abc123def456",
        payload_hash="a" * 64,
        idempotency_key="task-idem-1",
        agent_version="1.0.0",
        status=JobDiscoveryTaskStatus.queued,
    )
    db.add(t)
    db.commit()  # commit so the worker (separate session) can see it
    return t


@pytest.fixture
def worker(
    db_session_factory: sessionmaker[Session],
    settings: Any,
) -> JobDiscoveryWorker:
    """Worker uses a session factory so it creates/commits/closes its own sessions."""
    return JobDiscoveryWorker(db_session_factory, settings)


# ---------------------------------------------------------------------------
# Worker ID
# ---------------------------------------------------------------------------


class TestWorkerId:
    def test_includes_hostname_and_pid(self) -> None:
        wid = _build_worker_id()
        assert "::" in wid
        parts = wid.split("::")
        assert len(parts) == 2
        assert parts[0]  # hostname
        assert parts[1].isdigit()  # PID


def test_worker_uses_skill_runtime_not_legacy_supervisor(
    worker: JobDiscoveryWorker, queued_task: JobDiscoveryTask, settings: Any,
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.job_discovery_skill_runtime_enabled = True
    expected = SkillRuntimeResult(
        result=DiscoveryRunResult(
            status="succeeded", candidates=[NormalizedJobCandidate(title="工程师", company_name="公司", responsibilities="开发")],
            evidence=[PageEvidence(evidence_type="skill_page_text", content_hash="a" * 64, metadata={"storage_uri": "file:///task/page.txt"})],
        ),
        trace_steps=[{"tool": "browse", "status": "ok", "duration_ms": 1}], artifact_root=Path.cwd(), coverage_verified=True,
    )
    worker._skill_runtime = MagicMock()
    worker._skill_runtime.run.return_value = expected
    legacy = MagicMock()
    monkeypatch.setattr("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent", legacy)

    assert worker.run_once() == 1
    worker._skill_runtime.run.assert_called_once()
    legacy.assert_not_called()
    with db_session_factory() as session:
        persisted = session.get(JobDiscoveryTask, queued_task.id)
        assert persisted.result_summary_json["execution_path"] == "skill_agent"
        assert session.query(JobDiscoveryEvidence).one().storage_uri == "file:///task/page.txt"
        assert session.query(JobDiscoveryTrajectory).one().completed_steps[0]["tool"] == "browse"


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------


class TestParseAgentResult:
    def test_structured_response(self) -> None:
        raw = {
            "structured_response": {
                "status": "succeeded",
                "block_reason": None,
                "evidence": [],
                "candidates": [{"title": "Engineer"}],
                "summary": "Done",
            }
        }
        result = _parse_agent_result(raw)
        assert result.status == "succeeded"
        assert len(result.candidates) == 1

    def test_pydantic_structured_response(self) -> None:
        class FakeStructuredResponse:
            def model_dump(self) -> dict:
                return {
                    "status": "succeeded",
                    "block_reason": None,
                    "evidence": [],
                    "candidates": [{"title": "Engineer"}],
                    "summary": "Done",
                }

        result = _parse_agent_result(
            {"structured_response": FakeStructuredResponse()}
        )
        assert result.status == "succeeded"
        assert len(result.candidates) == 1

    def test_direct_dict(self) -> None:
        raw = {"status": "failed", "summary": "Something broke"}
        result = _parse_agent_result(raw)
        assert result.status == "failed"
        assert result.summary == "Something broke"

    def test_last_message_json(self) -> None:
        class FakeMessage:
            content: str = '{"status": "needs_manual_review", "block_reason": "captcha", "summary": "Blocked"}'

        raw = {"messages": [FakeMessage()]}
        result = _parse_agent_result(raw)
        assert result.status == "needs_manual_review"
        assert result.block_reason == "captcha"

    def test_unparseable_falls_back_to_failed(self) -> None:
        raw = {"unexpected": "data"}
        result = _parse_agent_result(raw)
        assert result.status == "failed"

    def test_empty_messages_falls_back(self) -> None:
        raw = {"messages": []}
        result = _parse_agent_result(raw)
        assert result.status == "failed"

    @patch("backend.app.services.job_discovery.worker.package_candidates")
    @patch("backend.app.services.job_discovery.worker.verify_evidence")
    @patch("backend.app.services.job_discovery.worker.standardize_from_record_fields")
    @patch("backend.app.services.job_discovery.worker.run_web_navigation")
    def test_record_field_fallback_skips_manual_review_result(
        self,
        mock_navigation: MagicMock,
        mock_standardize: MagicMock,
        mock_verify: MagicMock,
        mock_package: MagicMock,
        settings: Any,
    ) -> None:
        mock_navigation.return_value = {
            "evidence_pages": [
                {
                    "evidence_type": "page_text",
                    "url": "https://example.test/job",
                    "content_hash": "hash1",
                    "text_excerpt": "岗位 招聘 投递",
                }
            ]
        }
        mock_standardize.return_value = '[{"title":"Engineer"}]'
        mock_verify.return_value = '[{"title":"Engineer"}]'
        mock_package.return_value = '[{"title":"Engineer","idempotency_key":"k","similarity_group_key":"g"}]'
        task = MagicMock(
            source_url="https://example.test/job",
            url_hash="urlhash",
            source_key="tencent-27-referrals",
        )
        task_input = DiscoveryTaskInput(
            source_id="source",
            raw_record_id="raw",
            external_record_id="record",
            source_key="tencent-27-referrals",
            source_url="https://example.test/job",
            url_hash="urlhash",
            record_fields=[],
        )

        result = _fallback_with_record_fields_if_agent_missed_evidence(
            DiscoveryRunResult(status="needs_manual_review", summary="blocked"),
            task=task,
            task_input=task_input,
            settings=settings,
        )

        mock_navigation.assert_not_called()
        assert result.status == "needs_manual_review"
        assert result.evidence == []
        assert result.candidates == []


# ---------------------------------------------------------------------------
# Successful task
# ---------------------------------------------------------------------------


class TestSuccessfulTask:
    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    def test_succeeded_status_persists_candidates(
        self,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        engine: Engine,
        settings: Any,
        worker: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
    ) -> None:
        # Merge into worker's session so ORM changes persist
        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "structured_response": {
                "status": "succeeded",
                "block_reason": None,
                "evidence": [
                    {
                        "evidence_type": "page_text",
                        "url": "https://example.com/jobs/1",
                        "title": "Job Page",
                        "content_hash": "ev-hash-1",
                        "text_excerpt": "Some text",
                        "metadata": {"page_num": 1},
                    }
                ],
                "candidates": [
                    {
                        "idempotency_key": "cand-1",
                        "similarity_group_key": "group-a",
                        "title": "Software Engineer",
                        "company_name": "Example Corp",
                        "locations": ["Beijing"],
                        "recruitment_types": ["Full-time"],
                        "confidence": 0.95,
                    }
                ],
                "summary": "Found 1 candidate",
            }
        }
        mock_build_agent.return_value = mock_agent

        result = worker.run_once()

        assert result == 1

        # Use a fresh session to verify persisted state
        with Session(engine) as verify_session:
            # Task should be marked succeeded
            task = verify_session.get(JobDiscoveryTask, queued_task.id)
            assert task is not None
            assert task.status is JobDiscoveryTaskStatus.succeeded
            assert task.finished_at is not None
            assert task.result_summary_json is not None
            assert task.result_summary_json["candidate_count"] == 1
            # Personalized discovery v1: the supervisor path is a legacy
            # executor, so ``single_source_complete`` must serialize as None
            # (never proven by implication). The key must always be present.
            assert "single_source_complete" in task.result_summary_json
            assert task.result_summary_json["single_source_complete"] is None

            # Evidence should be persisted
            evidence = verify_session.query(JobDiscoveryEvidence).all()
            assert len(evidence) == 1
            assert evidence[0].evidence_type == "page_text"
            assert evidence[0].content_hash == "ev-hash-1"

            # Candidate should be persisted
            candidates = verify_session.query(DiscoveredJobCandidate).all()
            assert len(candidates) == 1
            assert candidates[0].title == "Software Engineer"
            # Dedup repackages candidates as NormalizedJobCandidate objects, dropping
            # the mock's idempotency_key (not a dataclass field); the worker recomputes
            # it via build_candidate_idempotency_key (SHA-256 hex) on the object path.
            assert candidates[0].idempotency_key != "cand-1"
            assert len(candidates[0].idempotency_key) == 64

    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    def test_worker_generates_missing_candidate_keys(
        self,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        engine: Engine,
        worker: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
    ) -> None:
        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "structured_response": {
                "status": "succeeded",
                "block_reason": None,
                "evidence": [
                    {
                        "evidence_type": "page_text",
                        "url": "https://example.com/jobs/1",
                        "title": "Job Page",
                        "content_hash": "ev-hash-1",
                        "text_excerpt": "Some text",
                        "metadata": {"page_num": 1},
                    }
                ],
                "candidates": [
                    {
                        "title": "Software Engineer",
                        "company_name": "Example Corp",
                        "locations": ["Beijing"],
                        "recruitment_types": ["Internship"],
                        "apply_url": "https://example.com/apply/1",
                        "confidence": 0.95,
                    },
                    {
                        "title": "Product Manager",
                        "company_name": "Example Corp",
                        "locations": ["Shanghai"],
                        "recruitment_types": ["Internship"],
                        "apply_url": "https://example.com/apply/2",
                        "confidence": 0.9,
                    },
                ],
                "summary": "Found 2 candidates",
            }
        }
        mock_build_agent.return_value = mock_agent

        result = worker.run_once()

        assert result == 1
        with Session(engine) as verify_session:
            candidates = verify_session.query(DiscoveredJobCandidate).order_by(
                DiscoveredJobCandidate.title
            ).all()
            assert len(candidates) == 2
            assert all(c.idempotency_key for c in candidates)
            assert all(c.similarity_group_key for c in candidates)
            assert candidates[0].idempotency_key != candidates[1].idempotency_key

    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    def test_empty_partial_success_requires_manual_review(
        self,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        engine: Engine,
        settings: Any,
        worker: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
    ) -> None:
        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "structured_response": {
                "status": "partial_success",
                "block_reason": None,
                "evidence": [],
                "candidates": [],
                "summary": "Partial result",
            }
        }
        mock_build_agent.return_value = mock_agent

        result = worker.run_once()

        assert result == 1
        with Session(engine) as vs:
            t = vs.get(JobDiscoveryTask, queued_task.id)
            assert t is not None
            assert t.status is JobDiscoveryTaskStatus.needs_manual_review
            assert t.block_reason is DiscoveryBlockReason.parse_failed
            assert t.finished_at is not None


# ---------------------------------------------------------------------------
# Single-source proof serialization (personalized discovery v1)
# ---------------------------------------------------------------------------


class TestSingleSourceProofSerialization:
    """The worker must serialize ``single_source_complete`` into the task
    summary JSON, round-tripping the proof dict when the evaluator returns one
    and emitting ``None`` when it does not."""

    @patch("backend.app.services.job_discovery.worker.evaluate_single_source_proof")
    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    def test_proof_serialized_into_summary_json(
        self,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        mock_evaluate: MagicMock,
        engine: Engine,
        worker: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
    ) -> None:
        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "structured_response": {
                "status": "succeeded",
                "block_reason": None,
                "evidence": [
                    {
                        "evidence_type": "page_text",
                        "url": "https://example.com/jobs/1",
                        "title": "Job Page",
                        "content_hash": "ev-hash-1",
                        "text_excerpt": "Some text",
                        "metadata": {"page_num": 1},
                    }
                ],
                "candidates": [
                    {
                        "title": "Software Engineer",
                        "company_name": "Example Corp",
                        "locations": ["Beijing"],
                        "recruitment_types": ["Full-time"],
                        "confidence": 0.95,
                    }
                ],
                "summary": "Found 1 candidate",
            }
        }
        mock_build_agent.return_value = mock_agent

        fixture_proof = SingleSourceProof(
            contract_id="test-contract",
            evidence_hash="ev-hash-1",
            terminal_signal="job_list_complete",
            application_hosts=["app.example.com"],
        )
        mock_evaluate.return_value = fixture_proof

        result = worker.run_once()
        assert result == 1

        with Session(engine) as verify_session:
            task = verify_session.get(JobDiscoveryTask, queued_task.id)
            assert task is not None
            assert task.result_summary_json is not None
            payload = task.result_summary_json["single_source_complete"]
            assert payload is not None
            assert payload["contract_id"] == "test-contract"
            assert payload["evidence_hash"] == "ev-hash-1"
            assert payload["terminal_signal"] == "job_list_complete"
            assert payload["application_hosts"] == ["app.example.com"]
            # The evaluator is invoked with the worker's wired registry.
            assert mock_evaluate.call_count == 1
            _, kwargs = mock_evaluate.call_args
            assert kwargs["registry"] is worker.single_source_proof_registry

    @patch("backend.app.services.job_discovery.worker.evaluate_single_source_proof")
    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    def test_proof_none_serializes_as_none(
        self,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        mock_evaluate: MagicMock,
        engine: Engine,
        worker: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
    ) -> None:
        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "structured_response": {
                "status": "succeeded",
                "block_reason": None,
                "evidence": [
                    {
                        "evidence_type": "page_text",
                        "url": "https://example.com/jobs/1",
                        "title": "Job Page",
                        "content_hash": "ev-hash-1",
                        "text_excerpt": "Some text",
                        "metadata": {"page_num": 1},
                    }
                ],
                "candidates": [
                    {
                        "title": "Software Engineer",
                        "company_name": "Example Corp",
                        "locations": ["Beijing"],
                        "recruitment_types": ["Full-time"],
                        "confidence": 0.95,
                    }
                ],
                "summary": "Found 1 candidate",
            }
        }
        mock_build_agent.return_value = mock_agent
        mock_evaluate.return_value = None

        result = worker.run_once()
        assert result == 1

        with Session(engine) as verify_session:
            task = verify_session.get(JobDiscoveryTask, queued_task.id)
            assert task is not None
            assert task.result_summary_json is not None
            assert task.result_summary_json["single_source_complete"] is None


# ---------------------------------------------------------------------------
# Manual review task
# ---------------------------------------------------------------------------


class TestManualReviewTask:
    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    def test_marks_needs_manual_review_with_block_reason(
        self,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        engine: Engine,
        settings: Any,
        worker: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
    ) -> None:
        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "structured_response": {
                "status": "needs_manual_review",
                "block_reason": "captcha",
                "evidence": [],
                "candidates": [],
                "summary": "Blocked by CAPTCHA",
            }
        }
        mock_build_agent.return_value = mock_agent

        result = worker.run_once()

        assert result == 1
        with Session(engine) as vs:
            t = vs.get(JobDiscoveryTask, queued_task.id)
            assert t is not None
            assert t.status is JobDiscoveryTaskStatus.needs_manual_review
            assert t.block_reason is DiscoveryBlockReason.captcha
            assert t.finished_at is not None

    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    def test_unknown_block_reason_falls_back(
        self,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        engine: Engine,
        settings: Any,
        worker: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
    ) -> None:
        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "structured_response": {
                "status": "needs_manual_review",
                "block_reason": "some_unexpected_reason",
                "evidence": [],
                "candidates": [],
                "summary": "Unexpected block",
            }
        }
        mock_build_agent.return_value = mock_agent

        result = worker.run_once()

        assert result == 1
        with Session(engine) as vs:
            t = vs.get(JobDiscoveryTask, queued_task.id)
            assert t is not None
            assert t.status is JobDiscoveryTaskStatus.needs_manual_review
            assert t.block_reason is DiscoveryBlockReason.unknown


# ---------------------------------------------------------------------------
# Worker crash recovery
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    def test_agent_exception_marks_task_failed(
        self,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        engine: Engine,
        settings: Any,
        worker: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
    ) -> None:
        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        mock_agent = MagicMock()
        mock_agent.invoke.side_effect = RuntimeError("Agent crashed")
        mock_build_agent.return_value = mock_agent

        result = worker.run_once()

        assert result == 0
        with Session(engine) as vs:
            t = vs.get(JobDiscoveryTask, queued_task.id)
            assert t is not None
            assert t.status is JobDiscoveryTaskStatus.failed
            assert t.last_error is not None
            assert "Agent crashed" in t.last_error
            # Attempt count should be incremented
            assert t.attempt_count == 1
            assert t.finished_at is not None

    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    def test_empty_queue_returns_zero(
        self,
        mock_claim: MagicMock,
        settings: Any,
        worker: JobDiscoveryWorker,
    ) -> None:
        mock_claim.return_value = None

        result = worker.run_once()

        assert result == 0


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    @patch("backend.app.services.job_discovery.worker.JobDiscoveryWorker.run_once")
    def test_loop_calls_run_once_repeatedly(
        self,
        mock_run_once: MagicMock,
        worker: JobDiscoveryWorker,
    ) -> None:
        """Verify the loop invokes run_once until interrupted."""
        mock_run_once.side_effect = [0, 1, KeyboardInterrupt]

        worker.run_loop(poll_interval=0.01)

        assert mock_run_once.call_count == 3

    @patch("backend.app.services.job_discovery.worker.JobDiscoveryWorker.run_once")
    def test_loop_sleeps_when_empty(
        self,
        mock_run_once: MagicMock,
        worker: JobDiscoveryWorker,
    ) -> None:
        """Verify the loop sleeps after an empty poll."""
        import time as time_module

        mock_run_once.side_effect = [0, KeyboardInterrupt]

        with patch.object(time_module, "sleep") as mock_sleep:
            worker.run_loop(poll_interval=0.5)

        mock_sleep.assert_called_once_with(0.5)

    @patch("backend.app.services.job_discovery.worker.JobDiscoveryWorker.run_once")
    def test_loop_stops_on_keyboard_interrupt(
        self,
        mock_run_once: MagicMock,
        worker: JobDiscoveryWorker,
    ) -> None:
        mock_run_once.side_effect = KeyboardInterrupt

        # Should not raise — the loop catches KeyboardInterrupt
        worker.run_loop(poll_interval=0.01)

        mock_run_once.assert_called_once()
