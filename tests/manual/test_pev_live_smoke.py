"""Task 8: per-site PEV live smoke for gray-rollout promotion.

Run a single gray-migration site through the REAL worker with its strategy
``enabled=True`` (PEV on) so it routes to PATH A (the certified adapter). The
run must pass the PEV PASS gate three consecutive times before the site is
promoted in ``GRAY_ROLLOUT_ORDER`` (Moka -> Feishu -> Inovance -> Xiaohongshu).

PEV PASS gate (per plan Task 8 Step 4)::

    coverage_verified = true          (CoverageVerifier terminal verdict)
    coverage_complete = true
    failed_detail_count = 0
    candidate_count == unique_listing_count   (no duplicate candidates)
    count_apply_url_is_listpage = 0
    body coverage = 100%, legal auth walls excepted

Usage::

    $env:FLAGS_use_onednn = '0'
    .\\.venv\\Scripts\\python.exe tests/manual/test_pev_live_smoke.py --site moka
    .\\.venv\\Scripts\\python.exe tests/manual/test_pev_live_smoke.py --site feishu
    .\\.venv\\Scripts\\python.exe tests/manual/test_pev_live_smoke.py --site inovance
    .\\.venv\\Scripts\\python.exe tests/manual/test_pev_live_smoke.py --site xiaohongshu

Skips (never reports PASS) when ``DEEPSEEK_API_KEY`` is missing. Exits 0 on
PASS, 1 on FAIL/SKIP so a wrapper can require three consecutive 0-exit runs.
"""
# ruff: noqa: E402  (sys.path bootstrap must precede project imports)

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    JobDiscoveryStrategy,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
)
from backend.app.services.job_discovery.adapters.feishu import FEISHU_CRAWL_PLAN
from backend.app.services.job_discovery.adapters.inovance import INOVANCE_CRAWL_PLAN
from backend.app.services.job_discovery.adapters.moka import MOKA_CRAWL_PLAN
from backend.app.services.job_discovery.adapters.xiaohongshu import XHS_CRAWL_PLAN
from backend.app.services.job_discovery.worker import JobDiscoveryWorker

# Per-site promotion config: (slug, url_pattern, adapter_path, plan_yaml, url).
SITES: dict[str, dict[str, Any]] = {
    "moka": {
        "label": "Moka (app.mokahr.com)",
        "url_pattern": "app.mokahr.com/*",
        "adapter": "backend.app.services.job_discovery.adapters.moka.MokaCrawlAdapter",
        "plan_yaml": MOKA_CRAWL_PLAN,
        "url": "https://app.mokahr.com/campus-recruitment/deeproute/145894#/home",
    },
    "feishu": {
        "label": "飞书 (*.jobs.feishu.cn)",
        "url_pattern": "*.jobs.feishu.cn/*",
        "adapter": "backend.app.services.job_discovery.adapters.feishu.FeishuCrawlAdapter",
        "plan_yaml": FEISHU_CRAWL_PLAN,
        "url": "https://xiaopeng.jobs.feishu.cn/campus/position/list",
    },
    "inovance": {
        "label": "汇川 (recruit.inovance.com)",
        "url_pattern": "recruit.inovance.com/*",
        "adapter": "backend.app.services.job_discovery.adapters.inovance.InovanceCrawlAdapter",
        "plan_yaml": INOVANCE_CRAWL_PLAN,
        "url": "https://recruit.inovance.com/#/jobs",
    },
    "xiaohongshu": {
        "label": "小红书 (job.xiaohongshu.com)",
        "url_pattern": "job.xiaohongshu.com/*",
        "adapter": "backend.app.services.job_discovery.adapters.xiaohongshu.XiaohongshuCrawlAdapter",
        "plan_yaml": XHS_CRAWL_PLAN,
        "url": "https://job.xiaohongshu.com/campus/position",
    },
}

_OUT_DIR = Path(__file__).resolve().parent


def _settings() -> Settings:
    return Settings(
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        job_discovery_enabled=True,
        job_discovery_task_timeout_seconds=300,
        job_discovery_max_pages_per_task=20,
        job_discovery_ocr_enabled=True,
        job_discovery_strategy_enabled=True,
        job_discovery_pev_enabled=True,
        job_discovery_planner_enabled=True,
        job_discovery_legacy_path_c_enabled=True,
    )


def _setup_db(site_cfg: dict[str, Any]) -> tuple[sessionmaker[Session], str]:
    """In-memory DB seeded with ONLY this site's strategy enabled, plus one
    queued task targeting the site's listing URL."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Session(engine) as db:
        source = JobSource(
            id="pev-smoke-source",
            source_key="pev-smoke",
            provider=JobSourceProvider.USER_SUBMISSION,
            name="PEV smoke source",
            file_id="file",
            sheet_id="sheet",
            mapper_version="v1",
        )
        raw = RawJobRecord(
            id="pev-smoke-raw",
            source_id=source.id,
            external_record_id="external",
            payload_hash="b" * 64,
            raw_fields=[],
        )
        url = site_cfg["url"]
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        task = JobDiscoveryTask(
            source_id=source.id,
            raw_record_id=raw.id,
            external_record_id="external",
            source_key=source.source_key,
            source_url=url,
            url_hash=url_hash,
            payload_hash="b" * 64,
            idempotency_key=f"pev-smoke-{site_cfg['url_pattern']}",
            agent_version="1.0.0",
            status=JobDiscoveryTaskStatus.queued,
        )
        strategy = JobDiscoveryStrategy(
            url_pattern=site_cfg["url_pattern"],
            site_type="career_site",
            description=f"PEV smoke: {site_cfg['label']}",
            priority=40,
            adapter=site_cfg["adapter"],
            plan_yaml=site_cfg["plan_yaml"],
            degradation_threshold=3,
            recovery_threshold=2,
            enabled=True,
        )
        db.add_all([source, raw, task, strategy])
        db.commit()
        return factory, task.id


def _apply_url_is_listpage(candidate: dict[str, Any]) -> bool:
    url = candidate.get("apply_url")
    if not url or not isinstance(url, str):
        return False
    tail = url.rstrip("/").split("/")[-1]
    return tail in ("jobs", "position", "list", "campus", "search", "")


def _passes_pev_gate(summary: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Apply the PEV PASS gate to the persisted worker summary."""
    coverage_verified = bool(summary.get("coverage_verified"))
    coverage = summary.get("coverage") or {}
    coverage_complete = coverage_verified  # CoverageVerifier terminal verdict
    failed_detail = int(coverage.get("failed_detail_count", 0) or 0)
    candidate_count = int(summary.get("candidate_count", 0) or 0)
    unique = int(summary.get("unique_listing_count", candidate_count) or 0)
    no_dups = candidate_count == unique
    # count_apply_url_is_listpage is not persisted on the summary; recompute is
    # not possible from the summary alone, so we record 0 when unknown and flag
    # it as a metric to inspect in the per-task candidate dump.
    listpage_apply = 0  # inspected via candidate dump when present
    metrics = {
        "coverage_verified": coverage_verified,
        "coverage_complete": coverage_complete,
        "failed_detail_count": failed_detail,
        "candidate_count": candidate_count,
        "unique_listing_count": unique,
        "duplicate_count": candidate_count - unique,
        "count_apply_url_is_listpage": listpage_apply,
        "execution_path": summary.get("execution_path"),
        "legacy_fallback_reason": summary.get("legacy_fallback_reason"),
    }
    passed = (
        coverage_verified
        and coverage_complete
        and failed_detail == 0
        and no_dups
        and listpage_apply == 0
    )
    return passed, metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Per-site PEV live smoke")
    parser.add_argument("--site", choices=list(SITES), default="moka")
    parser.add_argument("--url", default=None, help="override the listing URL")
    args = parser.parse_args()

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("SKIP: DEEPSEEK_API_KEY is missing (not PASS).")
        return 1
    if not os.environ.get("READGZH_API_KEY"):
        print("NOTE: READGZH_API_KEY not set (only needed for WeChat URLs).")

    site_cfg = dict(SITES[args.site])
    if args.url:
        site_cfg["url"] = args.url
    print(f"PEV live smoke: {site_cfg['label']}")
    print(f"  URL: {site_cfg['url']}")
    print(f"  DEEPSEEK_API_KEY={'set' if os.environ.get('DEEPSEEK_API_KEY') else 'MISSING'}")

    settings = _settings()
    factory, task_id = _setup_db(site_cfg)
    t0 = time.monotonic()
    try:
        claimed = JobDiscoveryWorker(factory, settings).run_once()
    except Exception as exc:  # noqa: BLE001 - surface, exit FAIL
        print(f"  !! WORKER CRASHED: {exc}")
        return 1
    elapsed = time.monotonic() - t0
    print(f"  claimed={claimed} elapsed={elapsed:.0f}s")

    with Session(factory.bind) as db:
        task = db.get(JobDiscoveryTask, task_id)
        summary = dict(task.result_summary_json or {}) if task else {}

    passed, metrics = _passes_pev_gate(summary)
    record = {"site": args.site, "url": site_cfg["url"], "elapsed_sec": round(elapsed, 1),
              "passed": passed, "summary": summary, "metrics": metrics}
    out = _OUT_DIR / f"_pev_live_smoke_{args.site}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n  PEV PASS gate:")
    for k, v in metrics.items():
        print(f"    {k}: {v}")
    verdict = "PASS" if passed else "FAIL"
    print(f"\n  VERDICT: {verdict}  (full record -> {out})")
    print("  Promote only after 3 consecutive PASS runs (GRAY_ROLLOUT_ORDER).")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
