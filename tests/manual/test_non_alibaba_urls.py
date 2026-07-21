"""Smoke test: 4 non-Alibaba career sites + 2 WeChat URLs via Strategy Router.

Target URLs (diverse domains, no Alibaba):
  WeChat:
    1. 柏楚电子 - https://mp.weixin.qq.com/s/F_ehY3q8Zi3-QV-AwoOF5g
    2. 华金证券 - https://mp.weixin.qq.com/s/rjuqB1qQnl9sy5qX9-Xs3w
  Career:
    3. 元戎启行 (app.mokahr.com)
    4. 禾赛科技 (jobs.feishu.cn)
    5. 小米 (xiaomi.jobs.f.mioffice.cn)
    6. 拼多多 (careers.pddglobalhr.com)

Usage:
  cd D:\Python\langgraph-multi-agent-career-assistant-main
  .\.venv\Scripts\python.exe tests\manual\test_non_alibaba_urls.py
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryStrategy
from backend.app.services.job_discovery.deepagents_runner import (
    build_discovery_supervisor_agent,
    _build_job_discovery_llm,
)
from backend.app.services.job_discovery.result_contract import (
    AgentResultParseError,
    enforce_result_invariants,
    parse_agent_result,
)
from backend.app.services.job_discovery.schemas import DiscoveryRunResult, DiscoveryTaskInput
from backend.app.services.job_discovery.strategy.strategy_router import StrategyRouter
from backend.app.services.job_discovery.strategy.snapshot_executor import (
    SnapshotExecutor,
    SnapshotExecutionResult,
)
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer

# -- Test URLs ---------------------------------------------------------------

TEST_URLS: list[dict[str, str]] = [
    # WeChat (2)
    {
        "company": "柏楚电子",
        "url": "https://mp.weixin.qq.com/s/F_ehY3q8Zi3-QV-AwoOF5g",
        "url_type": "wechat",
        "source_key": "tencent-27-referrals",
    },
    {
        "company": "华金证券",
        "url": "https://mp.weixin.qq.com/s/rjuqB1qQnl9sy5qX9-Xs3w",
        "url_type": "wechat",
        "source_key": "tencent-27-referrals",
    },
    # Career sites - diverse domains, non-Alibaba (4)
    {
        "company": "元戎启行",
        "url": "https://app.mokahr.com/recommendation-apply/deeproute/6488?sharePageId=4200484&recommendCode=NTAIUtn&codeType=1&code=061yTN0w36fOd53Sqd0w3ct2UU0yTN0e&state=3#/jobs/?isCampusJob=1",
        "url_type": "career_site",
        "source_key": "tencent-intern-referrals",
    },
    {
        "company": "禾赛科技",
        "url": "https://kwh0jtf778.jobs.feishu.cn/229043/m/?external_referral_code=GA2DJVE",
        "url_type": "career_site",
        "source_key": "tencent-intern-referrals",
    },
    {
        "company": "小米",
        "url": "https://xiaomi.jobs.f.mioffice.cn/s/m5DjWDrhl_g",
        "url_type": "career_site",
        "source_key": "tencent-intern-referrals",
    },
    {
        "company": "拼多多",
        "url": "https://careers.pddglobalhr.com/campus/grad?t=N5ch0DXEtA",
        "url_type": "career_site",
        "source_key": "tencent-intern-referrals",
    },
]

# -- Config ------------------------------------------------------------------

MAIN_PROJECT_DOTENV = Path("D:/Python/langgraph-multi-agent-career-assistant-main/.env")
MAX_PAGES = 5
RECURSION_LIMIT = 25
# Hard wall-clock cap per URL (seconds). Enforced by running each URL in its own
# subprocess; a hung/slow Supervisor or Web Navigation Agent loop on one site
# cannot block the whole run. Generous vs. the 300s task budget + 120s LLM
# call cap so legitimate extractions are not cut short.
PER_URL_TIMEOUT = 600

# WeChat snapshot plan (3 deterministic steps: triage -> fetch -> extract)
WECHAT_PLAN_YAML = """
plan:
  - tool: triage_link
    params:
      url: "{{task.source_url}}"
  - tool: fetch_wechat_article
    params:
      url: "{{task.source_url}}"
  - tool: extract_jd_candidates
    params:
      page_text: "{{prev.result.text}}"
      url: "{{task.source_url}}"
"""


def _bootstrap_env() -> None:
    if not MAIN_PROJECT_DOTENV.exists():
        return
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(MAIN_PROJECT_DOTENV, interpolate=False)
        for key in ("READGZH_API_KEY",):
            if key not in os.environ and key in vals and vals[key]:
                os.environ[key] = vals[key]
    except ImportError:
        pass


_bootstrap_env()


# -- Helpers -----------------------------------------------------------------


def _snapshot_has_manual_review_flag(trajectory: TrajectoryBuffer) -> bool:
    for step in trajectory.steps:
        result = step.get("result")
        if isinstance(result, dict) and result.get("needs_manual_review") is True:
            return True
    return False


def _parse_agent_result(raw: dict[str, Any]) -> Any:
    """Use the production result contract (no duplicated parser)."""
    try:
        result = parse_agent_result(raw)
    except AgentResultParseError:
        return DiscoveryRunResult(status="failed", summary="Could not parse agent result")
    return enforce_result_invariants(result)


# -- Strategy seeding --------------------------------------------------------


def seed_wechat_strategy(db: Session) -> JobDiscoveryStrategy:
    from sqlalchemy import select
    existing = db.scalars(
        select(JobDiscoveryStrategy).where(
            JobDiscoveryStrategy.url_pattern == "mp.weixin.qq.com/s/*"
        )
    ).first()
    if existing:
        return existing
    strat = JobDiscoveryStrategy(
        url_pattern="mp.weixin.qq.com/s/*",
        site_type="wechat",
        description="WeChat article JD extraction (seed)",
        plan_yaml=WECHAT_PLAN_YAML,
        priority=10,
        degradation_threshold=3,
        recovery_threshold=2,
    )
    db.add(strat)
    db.commit()
    return strat


# -- Single URL execution ----------------------------------------------------


def execute_one_url(
    *,
    entry: dict[str, str],
    settings: Settings,
    db: Session,
    llm_model: Any,
) -> dict[str, Any]:
    url = entry["url"]
    source_key = entry["source_key"]
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]

    task_input = DiscoveryTaskInput(
        source_id=source_key,
        raw_record_id=f"manual-{url_hash}",
        external_record_id=f"manual-{url_hash}",
        source_key=source_key,
        source_url=url,
        url_hash=url_hash,
        record_fields=[],
    )

    t0 = time.monotonic()
    router = StrategyRouter(db)
    strategy_record = router.match(url)
    route_elapsed = time.monotonic() - t0
    print(f"  Router: {route_elapsed:.2f}s -> {'matched: ' + strategy_record.url_pattern if strategy_record else 'NO MATCH'}")

    trajectory = TrajectoryBuffer(
        task_id=str(url_hash),
        strategy_id=strategy_record.id if strategy_record else None,
        executor_type="snapshot" if strategy_record else "supervisor",
    )

    result = None
    snapshot_context = None
    executor_type = "unknown"

    # --- PATH A: Adapter ---
    if strategy_record and strategy_record.adapter:
        executor_type = "adapter"
        print(f"  -> Adapter: {strategy_record.adapter}")
        try:
            import importlib
            module_path, class_name = strategy_record.adapter.rsplit(".", 1)
            module = importlib.import_module(module_path)
            adapter_cls = getattr(module, class_name)
            adapter_instance = adapter_cls()
            t_a = time.monotonic()
            result = adapter_instance.execute(task_input, strategy_record, trajectory)
            print(f"  Adapter done: {time.monotonic() - t_a:.0f}s, status={result.status}")
        except Exception as exc:
            print(f"  Adapter failed: {exc}")
            trajectory.record_step("adapter", "failed", {"adapter": strategy_record.adapter}, None, error=exc)
            executor_type = "supervisor"
            snapshot_context = trajectory.to_snapshot_context()

    # --- PATH B: SnapshotExecutor ---
    elif strategy_record and strategy_record.plan_yaml:
        executor_type = "snapshot"
        print(f"  -> Snapshot: {strategy_record.url_pattern}")

        executor = SnapshotExecutor(
            strategy=strategy_record,
            task=task_input,
            trajectory=trajectory,
        )
        t_s = time.monotonic()
        snap_result = executor.execute()
        snap_elapsed = time.monotonic() - t_s
        print(f"  Snapshot done: {snap_elapsed:.0f}s, status={snap_result.status}, candidates={len(snap_result.candidates)}")

        if isinstance(snap_result, SnapshotExecutionResult) and snap_result.needs_supervisor_fallback:
            print(f"  -> Supervisor takeover (step failed)")
            executor_type = "partial_fallback"
            snapshot_context = snap_result.snapshot_context
        elif not snap_result.candidates:
            if _snapshot_has_manual_review_flag(trajectory):
                print(f"  -> Blocked (needs_manual_review), skipping Supervisor")
                result = DiscoveryRunResult(
                    status="needs_manual_review",
                    evidence=snap_result.evidence,
                    candidates=[],
                    summary="WeChat article blocked by verification wall",
                )
            else:
                print(f"  -> 0 candidates, handing off to Supervisor")
                executor_type = "partial_fallback"
                snapshot_context = None
        else:
            result = snap_result

    # --- PATH C: Pure Supervisor (no match or fallback) ---
    if result is None or executor_type in ("supervisor", "partial_fallback"):
        if executor_type in ("unknown", "supervisor"):
            executor_type = "supervisor"
            print(f"  -> Pure Supervisor (no strategy match)")
        else:
            print(f"  -> Supervisor fallback")

        agent = build_discovery_supervisor_agent(
            settings=settings,
            model=llm_model,
            snapshot_context=snapshot_context,
        )

        msg_content = json.dumps(asdict(task_input), ensure_ascii=False)
        from langchain_core.messages import HumanMessage
        agent_input = {"messages": [HumanMessage(content=msg_content)]}

        t_sup = time.monotonic()
        try:
            config = {"recursion_limit": RECURSION_LIMIT}
            raw = agent.invoke(agent_input, config=config)
        except TypeError:
            raw = agent.invoke(agent_input)
        print(f"  Supervisor done: {time.monotonic() - t_sup:.0f}s")

        result = _parse_agent_result(raw)

    elapsed = time.monotonic() - t0
    return {
        "company": entry["company"],
        "url": url,
        "url_type": entry["url_type"],
        "executor_type": executor_type,
        "elapsed_sec": round(elapsed, 1),
        "status": result.status if result else "unknown",
        "evidence_count": len(result.evidence) if result and result.evidence else 0,
        "candidate_count": len(result.candidates) if result and result.candidates else 0,
        "summary": (result.summary or "")[:300] if result else "",
        "error": None,
    }


# -- Main --------------------------------------------------------------------


def _setup_runtime():
    """Build settings, in-memory DB (with seeded WeChat strategy), and LLM."""
    settings = Settings(
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        job_discovery_enabled=True,
        job_discovery_task_timeout_seconds=300,
        job_discovery_max_pages_per_task=MAX_PAGES,
        job_discovery_ocr_enabled=True,
        job_discovery_strategy_enabled=True,
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    seed_wechat_strategy(db)
    llm_model = _build_job_discovery_llm(settings=settings)
    return settings, db, llm_model


def _empty_result(entry: dict[str, str], index: int, status: str, err: str) -> dict[str, Any]:
    return {
        "index": index + 1,
        "company": entry["company"],
        "url": entry["url"],
        "url_type": entry["url_type"],
        "executor_type": status,
        "elapsed_sec": PER_URL_TIMEOUT,
        "status": status,
        "evidence_count": 0,
        "candidate_count": 0,
        "summary": "",
        "error": err[:200],
    }


def _run_single(index: int) -> dict[str, Any]:
    """Run ONE url (by index) in this process. Used by --single subprocess mode."""
    entry = TEST_URLS[index]
    settings, db, llm_model = _setup_runtime()
    print(f"  -> running [{index + 1}/{len(TEST_URLS)}] {entry['company']}")
    sys.stdout.flush()
    try:
        r = execute_one_url(entry=entry, settings=settings, db=db, llm_model=llm_model)
    except Exception as exc:
        print(f"  !! CRASHED: {exc}")
        traceback.print_exc()
        r = {
            "company": entry["company"],
            "url": entry["url"],
            "url_type": entry["url_type"],
            "executor_type": "error",
            "elapsed_sec": 0,
            "status": "failed",
            "evidence_count": 0,
            "candidate_count": 0,
            "summary": "",
            "error": str(exc)[:200],
        }
    r["index"] = index + 1
    return r


def main() -> None:
    # --single mode: run one url, emit JSON to the path in --out ----------
    if "--single" in sys.argv:
        idx = int(sys.argv[sys.argv.index("--single") + 1])
        out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
        result = _run_single(idx)
        if out:
            Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            print(json.dumps(result, ensure_ascii=False))
        return

    print(f"READGZH_API_KEY: {'set' if os.environ.get('READGZH_API_KEY') else 'MISSING'}")
    print(f"DEEPSEEK_API_KEY: {'set' if os.environ.get('DEEPSEEK_API_KEY') else 'MISSING'}")
    print(f"PER_URL_TIMEOUT: {PER_URL_TIMEOUT}s (hard subprocess cap)")
    print()

    results: list[dict[str, Any]] = []
    total = len(TEST_URLS)

    for i, entry in enumerate(TEST_URLS):
        print(f"{'='*70}")
        print(f"  [{i+1}/{total}] [{entry['url_type'].upper()}] {entry['company']}")
        print(f"  URL: {entry['url'][:120]}")
        print(f"{'='*70}")
        sys.stdout.flush()

        t0 = time.monotonic()
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="_nonali_")
        os.close(tmp_fd)
        try:
            proc = subprocess.run(
                [sys.executable, __file__, "--single", str(i), "--out", tmp_path],
                timeout=PER_URL_TIMEOUT,
            )
            elapsed = time.monotonic() - t0
            try:
                r = json.loads(Path(tmp_path).read_text(encoding="utf-8"))
            except Exception:
                r = _empty_result(entry, i, "failed", f"subprocess exit={proc.returncode}, no JSON")
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            r = _empty_result(entry, i, "timeout", f"exceeded {PER_URL_TIMEOUT}s hard cap")
            print(f"  !! TIMEOUT after {elapsed:.0f}s")
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

        r["elapsed_sec"] = round(r.get("elapsed_sec") or elapsed, 1)
        results.append(r)
        print(f"  done: {r['elapsed_sec']:.0f}s, status={r['status']}, candidates={r['candidate_count']}")
        print()

    # -- Final summary --
    print(f"\n{'='*70}")
    print(f"  FINAL SUMMARY - {len(results)} URLs (non-Alibaba)")
    print(f"{'='*70}")
    print(f"\n  {'#':<3} {'Company':<12} {'Type':<12} {'Executor':<18} {'Status':<20} {'Ev':>4} {'Cand':>5} {'Time':>7}")
    print(f"  {'-'*3} {'-'*12} {'-'*12} {'-'*18} {'-'*20} {'-'*4} {'-'*5} {'-'*7}")
    for r in results:
        print(
            f"  {r['index']:<3} {r.get('company','?')[:11]:<12} {r.get('url_type','?'):<12} "
            f"{r.get('executor_type','?'):<18} {r['status']:<20} "
            f"{r['evidence_count']:>4} {r['candidate_count']:>5} {r['elapsed_sec']:>6.0f}s"
        )

    print(f"\n  {'─'*70}")
    for r in results:
        print(f"\n  [{r['index']}] {r['company']}")
        print(f"      URL:      {r['url'][:100]}")
        print(f"      Executor: {r['executor_type']}")
        print(f"      Status:   {r['status']}")
        print(f"      Evidence: {r['evidence_count']} | Candidates: {r['candidate_count']}")
        if r.get('summary'):
            print(f"      Summary:  {r['summary'][:200]}")
        if r.get('error'):
            print(f"      Error:    {r['error'][:200]}")

    wechat_ok = sum(1 for r in results if r.get("url_type") == "wechat" and r["candidate_count"] > 0)
    career_ok = sum(1 for r in results if r.get("url_type") == "career_site" and r["candidate_count"] > 0)
    total_candidates = sum(r["candidate_count"] for r in results)
    total_evidence = sum(r["evidence_count"] for r in results)
    wechat_total = sum(1 for r in results if r.get("url_type") == "wechat")
    career_total = sum(1 for r in results if r.get("url_type") == "career_site")

    print(f"\n  WeChat success:  {wechat_ok}/{wechat_total} (candidates > 0)")
    print(f"  Career success:  {career_ok}/{career_total} (candidates > 0)")
    print(f"  Total candidates: {total_candidates}")
    print(f"  Total evidence:   {total_evidence}")

    # Write JSON
    out_path = Path(__file__).parent / "_non_alibaba_smoke_output.json"
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n  Full results -> {out_path}")


if __name__ == "__main__":
    main()
