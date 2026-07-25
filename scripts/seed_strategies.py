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
from backend.app.services.job_discovery.adapters.feishu import FEISHU_CRAWL_PLAN
from backend.app.services.job_discovery.adapters.inovance import INOVANCE_CRAWL_PLAN
from backend.app.services.job_discovery.adapters.xiaohongshu import XHS_CRAWL_PLAN

# Use the same engine pattern as tests
from backend.app.config import Settings


# ---------------------------------------------------------------------------
# Gray rollout order (plan Task 8 Steps 5-6).
#
# Each adapter below ships ``enabled=False`` (gray) so the site continues to
# flow through legacy PATH C until it is explicitly promoted. Promotion rules:
#   1. The site must pass three consecutive coverage-verified live smokes
#      (``tests/manual/test_pev_live_smoke.py``) with a stable listing count.
#   2. Only then flip THIS site's ``JobDiscoveryStrategy.enabled`` to True.
#   3. Promote in this order only; do not skip ahead:
# ---------------------------------------------------------------------------
GRAY_ROLLOUT_ORDER: tuple[str, ...] = (
    "moka",        # 1. Moka       (app.mokahr.com/*)
    "feishu",      # 2. 飞书       (*.jobs.feishu.cn/*)
    "inovance",    # 3. 汇川       (recruit.inovance.com/*)
    "xiaohongshu", # 4. 小红书     (job.xiaohongshu.com/*)
)

# Per-site rollback triggers (plan Task 8 Step 6). When ANY of these fire for a
# promoted site, disable ONLY that site's strategy (flip ``enabled=False``);
# do not touch other sites, the new contracts, or the global result invariant.
GRAY_ROLLBACK_TRIGGERS: tuple[str, ...] = (
    "expected/raw listing count drift",
    "positive terminal signal lost (no completion_evidence)",
    "detail failure > 0 (failed_detail_count)",
    "listpage apply URL > 0 (count_apply_url_is_listpage)",
    "new blocked marker (login/captcha/anti-bot/permission_denied)",
    "listing count inconsistent across 3 consecutive runs",
)


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
        # Gray rollout #1/4 (Moka) - PATH A driver + PATH B executor. Disabled
        # by default until three consecutive coverage-verified live smokes pass;
        # promote first, per GRAY_ROLLOUT_ORDER.
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
        # Gray rollout #2/4 (飞书) - PATH A driver + PATH B executor. Disabled
        # by default until three consecutive coverage-verified live smokes pass;
        # promote after Moka, per GRAY_ROLLOUT_ORDER.
        JobDiscoveryStrategy(
            url_pattern="*.jobs.feishu.cn/*",
            site_type="career_site",
            description="飞书招聘 -> search XHR 跟随 total 翻页 -> /campus/position/{id}/detail 完整抓取",
            priority=40,
            adapter="backend.app.services.job_discovery.adapters.feishu.FeishuCrawlAdapter",
            plan_yaml=FEISHU_CRAWL_PLAN,
            degradation_threshold=3,
            recovery_threshold=2,
            enabled=False,
        ),
        # Gray rollout #3/4 (汇川) - PATH A driver + PATH B executor. Disabled
        # by default until three consecutive coverage-verified live smokes pass;
        # promote after 飞书, per GRAY_ROLLOUT_ORDER.
        JobDiscoveryStrategy(
            url_pattern="recruit.inovance.com/*",
            site_type="career_site",
            description="汇川招聘 SPA -> 渲染 DOM 抽取 #/jobs/<uuid> 路由 -> 完整抓取",
            priority=40,
            adapter="backend.app.services.job_discovery.adapters.inovance.InovanceCrawlAdapter",
            plan_yaml=INOVANCE_CRAWL_PLAN,
            degradation_threshold=3,
            recovery_threshold=2,
            enabled=False,
        ),
        # Gray rollout #4/4 (小红书) - PATH A driver + PATH B executor. Disabled
        # by default until three consecutive coverage-verified live smokes pass;
        # promote last, per GRAY_ROLLOUT_ORDER.
        JobDiscoveryStrategy(
            url_pattern="job.xiaohongshu.com/*",
            site_type="career_site",
            description="小红书招聘 -> search XHR 跟随 cursor/total 翻页 -> /campus/position/{id} 完整抓取",
            priority=40,
            adapter="backend.app.services.job_discovery.adapters.xiaohongshu.XiaohongshuCrawlAdapter",
            plan_yaml=XHS_CRAWL_PLAN,
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
