"""Worker-routed ten-URL evaluator.

Certified sites are executed through PATH A/PEV; all other rows are explicitly
reported as Legacy-only unless a configured Worker execution can run them.  The
output never counts a coverage-unverified result as a PEV pass.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy.orm import Session

from backend.app.db.models import JobDiscoveryTask
from backend.app.services.job_discovery.worker import JobDiscoveryWorker
from tests.manual.test_pev_live_smoke import (
    SITES as PEV_SITE_CONFIG,
    _passes_pev_gate,
    _settings,
    _setup_db,
)


URLS: tuple[tuple[str, str, str], ...] = (
    ("deeproute", "元戎启行", "https://app.mokahr.com/campus-recruitment/deeproute/145894#/home"),
    ("pdd", "拼多多", "https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x"),
    ("feishu-xiaopeng", "小鹏汽车", "https://xiaopeng.jobs.feishu.cn/campus/position/list"),
    ("inovance", "汇川技术", "https://recruit.inovance.com/#/jobs"),
    ("xiaohongshu", "小红书", "https://job.xiaohongshu.com/campus/position"),
    ("didi", "滴滴", "https://talent.didiglobal.com/campus/"),
    ("netease", "网易", "https://hr.163.com/campus.html"),
    ("baidu", "百度", "https://talent.baidu.com/jobs/campus/list"),
    ("bytedance", "字节跳动", "https://jobs.bytedance.com/campus/position"),
    ("xiaomi", "小米", "https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY"),
)

# Maps URL slugs to the certified smoke-site key.  Only these rows may receive
# a PEV verdict; every other row remains explicitly Legacy/blocked.
PEV_SITES = {
    "deeproute": "moka",
    "pdd": "pdd",
    "xiaomi": "xiaomi",
    "bytedance": "bytedance",
    "feishu-xiaopeng": "feishu",
    "inovance": "inovance",
    "xiaohongshu": "xiaohongshu",
}

_OUT = Path(__file__).resolve().parent / "_worker_ten_url_eval_summary.json"
_ROW_OUT_DIR = Path(__file__).resolve().parent


def _bucket_for(summary: dict[str, object]) -> str:
    if bool(summary.get("coverage_verified")):
        return "pev_pass"
    if summary.get("execution_path") == "path_a_adapter":
        return "pev_fail"
    return "legacy"


def _timeout_row(slug: str, company: str, url: str, timeout_sec: int) -> dict[str, Any]:
    """Return an explicit, non-success result for an isolated site timeout."""
    return {
        "slug": slug,
        "company": company,
        "url": url,
        "target_path": "pev" if slug in PEV_SITES else "legacy",
        "status": "timed_out",
        "bucket": "timed_out",
        "timeout_sec": timeout_sec,
    }


def _row_path(slug: str) -> Path:
    return _ROW_OUT_DIR / f"_worker_ten_url_eval_{slug}.json"


def _write_row(row: dict[str, Any]) -> None:
    _row_path(str(row["slug"])).write_text(
        json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_summary(rows: list[dict[str, Any]]) -> None:
    buckets: dict[str, int] = {}
    for row in rows:
        bucket = str(row["bucket"])
        buckets[bucket] = buckets.get(bucket, 0) + 1
    _OUT.write_text(
        json.dumps({"rows": rows, "buckets": buckets}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _run_pev_row(slug: str, company: str, url: str) -> dict[str, Any]:
    site_cfg = dict(PEV_SITE_CONFIG[PEV_SITES[slug]])
    site_cfg["url"] = url
    factory, task_id = _setup_db(site_cfg)
    started = time.monotonic()
    JobDiscoveryWorker(factory, _settings()).run_once()
    elapsed = time.monotonic() - started
    with factory() as db:
        task = db.get(JobDiscoveryTask, task_id)
        summary = dict(task.result_summary_json or {}) if task else {}
    passed, metrics = _passes_pev_gate(summary)
    return {
        "slug": slug,
        "company": company,
        "url": url,
        "target_path": "pev",
        "status": "succeeded" if passed else "failed",
        "bucket": "pev_pass" if passed else "pev_fail",
        "elapsed_sec": round(elapsed, 1),
        "candidate_count": metrics["candidate_count"],
        "body_candidate_count": metrics["body_candidate_count"],
        "unique_listing_count": metrics["unique_listing_count"],
        "failed_detail_count": metrics["failed_detail_count"],
        "coverage_verified": metrics["coverage_verified"],
    }


def _load_legacy_evaluator():
    """Load the direct-supervisor evaluator only in its isolated child process."""
    path = _PROJECT_ROOT / "tests" / "integration" / "job_discovery" / "test_supervisor_ten_url_eval.py"
    spec = importlib.util.spec_from_file_location("legacy_ten_url_eval", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_legacy_row(slug: str, company: str, url: str) -> dict[str, Any]:
    """Run the production legacy fallback in a fresh process.

    A no-strategy task routes to the generic supervisor.  It is deliberately
    kept separate from PEV: no coverage-unverified result can become a PEV pass.
    """
    from src.utils import load_env

    load_env()
    if not os.environ.get("DEEPSEEK_API_KEY") and os.environ.get("OPENAI_API_KEY"):
        # The project accepts OPENAI_API_KEY as its configured provider key;
        # expose it under the legacy runner's expected name only in this child.
        os.environ["DEEPSEEK_API_KEY"] = os.environ["OPENAI_API_KEY"]
    if not os.environ.get("DEEPSEEK_API_KEY"):
        return {
            "slug": slug, "company": company, "url": url,
            "target_path": "legacy", "status": "not_run", "bucket": "legacy",
            "reason": "missing_model_credential",
        }
    legacy = _load_legacy_evaluator()
    settings = legacy._settings()
    db = legacy._setup_db()
    model = legacy._build_job_discovery_llm(settings=settings)
    record = legacy._run_one(slug, company, url, None, settings, db, model)
    record["target_path"] = "legacy"
    return record


def _run_site(slug: str) -> dict[str, Any]:
    for candidate_slug, company, url in URLS:
        if candidate_slug == slug:
            try:
                row = (_run_pev_row(slug, company, url) if slug in PEV_SITES
                       else _run_legacy_row(slug, company, url))
            except Exception as exc:  # noqa: BLE001 - persist and continue parent eval
                row = {
                    "slug": slug, "company": company, "url": url,
                    "target_path": "pev" if slug in PEV_SITES else "legacy",
                    "status": "crashed", "bucket": "failed", "error": str(exc)[:300],
                }
            _write_row(row)
            return row
    raise ValueError(f"unknown site slug: {slug}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated Worker/PEV ten-URL evaluator")
    parser.add_argument("--site", choices=[slug for slug, _, _ in URLS])
    parser.add_argument("--timeout-sec", type=int, default=360)
    args = parser.parse_args()

    if args.site:
        _run_site(args.site)
        return 0

    from src.utils import load_env

    load_env()
    rows: list[dict[str, Any]] = []
    for slug, company, url in URLS:
        row_file = _row_path(slug)
        try:
            subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--site", slug],
                cwd=_PROJECT_ROOT,
                env=os.environ.copy(),
                timeout=args.timeout_sec,
                check=False,
            )
            if row_file.exists():
                rows.append(json.loads(row_file.read_text(encoding="utf-8")))
            else:
                rows.append({
                    "slug": slug, "company": company, "url": url,
                    "target_path": "pev" if slug in PEV_SITES else "legacy",
                    "status": "crashed", "bucket": "failed",
                    "error": "child exited without a result record",
                })
        except subprocess.TimeoutExpired:
            row = _timeout_row(slug, company, url, args.timeout_sec)
            _write_row(row)
            rows.append(row)
        _write_summary(rows)
        print(f"[{slug}] {rows[-1]['status']} -> {_OUT}", flush=True)
    print(f"Wrote isolated Worker/PEV ten-URL evaluation: {_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
