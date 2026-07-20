"""Unit tests for Strategy and Trajectory ORM models."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryStrategy, JobDiscoveryTrajectory


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


class TestJobDiscoveryStrategy:
    def test_create_minimal(self, engine):
        s = JobDiscoveryStrategy(
            url_pattern="mp.weixin.qq.com/s/*",
            site_type="wechat",
            description="Test strategy",
            plan_yaml="plan: []",
        )
        with Session(engine) as db:
            db.add(s)
            db.commit()
            db.refresh(s)
            assert s.id is not None
            assert s.status == "active"
            assert s.enabled is True
            assert s.error_count == 0
            assert s.consecutive_ok == 0
            assert s.degradation_threshold == 3

    def test_create_with_adapter(self, engine):
        s = JobDiscoveryStrategy(
            url_pattern="campus*.alibaba.com/*",
            site_type="spa",
            description="Ali SPA",
            plan_yaml="plan: []",
            adapter="adapters.alibaba_spa.AlibabaSPAAdapter",
            priority=10,
        )
        with Session(engine) as db:
            db.add(s)
            db.commit()
            assert s.adapter == "adapters.alibaba_spa.AlibabaSPAAdapter"
            assert s.priority == 10

    def test_url_pattern_indexed(self, engine):
        """Verify url_pattern column is queryable (indexed)."""
        s1 = JobDiscoveryStrategy(url_pattern="example.com/a/*", site_type="other", plan_yaml="plan: []")
        s2 = JobDiscoveryStrategy(url_pattern="example.com/b/*", site_type="other", plan_yaml="plan: []")
        with Session(engine) as db:
            db.add_all([s1, s2])
            db.commit()
        with Session(engine) as db:
            from sqlalchemy import select
            rows = db.scalars(select(JobDiscoveryStrategy).where(
                JobDiscoveryStrategy.url_pattern.like("example.com/%")
            )).all()
            assert len(rows) == 2


class TestJobDiscoveryTrajectory:
    def test_create_basic(self, engine):
        t = JobDiscoveryTrajectory(
            task_id="task-1",
            executor_type="supervisor",
            overall_status="completed",
            url="https://example.com/job",
            url_pattern="example.com/*",
            completed_steps=[{"tool": "open_url", "ok": True}],
            annotations={"reusability_score": 0.5},
        )
        with Session(engine) as db:
            db.add(t)
            db.commit()
            db.refresh(t)
            assert t.id is not None
            assert t.overall_status == "completed"
            assert t.completed_steps == [{"tool": "open_url", "ok": True}]

    def test_create_with_failure(self, engine):
        t = JobDiscoveryTrajectory(
            task_id="task-2",
            executor_type="snapshot",
            overall_status="partial_fallback",
            url="https://example.com/job",
            url_pattern="example.com/*",
            failed_at_step=3,
            failed_tool="extract_jd_candidates",
            failed_error_message="empty text",
            failed_error_reason="empty_text",
            completed_steps=[
                {"tool": "open_url", "ok": True},
                {"tool": "parse_wechat_article", "ok": True},
            ],
            fallback_trace=[
                {"tool": "run_ocr", "ok": True},
                {"tool": "extract_jd_candidates", "ok": True},
            ],
        )
        with Session(engine) as db:
            db.add(t)
            db.commit()
            db.refresh(t)
            assert t.failed_at_step == 3
            assert t.failed_error_reason == "empty_text"
            assert len(t.completed_steps) == 2
            assert len(t.fallback_trace) == 2

    def test_strategy_id_nullable(self, engine):
        t = JobDiscoveryTrajectory(
            task_id="task-3",
            executor_type="supervisor",
            overall_status="completed",
            url="https://example.com/job",
            url_pattern="example.com/*",
            completed_steps=[],
            strategy_id=None,
        )
        with Session(engine) as db:
            db.add(t)
            db.commit()
            assert t.strategy_id is None

    def test_timestamp_auto(self, engine):
        t = JobDiscoveryTrajectory(
            task_id="task-4",
            executor_type="adapter",
            overall_status="completed",
            url="https://example.com/job",
            url_pattern="example.com/*",
            completed_steps=[],
        )
        with Session(engine) as db:
            db.add(t)
            db.commit()
            db.refresh(t)
            assert t.created_at is not None
