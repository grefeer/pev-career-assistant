"""Unit tests for StrategyRouter."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryStrategy
from backend.app.services.job_discovery.strategy.strategy_router import StrategyRouter


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


class TestStrategyRouter:
    def test_match_wechat_pattern(self, engine):
        s = JobDiscoveryStrategy(
            url_pattern="mp.weixin.qq.com/s/*",
            site_type="wechat",
            plan_yaml="plan: []",
            priority=10,
        )
        with Session(engine) as db:
            db.add(s)
            db.commit()

        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://mp.weixin.qq.com/s/abc123?token=xyz")
            assert result is not None
            assert result.url_pattern == "mp.weixin.qq.com/s/*"

    def test_match_alibaba_pattern(self, engine):
        s = JobDiscoveryStrategy(
            url_pattern="campus*.alibaba.com/*",
            site_type="spa",
            plan_yaml="plan: []",
            priority=10,
        )
        with Session(engine) as db:
            db.add(s)
            db.commit()

        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://campus-talent.alibaba.com/search?q=java")
            assert result is not None

    def test_no_match_returns_none(self, engine):
        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://unknown-site.com/jobs/123")
            assert result is None

    def test_highest_priority_wins(self, engine):
        s1 = JobDiscoveryStrategy(url_pattern="*.example.com/*", site_type="other", plan_yaml="plan: []", priority=1)
        s2 = JobDiscoveryStrategy(url_pattern="jobs.example.com/*", site_type="other", plan_yaml="plan: []", priority=10)
        with Session(engine) as db:
            db.add_all([s1, s2])
            db.commit()

        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://jobs.example.com/job/1")
            assert result is not None
            assert result.priority == 10

    def test_skip_unavailable(self, engine):
        s = JobDiscoveryStrategy(
            url_pattern="mp.weixin.qq.com/s/*",
            site_type="wechat",
            plan_yaml="plan: []",
            status="unavailable",
        )
        with Session(engine) as db:
            db.add(s)
            db.commit()

        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://mp.weixin.qq.com/s/abc123")
            assert result is None

    def test_degraded_still_matches(self, engine):
        s = JobDiscoveryStrategy(
            url_pattern="mp.weixin.qq.com/s/*",
            site_type="wechat",
            plan_yaml="plan: []",
            status="degraded",
        )
        with Session(engine) as db:
            db.add(s)
            db.commit()

        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://mp.weixin.qq.com/s/abc123")
            assert result is not None

    def test_same_priority_highest_success_count_wins(self, engine):
        s1 = JobDiscoveryStrategy(url_pattern="*.x.com/*", site_type="other", plan_yaml="plan: []",
                                  priority=5, success_count=10)
        s2 = JobDiscoveryStrategy(url_pattern="a.x.com/*", site_type="other", plan_yaml="plan: []",
                                  priority=5, success_count=100)
        with Session(engine) as db:
            db.add_all([s1, s2])
            db.commit()

        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://a.x.com/job")
            assert result is not None
            assert result.success_count == 100
