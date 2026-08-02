"""Unit tests for trajectory_store."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryTrajectory
from backend.app.services.job_discovery.schemas import DiscoveryRunResult
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer
from backend.app.services.job_discovery.strategy.trajectory_store import (
    get_pending_annotations,
    save_trajectory,
    schedule_annotation,
)


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


class TestSaveTrajectory:
    def test_save_basic(self, engine):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        buf.record_step("open_url", "ok", {"url": "x"}, "result")

        result = DiscoveryRunResult(status="succeeded", summary="ok")

        with Session(engine) as db:
            tid = save_trajectory(db, buf, result, "https://x.com/job", "x.com/*")
            db.commit()

        with Session(engine) as db:
            traj = db.get(JobDiscoveryTrajectory, tid)
            assert traj is not None
            assert traj.executor_type == "snapshot"
            assert traj.overall_status == "succeeded"
            assert traj.url_pattern == "x.com/*"
            assert traj.completed_steps is not None

    def test_save_with_failure(self, engine):
        buf = TrajectoryBuffer(task_id="t2", strategy_id="s2", executor_type="snapshot")
        buf.record_step("s1", "ok", {}, "ok")
        buf.record_step("s2", "failed", {}, None, error=ValueError("bad input"))

        result = DiscoveryRunResult(status="failed", summary="step 2 failed")

        with Session(engine) as db:
            tid = save_trajectory(db, buf, result, "https://x.com/job", "x.com/*")
            db.commit()

        with Session(engine) as db:
            traj = db.get(JobDiscoveryTrajectory, tid)
            assert traj is not None
            assert traj.overall_status == "failed"
            assert traj.failed_at_step == 1
            assert traj.failed_tool == "s2"
            assert traj.failed_error_message == "bad input"
            assert traj.failed_error_reason == "unknown"  # "bad input" doesn't match any keyword

    def test_save_with_fallback(self, engine):
        """Failure at index 0 followed by supervisor fallback steps."""
        buf = TrajectoryBuffer(task_id="t3", strategy_id="s3", executor_type="snapshot")
        buf.record_step("open_url", "failed", {}, None, error=ConnectionError("timeout"))
        # Supervisor fallback steps
        buf.record_step("open_url_retry", "ok", {}, "fallback html")
        buf.record_step("extract_jd", "ok", {}, [{"title": "Engineer"}])

        result = DiscoveryRunResult(status="partial_success", summary="fallback completed")

        with Session(engine) as db:
            tid = save_trajectory(db, buf, result, "https://x.com/job", "x.com/*")
            db.commit()

        with Session(engine) as db:
            traj = db.get(JobDiscoveryTrajectory, tid)
            assert traj is not None
            assert traj.overall_status == "partial_fallback"
            assert traj.failed_at_step == 0
            assert traj.failed_tool == "open_url"
            assert traj.failed_error_reason == "network_timeout"
            assert traj.completed_steps == []  # no completed steps before failure
            assert traj.fallback_trace is not None
            assert len(traj.fallback_trace) == 2  # both fallback steps

    def test_save_no_url_pattern(self, engine):
        """url_pattern can be None."""
        buf = TrajectoryBuffer(task_id="t4", strategy_id="s4", executor_type="supervisor")
        buf.record_step("triage", "ok", {}, {"type": "wechat"})

        result = DiscoveryRunResult(status="succeeded", summary="ok")

        with Session(engine) as db:
            tid = save_trajectory(db, buf, result, "https://mp.weixin.qq.com/s/xxx", None)
            db.commit()

        with Session(engine) as db:
            traj = db.get(JobDiscoveryTrajectory, tid)
            assert traj is not None
            assert traj.url_pattern is None
            assert traj.overall_status == "succeeded"
            assert len(traj.completed_steps) == 1


class TestScheduleAnnotation:
    def test_annotation_field_set(self, engine):
        buf = TrajectoryBuffer(task_id="t5", strategy_id=None, executor_type="supervisor")
        buf.record_step("open_url", "ok", {}, "ok")

        result = DiscoveryRunResult(status="succeeded", summary="ok")

        with Session(engine) as db:
            tid = save_trajectory(db, buf, result, "https://x.com", "x.com/*")
            schedule_annotation(db, tid)
            db.commit()

        with Session(engine) as db:
            traj = db.get(JobDiscoveryTrajectory, tid)
            assert traj is not None
            # Annotation scheduled flag stored in annotations JSON
            assert traj.annotations is not None
            assert traj.annotations.get("_annotation_pending") is True

    def test_schedule_nonexistent(self, engine):
        """schedule_annotation on a missing ID is a no-op."""
        with Session(engine) as db:
            schedule_annotation(db, "nonexistent-id")
            db.commit()
            # No exception raised


class TestGetPendingAnnotations:
    def test_returns_pending_only(self, engine):
        """get_pending_annotations returns only trajectories with _annotation_pending."""
        buf1 = TrajectoryBuffer(task_id="t6", strategy_id=None, executor_type="supervisor")
        buf1.record_step("step1", "ok", {}, "ok")

        buf2 = TrajectoryBuffer(task_id="t7", strategy_id=None, executor_type="snapshot")
        buf2.record_step("step1", "ok", {}, "ok")

        result = DiscoveryRunResult(status="succeeded", summary="ok")

        with Session(engine) as db:
            tid1 = save_trajectory(db, buf1, result, "https://x.com/1", "x.com/*")
            schedule_annotation(db, tid1)

            tid2 = save_trajectory(db, buf2, result, "https://x.com/2", "x.com/*")
            # tid2 NOT scheduled for annotation

            db.commit()

        with Session(engine) as db:
            pending = get_pending_annotations(db)
            ids = [t.id for t in pending]
            assert tid1 in ids
            assert tid2 not in ids
            assert len(pending) == 1
