"""Seed the strategy library with initial well-known patterns.

Usage:
    python scripts/seed_strategies.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryStrategy
from backend.app.services.job_discovery.adapters.moka import MOKA_CRAWL_PLAN

# Use the same engine pattern as tests
from backend.app.config import Settings


WECHAT_PLAN = """plan:
  - tool: triage_link
    params:
      url: "{{task.url}}"
    expect: "classify URL as wechat article"
    on_error: "skip"
  - tool: run_web_navigation
    params:
      start_url: "{{task.url}}"
    expect: "fetch wechat article via ReadGZH"
    on_error: "retry_with_fallback"
  - tool: parse_wechat_article
    params:
      html: "{{prev.result.text}}"
      url: "{{task.url}}"
    expect: "extract article text and images"
    on_error: "mark_manual_review"
  - tool: extract_jd_candidates
    params:
      page_text: "{{prev.result.text}}"
      url: "{{task.url}}"
    expect: "extract structured JD candidates"
    on_error: "retry_then_skip"
  - tool: verify_evidence
    params:
      candidates_json: "{{prev.result}}"
      evidence_json: "{{evidence_json}}"
    expect: "verify candidates against evidence"
    on_error: "skip"
  - tool: package_candidates
    params:
      candidates_json: "{{prev.result}}"
      evidence_hash: "{{task.evidence_hash}}"
      source_key: "{{task.source_key}}"
    expect: "package final candidates"
"""

ALIBABA_PLAN = """plan: []
# Alibaba SPA uses the DomainAdapter fast lane (adapter field set below).
# The YAML plan is intentionally empty — all logic is in the adapter code.
"""


def seed(db: Session) -> None:
    existing = db.query(JobDiscoveryStrategy).count()
    if existing > 0:
        print(f"Already have {existing} strategies — skipping seed.")
        return

    strategies = [
        JobDiscoveryStrategy(
            url_pattern="mp.weixin.qq.com/s/*",
            site_type="wechat",
            description="微信公众号文章 → ReadGZH → 文本提取 → JD 提取",
            priority=10,
            adapter=None,
            plan_yaml=WECHAT_PLAN,
            degradation_threshold=3,
            recovery_threshold=2,
        ),
        JobDiscoveryStrategy(
            url_pattern="*talent.alibaba.com/*",
            site_type="spa",
            description="阿里巴巴校园招聘 SPA → API 直调 → JD 提取",
            priority=10,
            adapter="backend.app.services.job_discovery.adapters.alibaba_spa.AlibabaSPAAdapter",
            plan_yaml=ALIBABA_PLAN,
            degradation_threshold=3,
            recovery_threshold=2,
        ),
        # Gray rollout (PATH A driver + PATH B executor). Disabled by default
        # until three consecutive coverage-verified live smokes pass; enable
        # only in a test environment by flipping this row manually.
        JobDiscoveryStrategy(
            url_pattern="app.mokahr.com/*",
            site_type="career_site",
            description="Moka 招聘 SPA -> 渲染 DOM 抽取 #/job/ 路由 -> 完整抓取",
            priority=40,
            adapter="backend.app.services.job_discovery.adapters.moka.MokaCrawlAdapter",
            plan_yaml=MOKA_CRAWL_PLAN,
            degradation_threshold=3,
            recovery_threshold=2,
            enabled=False,
        ),
    ]
    db.add_all(strategies)
    db.commit()
    print(f"Seeded {len(strategies)} strategies.")


if __name__ == "__main__":
    settings = Settings(
        app_auth_secret="x" * 32,
        database_url=os.environ.get("DATABASE_URL", "sqlite:///seed_test.db"),
        redis_url="redis://localhost:6379/0",
        object_encryption_key="x" * 32,
    )
    from sqlalchemy import create_engine

    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed(db)
