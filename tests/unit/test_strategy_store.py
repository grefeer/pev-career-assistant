"""Unit tests for strategy_store -- in-memory SQLite."""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryStrategy
from backend.app.services.job_discovery.strategy.strategy_store import (
    get_active_strategies,
    get_strategy_by_id,
    increment_error_count,
    increment_success,
    get_strategies_due_for_health_check,
    record_health_check,
)


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        yield s


class TestGetActiveStrategies:
    def test_returns_only_active_and_degraded(self, db):
        s1 = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []", status="active")
        s2 = JobDiscoveryStrategy(url_pattern="b/*", site_type="spa", plan_yaml="plan: []", status="degraded")
        s3 = JobDiscoveryStrategy(url_pattern="c/*", site_type="other", plan_yaml="plan: []", status="unavailable")
        db.add_all([s1, s2, s3])
        db.commit()

        result = get_active_strategies(db)
        assert len(result) == 2
        statuses = {s.status for s in result}
        assert statuses == {"active", "degraded"}

    def test_returns_enabled_only(self, db):
        s1 = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []", enabled=True)
        s2 = JobDiscoveryStrategy(url_pattern="b/*", site_type="spa", plan_yaml="plan: []", enabled=False)
        db.add_all([s1, s2])
        db.commit()

        result = get_active_strategies(db)
        assert len(result) == 1
        assert result[0].url_pattern == "a/*"


class TestIncrementErrorCount:
    def test_atomic_increment(self, db):
        s = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []", error_count=2)
        db.add(s)
        db.commit()
        sid = s.id

        increment_error_count(db, sid, {"tool": "extract", "reason": "empty_text", "message": "no text"})
        db.commit()

        # Re-fetch
        updated = get_strategy_by_id(db, sid)
        assert updated is not None
        assert updated.error_count == 3
        assert updated.last_error_tool == "extract"
        assert updated.last_error_reason == "empty_text"
        assert updated.last_error_at is not None

    def test_marks_unavailable_at_threshold(self, db):
        s = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []",
                                 error_count=2, degradation_threshold=3)
        db.add(s)
        db.commit()
        sid = s.id

        increment_error_count(db, sid, {"tool": "x", "reason": "unknown", "message": "e"})
        db.commit()

        updated = get_strategy_by_id(db, sid)
        assert updated is not None
        assert updated.status == "unavailable"


class TestIncrementSuccess:
    def test_resets_error_count(self, db):
        s = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []",
                                 error_count=2, consecutive_ok=0, status="degraded")
        db.add(s)
        db.commit()
        sid = s.id

        increment_success(db, sid, duration_s=45.0)
        db.commit()

        updated = get_strategy_by_id(db, sid)
        assert updated is not None
        assert updated.error_count == 0
        assert updated.consecutive_ok == 1
        assert updated.success_runs == 1
        assert updated.total_runs == 1
        assert updated.avg_duration_s == 45.0

    def test_recovery_from_degraded(self, db):
        s = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []",
                                 consecutive_ok=1, status="degraded",
                                 degradation_threshold=3, recovery_threshold=2)
        db.add(s)
        db.commit()
        sid = s.id

        increment_success(db, sid)
        db.commit()

        updated = get_strategy_by_id(db, sid)
        assert updated is not None
        assert updated.status == "active"


class TestHealthCheck:
    def test_returns_strategies_due(self, db):
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=25)
        recent = now - timedelta(hours=1)

        s1 = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []",
                                  last_health_check_at=old, status="active")
        s2 = JobDiscoveryStrategy(url_pattern="b/*", site_type="spa", plan_yaml="plan: []",
                                  last_health_check_at=recent, status="active")
        s3 = JobDiscoveryStrategy(url_pattern="c/*", site_type="other", plan_yaml="plan: []",
                                  last_health_check_at=None, status="active")
        db.add_all([s1, s2, s3])
        db.commit()

        due = get_strategies_due_for_health_check(db, interval_hours=24)
        patterns = {s.url_pattern for s in due}
        assert "a/*" in patterns
        assert "c/*" in patterns       # never checked
        assert "b/*" not in patterns   # checked 1h ago

    def test_record_health_check_ok(self, db):
        s = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []")
        db.add(s)
        db.commit()
        sid = s.id

        record_health_check(db, sid, ok=True, detail="all good")
        db.commit()

        updated = get_strategy_by_id(db, sid)
        assert updated is not None
        assert updated.last_health_check_at is not None

    def test_record_health_check_fail_increments_error(self, db):
        s = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []",
                                 error_count=0)
        db.add(s)
        db.commit()
        sid = s.id

        record_health_check(db, sid, ok=False, detail="404")
        db.commit()

        updated = get_strategy_by_id(db, sid)
        assert updated is not None
        assert updated.error_count == 1
        assert updated.last_error_reason == "site_changed"
