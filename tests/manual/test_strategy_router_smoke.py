"""Smoke test: StrategyRouter matches 4 known URLs and produces expected results.

Usage:
    python tests/manual/test_strategy_router_smoke.py

Requires: seeded strategy DB + running backend (or use SQLite test DB).
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryStrategy
from backend.app.services.job_discovery.strategy.strategy_router import StrategyRouter


TEST_URLS = [
    ("https://mp.weixin.qq.com/s/abc123?token=xyz", "wechat", True),
    ("https://mp.weixin.qq.com/s/def456", "wechat", True),
    ("https://campus-talent.alibaba.com/search?q=java", "spa", True),
    ("https://talent.alibaba.com/position/123", "spa", True),
    ("https://www.baidu.com/jobs/unknown", None, False),  # no match
]


def main():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    # Seed
    from scripts.seed_strategies import seed

    with Session(engine) as db:
        seed(db)

    with Session(engine) as db:
        router = StrategyRouter(db)
        passed = 0
        failed = 0
        for url, expected_site_type, expect_match in TEST_URLS:
            result = router.match(url)
            if expect_match:
                if result is None:
                    print(f"FAIL: {url} expected match but got None")
                    failed += 1
                elif expected_site_type and result.site_type != expected_site_type:
                    print(f"FAIL: {url} expected {expected_site_type}, got {result.site_type}")
                    failed += 1
                else:
                    print(f"PASS: {url} → {result.site_type} ({result.description})")
                    passed += 1
            else:
                if result is not None:
                    print(f"FAIL: {url} expected no match, got {result.site_type}")
                    failed += 1
                else:
                    print(f"PASS: {url} → no match (correct)")
                    passed += 1

    print(f"\n{passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
