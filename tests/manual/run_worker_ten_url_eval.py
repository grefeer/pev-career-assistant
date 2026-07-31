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

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import DiscoveredJobCandidate, JobDiscoveryTask
from backend.app.services.job_discovery.role_preferences import (
    DEFAULT_ROLE_PREFERENCES,
    filter_candidates_for_preferences,
)
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate
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
_EVAL_MODE = os.environ.get("JOB_DISCOVERY_EVAL_MODE", "adapter")


def _bucket_for(summary: dict[str, object]) -> str:
    if bool(summary.get("coverage_verified")):
        return "pev_pass"
    if summary.get("execution_path") == "path_a_adapter":
        return "pev_fail"
    return "legacy"


def _timeout_row(
    slug: str, company: str, url: str, timeout_sec: int, *, mode: str,
) -> dict[str, Any]:
    """Return an explicit, non-success result for an isolated site timeout."""
    return {
        "slug": slug,
        "company": company,
        "url": url,
        "target_path": "skill_no_adapter" if mode == "skill" else (
            "pev" if slug in PEV_SITES else "legacy"
        ),
        "status": "timed_out",
        "bucket": "timed_out",
        "timeout_sec": timeout_sec,
    }


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    """Terminate a timed-out evaluator and every descendant on Windows.

    ``venv\\Scripts\\python.exe`` starts the base interpreter as a child on
    Windows.  Killing only the launcher leaves a live browser/LLM evaluator
    behind, which can corrupt later URL measurements.  ``taskkill /T`` is the
    platform process-tree primitive and is intentionally limited to the PID
    created for this isolated manual-evaluation child.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, check=False,
        )
    else:
        process.kill()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def _row_path(slug: str) -> Path:
    suffix = "" if _EVAL_MODE == "adapter" else f"_{_EVAL_MODE}"
    return _ROW_OUT_DIR / f"_worker_ten_url_eval_{slug}{suffix}.json"


def _clear_row_file(slug: str) -> None:
    """Prevent a polling caller from reading this site's prior evaluation."""
    _row_path(slug).unlink(missing_ok=True)


def _write_row(row: dict[str, Any]) -> None:
    _row_path(str(row["slug"])).write_text(
        json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_summary(rows: list[dict[str, Any]]) -> None:
    buckets: dict[str, int] = {}
    for row in rows:
        bucket = str(row["bucket"])
        buckets[bucket] = buckets.get(bucket, 0) + 1
    summary_path = _OUT.with_name(
        _OUT.stem + ("" if _EVAL_MODE == "adapter" else f"_{_EVAL_MODE}") + _OUT.suffix
    )
    summary_path.write_text(
        json.dumps({"rows": rows, "buckets": buckets}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _preference_metrics(db: Session, task_id: str) -> dict[str, Any]:
    candidates = [
        NormalizedJobCandidate(
            title=candidate.title,
            description_text=candidate.description_text or "",
            responsibilities=candidate.responsibilities or "",
            requirements=candidate.requirements or "",
        )
        for candidate in db.scalars(
            select(DiscoveredJobCandidate).where(
                DiscoveredJobCandidate.task_id == task_id,
            )
        )
    ]
    matched = filter_candidates_for_preferences(candidates, DEFAULT_ROLE_PREFERENCES)
    return {
        "role_preferences": list(DEFAULT_ROLE_PREFERENCES),
        "preferred_candidate_count": len(matched),
        "preferred_candidate_titles": [candidate.title for candidate in matched if candidate.title],
    }


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
        preference_metrics = _preference_metrics(db, task_id)
    passed, metrics = _passes_pev_gate(summary)
    execution_path = summary.get("execution_path")
    adapter_routed = execution_path == "path_a_adapter"
    passed = passed and adapter_routed
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
        "execution_path": execution_path,
        "adapter_routed": adapter_routed,
        **preference_metrics,
    }


def _skill_site_config(company: str, url: str) -> dict[str, Any]:
    """Seed only generic task context; its strategy remains disabled."""
    site_cfg = dict(PEV_SITE_CONFIG["moka"])
    site_cfg["url"] = url
    site_cfg["raw_fields"] = [{"field_name": "公司名称", "value": company}]
    return site_cfg


def _run_skill_row(slug: str, company: str, url: str) -> dict[str, Any]:
    """Run the deployed Worker Skill path with routing/adapters disabled."""
    # The fixture needs a source/task, but the seeded strategy is unreachable:
    # Skill Runtime executes before strategy routing.
    site_cfg = _skill_site_config(company, url)
    factory, task_id = _setup_db(site_cfg)
    settings = _settings()
    settings.job_discovery_skill_runtime_enabled = True
    settings.job_discovery_strategy_enabled = False
    settings.job_discovery_max_pages_per_task = 50
    settings.job_discovery_max_candidates_per_task = 500
    started = time.monotonic()
    JobDiscoveryWorker(factory, settings).run_once()
    elapsed = time.monotonic() - started
    with factory() as db:
        task = db.get(JobDiscoveryTask, task_id)
        summary = dict(task.result_summary_json or {}) if task else {}
        preference_metrics = _preference_metrics(db, task_id)
    execution_path = summary.get("execution_path")
    coverage_verified = bool(summary.get("coverage_verified"))
    status = task.status.value if task is not None else "missing_task"
    return {
        "slug": slug,
        "company": company,
        "url": url,
        "target_path": "skill_no_adapter",
        "status": status,
        # Targeted recommendation success no longer means that every opening
        # was enumerated. It requires at least one preference-matched JD with
        # an apply link, which the Worker only persists as ``succeeded``.
        "bucket": "targeted_pass" if (
            execution_path == "skill_agent"
            and status == "succeeded"
            and preference_metrics["preferred_candidate_count"] > 0
        ) else "targeted_nonpass",
        "elapsed_sec": round(elapsed, 1),
        "candidate_count": int(summary.get("candidate_count") or 0),
        "coverage_verified": coverage_verified,
        "execution_path": execution_path,
        "adapter_routed": False,
        **preference_metrics,
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


def _run_site(slug: str, *, mode: str) -> dict[str, Any]:
    for candidate_slug, company, url in URLS:
        if candidate_slug == slug:
            try:
                if mode == "skill":
                    row = _run_skill_row(slug, company, url)
                else:
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
    parser.add_argument("--mode", choices=["adapter", "skill"], default="adapter")
    parser.add_argument("--timeout-sec", type=int, default=360)
    args = parser.parse_args()
    global _EVAL_MODE
    _EVAL_MODE = args.mode
    os.environ["JOB_DISCOVERY_EVAL_MODE"] = args.mode

    if args.site:
        _clear_row_file(args.site)
        _run_site(args.site, mode=args.mode)
        return 0

    from src.utils import load_env

    load_env()
    rows: list[dict[str, Any]] = []
    for slug, company, url in URLS:
        row_file = _row_path(slug)
        try:
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--site", slug, "--mode", args.mode],
                cwd=_PROJECT_ROOT,
                env=os.environ.copy(),
            )
            child.wait(timeout=args.timeout_sec)
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
            _terminate_process_tree(child)
            row = _timeout_row(slug, company, url, args.timeout_sec, mode=args.mode)
            _write_row(row)
            rows.append(row)
        _write_summary(rows)
        print(f"[{slug}] {rows[-1]['status']} -> {_OUT}", flush=True)
    print(f"Wrote isolated Worker/PEV ten-URL evaluation: {_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
