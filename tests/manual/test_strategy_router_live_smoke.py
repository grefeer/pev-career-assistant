"""Live smoke test: Strategy Router -> SnapshotExecutor/Adapter -> Supervisor fallback.

Exercises the FULL strategy router pipeline with 4 real URLs from Tencent:
  1. WeChat URL 1 -> SnapshotExecutor (6-step YAML plan)
  2. WeChat URL 2 -> SnapshotExecutor
  3. Career site URL 1 -> Supervisor (no strategy match) or Alibaba Adapter
  4. Career site URL 2 -> Supervisor (no strategy match) or Alibaba Adapter

Usage:
  cd D:\Python\langgraph-multi-agent-career-assistant-main-strategy-router
  D:\Python\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe tests\manual\test_strategy_router_live_smoke.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryStrategy, JobDiscoveryTrajectory
from backend.app.services.job_discovery.deepagents_runner import (
    build_discovery_supervisor_agent,
    _build_job_discovery_llm,
    run_web_navigation,
)
from backend.app.services.job_discovery.schemas import DiscoveryRunResult, DiscoveryTaskInput
from backend.app.services.job_discovery.strategy.strategy_router import StrategyRouter
from backend.app.services.job_discovery.strategy.strategy_store import get_active_strategies
from backend.app.services.job_discovery.strategy.snapshot_executor import (
    SnapshotExecutor,
    SnapshotExecutionResult,
    _call_tool_by_name,
)
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer
from backend.app.services.job_discovery.strategy.error_classifier import classify_error
from backend.app.services.job_mappers import BUILTIN_SOURCES, extract_discovery_urls
from backend.app.services.tencent_smartsheet import TencentRecord, TencentSmartsheetGateway
from backend.app.config import _literal_tencent_dotenv_values

# -- Constants ----------------------------------------------------------------

SOURCE_KEYS = ("tencent-27-referrals", "tencent-intern-referrals")
MAIN_PROJECT_DOTENV = Path("D:/Python/langgraph-multi-agent-career-assistant-main/.env")
MAX_PAGES = 5
RECURSION_LIMIT = 100  # enough for WebNavigationAgent (deep agent) + Supervisor pipeline

# Seed strategy YAML plans
# WeChat plan: triage → fetch via ReadGZH → deterministic JD extraction.
# All three steps are deterministic (no LLM planning), completing in
# ~5-15 s instead of routing through the Supervisor + WebNavigationAgent
# deep-agent pipeline which takes 2-7 minutes.
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


# -- Env setup ----------------------------------------------------------------


def _bootstrap_env() -> None:
    """Load required keys from .env into os.environ."""
    if not MAIN_PROJECT_DOTENV.exists():
        return
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(MAIN_PROJECT_DOTENV, interpolate=False)
        for key in ("READGZH_API_KEY",):
            if key not in os.environ and key in vals and vals[key]:
                os.environ[key] = vals[key]
                print(f"  [env] Loaded {key} from .env")
    except ImportError:
        pass


_bootstrap_env()


# -- Helpers ------------------------------------------------------------------


def _live_tencent_token() -> str | None:
    values = _literal_tencent_dotenv_values(MAIN_PROJECT_DOTENV)
    return (
        os.environ.get("TEST_TENCENT_DOCS_TOKEN")
        or os.environ.get("TENCENT_DOCS_TOKEN")
        or values.get("test_tencent_docs_token")
        or values.get("tencent_docs_token")
    )


def _source_definition(source_key: str):
    for source in BUILTIN_SOURCES:
        if source.source_key == source_key:
            return source
    raise AssertionError(f"unknown source: {source_key}")


def _select_two_records_with_urls(
    gateway: TencentSmartsheetGateway,
    source_key: str,
) -> list[TencentRecord]:
    source = _source_definition(source_key)
    selected: list[TencentRecord] = []
    offset = 0
    while len(selected) < 2:
        page = gateway.list_records(
            source.file_id, source.sheet_id, offset=offset, limit=10
        )
        for record in page.records:
            if extract_discovery_urls(record, source_key):
                selected.append(record)
            if len(selected) == 2:
                break
        if len(selected) == 2 or not page.has_more:
            break
        offset = page.next_offset
    assert len(selected) == 2, f"{source_key} did not expose two URL records"
    return selected


def _field_text(record: TencentRecord, name: str) -> str:
    for field in record.field_values:
        if field.get("field") != name:
            continue
        parts: list[str] = []
        for key in ("text_value", "option_value", "url_value"):
            block = field.get(key) or {}
            for item in block.get("items", []) or []:
                text = item.get("text") or item.get("link")
                if text:
                    parts.append(text)
        return "、".join(parts)
    return ""


def _field_values_for_task(record: TencentRecord) -> list[dict[str, Any]]:
    return record.field_values


# -- Strategy seeding ---------------------------------------------------------


def seed_strategies(db: Session) -> tuple[JobDiscoveryStrategy, JobDiscoveryStrategy]:
    """Seed WeChat and Alibaba strategies into the DB. Returns (wechat, alibaba)."""
    # Check existing
    from sqlalchemy import select
    existing = db.scalars(
        select(JobDiscoveryStrategy).where(
            JobDiscoveryStrategy.url_pattern == "mp.weixin.qq.com/s/*"
        )
    ).first()
    if existing:
        wechat = existing
    else:
        wechat = JobDiscoveryStrategy(
            url_pattern="mp.weixin.qq.com/s/*",
            site_type="wechat",
            description="WeChat article JD extraction (seed)",
            plan_yaml=WECHAT_PLAN_YAML,
            priority=10,
            degradation_threshold=3,
            recovery_threshold=2,
        )
        db.add(wechat)

    existing = db.scalars(
        select(JobDiscoveryStrategy).where(
            JobDiscoveryStrategy.url_pattern == "*talent.alibaba.com/*"
        )
    ).first()
    if existing:
        alibaba = existing
    else:
        alibaba = JobDiscoveryStrategy(
            url_pattern="*talent.alibaba.com/*",
            site_type="spa",
            description="Alibaba SPA career site (seed)",
            plan_yaml="plan: []",
            adapter="backend.app.services.job_discovery.adapters.alibaba_spa.AlibabaSPAAdapter",
            priority=10,
            degradation_threshold=3,
            recovery_threshold=2,
        )
        db.add(alibaba)

    db.commit()
    return wechat, alibaba


# -- Strategy Router execution ------------------------------------------------


def execute_with_strategy_router(
    *,
    url: str,
    source_key: str,
    record: TencentRecord,
    settings: Settings,
    db: Session,
    llm_model: Any,
) -> dict[str, Any]:
    """Full strategy router pipeline for one URL."""
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    task_input = DiscoveryTaskInput(
        source_id=source_key,
        raw_record_id=record.record_id,
        external_record_id=record.record_id,
        source_key=source_key,
        source_url=url,
        url_hash=url_hash,
        record_fields=_field_values_for_task(record),
    )

    t0 = time.monotonic()
    router = StrategyRouter(db)
    strategy_record = router.match(url)
    route_elapsed = time.monotonic() - t0
    print(f"  Router match: {route_elapsed:.2f}s")

    trajectory = TrajectoryBuffer(
        task_id=str(url_hash),
        strategy_id=strategy_record.id if strategy_record else None,
        executor_type="snapshot" if strategy_record else "supervisor",
    )
    # Store metadata for later use
    trajectory._url = url
    trajectory._url_pattern = strategy_record.url_pattern if strategy_record else None

    result = None
    snapshot_context = None

    # --- PATH A: Adapter (strategy has adapter field) ---
    if strategy_record and strategy_record.adapter:
        executor_type = "adapter"
        print(f"  -> Adapter path: {strategy_record.adapter}")
        try:
            adapter_cls = _load_adapter_class(strategy_record.adapter)
            adapter_instance = adapter_cls()
            t_adapter = time.monotonic()
            result = adapter_instance.execute(task_input, strategy_record, trajectory)
            print(f"  Adapter done: {time.monotonic() - t_adapter:.0f}s, status={result.status}")
        except Exception as exc:
            print(f"  Adapter failed: {exc}")
            trajectory.record_step(
                tool="adapter",
                status="failed",
                params={"adapter": strategy_record.adapter},
                result=None,
                error=exc,
            )
            executor_type = "supervisor"
            strategy_record = None  # Don't count adapter failure against strategy
            snapshot_context = trajectory.to_snapshot_context()

    # --- PATH B: SnapshotExecutor (strategy has plan_yaml) ---
    elif strategy_record and strategy_record.plan_yaml:
        executor_type = "snapshot"
        print(f"  -> Snapshot path: {strategy_record.url_pattern}")

        # Build tool_dependencies for run_web_navigation
        def _wrapped_run_web_navigation(start_url, **kw):
            return run_web_navigation(start_url, settings=settings, **kw)
        _wrapped_run_web_navigation.__name__ = "run_web_navigation"

        tool_deps = {"run_web_navigation": _wrapped_run_web_navigation}

        executor = SnapshotExecutor(
            strategy=strategy_record,
            task=task_input,
            trajectory=trajectory,
            tool_dependencies=tool_deps,
        )
        t_snapshot = time.monotonic()
        snap_result = executor.execute()
        print(f"  Snapshot done: {time.monotonic() - t_snapshot:.0f}s, status={snap_result.status}")

        if isinstance(snap_result, SnapshotExecutionResult) and snap_result.needs_supervisor_fallback:
            print(f"  -> Supervisor takeover (step {trajectory.failed_step_index} failed)")
            executor_type = "partial_fallback"
            snapshot_context = snap_result.snapshot_context
        elif not snap_result.candidates:
            # Snapshot succeeded but 0 candidates.
            # If the article was blocked (verification wall), skip the
            # expensive Supervisor handoff — there is nothing to extract.
            if _snapshot_has_manual_review_flag(trajectory):
                print(f"  -> Blocked article (needs_manual_review), skipping Supervisor")
                result = DiscoveryRunResult(
                    status="needs_manual_review",
                    evidence=snap_result.evidence,
                    candidates=[],
                    summary="WeChat article blocked by verification wall",
                )
            else:
                print(f"  -> Supervisor handoff (Snapshot OK but 0 candidates, running full pipeline)")
                executor_type = "partial_fallback"
                snapshot_context = None  # clean start, don't confuse supervisor
        else:
            result = snap_result

    # --- PATH C: Pure Supervisor (no match or fallback) ---
    if result is None or executor_type in ("supervisor", "partial_fallback"):
        if executor_type == "supervisor":
            print(f"  -> Pure Supervisor path (no strategy match)")
        else:
            print(f"  -> Supervisor fallback path")

        if trajectory is None:
            trajectory = TrajectoryBuffer(
                task_id=str(url_hash),
                strategy_id=None,
                executor_type="supervisor",
            )

        agent = build_discovery_supervisor_agent(
            settings=settings,
            model=llm_model,
            snapshot_context=snapshot_context,
        )

        msg_content = json.dumps(asdict(task_input), ensure_ascii=False)
        from langchain_core.messages import HumanMessage
        agent_input = {"messages": [HumanMessage(content=msg_content)]}

        t_supervisor = time.monotonic()
        try:
            config = {"recursion_limit": RECURSION_LIMIT}
            raw = agent.invoke(agent_input, config=config)
        except TypeError:
            raw = agent.invoke(agent_input)
        print(f"  Supervisor done: {time.monotonic() - t_supervisor:.0f}s")

        result = _parse_agent_result(raw)

    elapsed = time.monotonic() - t0
    return {
        "url": url,
        "url_type": "wechat" if "mp.weixin.qq.com" in url else "career_site",
        "executor_type": executor_type,
        "elapsed_sec": round(elapsed, 1),
        "status": result.status if result else "unknown",
        "evidence_count": len(result.evidence) if result and result.evidence else 0,
        "candidate_count": len(result.candidates) if result and result.candidates else 0,
        "summary": (result.summary or "")[:300] if result else "",
        "error": None,
        "strategy_matched": strategy_record is not None,
        "strategy_pattern": strategy_record.url_pattern if strategy_record else None,
    }


def _snapshot_has_manual_review_flag(trajectory: TrajectoryBuffer) -> bool:
    """Check whether any snapshot step returned needs_manual_review=True."""
    for step in trajectory.steps:
        result = step.get("result")
        if isinstance(result, dict) and result.get("needs_manual_review") is True:
            return True
    return False


def _load_adapter_class(adapter_path: str):
    """Dynamically load an adapter class from a dotted path."""
    module_path, class_name = adapter_path.rsplit(".", 1)
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _parse_agent_result(raw: dict[str, Any]) -> Any:
    """Parse deepagents output into DiscoveryRunResult-like object."""
    from backend.app.services.job_discovery.schemas import DiscoveryRunResult

    # Strategy 1: structured_response
    structured = raw.get("structured_response")
    if hasattr(structured, "model_dump"):
        structured = structured.model_dump()
    if isinstance(structured, dict) and "status" in structured:
        return DiscoveryRunResult(**{k: v for k, v in structured.items() if k in DiscoveryRunResult.__dataclass_fields__})

    # Strategy 2: direct dict
    if isinstance(raw, dict) and "status" in raw:
        return DiscoveryRunResult(**{k: v for k, v in raw.items() if k in DiscoveryRunResult.__dataclass_fields__})

    # Strategy 3: last message JSON
    messages = raw.get("messages", [])
    if messages:
        last = messages[-1]
        content = last.content if hasattr(last, "content") else str(last)
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict) and "status" in parsed:
                    return DiscoveryRunResult(**{k: v for k, v in parsed.items() if k in DiscoveryRunResult.__dataclass_fields__})
            except (json.JSONDecodeError, TypeError):
                pass

    return DiscoveryRunResult(status="failed", summary="Could not parse agent result")


def _error_result(index: int, source_key: str, url: str, exc: Exception) -> dict[str, Any]:
    return {
        "index": index,
        "source_key": source_key,
        "url": url,
        "url_type": "wechat" if "mp.weixin.qq.com" in url else "career_site",
        "executor_type": "error",
        "elapsed_sec": 0,
        "status": "failed",
        "evidence_count": 0,
        "candidate_count": 0,
        "summary": "",
        "error": str(exc),
        "strategy_matched": False,
        "strategy_pattern": None,
    }


# -- Main ---------------------------------------------------------------------


def main() -> None:
    token = _live_tencent_token()
    if not token:
        print("No Tencent Docs token found. Set TENCENT_DOCS_TOKEN.")
        sys.exit(1)

    print(f"READGZH_API_KEY: {'set' if os.environ.get('READGZH_API_KEY') else 'MISSING'}")
    print(f"TENCENT_DOCS_TOKEN: {'set' if token else 'MISSING'}")
    print(f"DEEPSEEK_API_KEY: {'set' if os.environ.get('DEEPSEEK_API_KEY') else 'MISSING'}")

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

    # Setup in-memory DB
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)

    # Seed strategies
    wechat_strat, alibaba_strat = seed_strategies(db)
    print(f"\nSeeded strategies:")
    print(f"  WeChat:  {wechat_strat.url_pattern} (id={wechat_strat.id[:8]}...)")
    print(f"  Alibaba: {alibaba_strat.url_pattern} (id={alibaba_strat.id[:8]}...)")

    # Pre-build LLM and web nav subagent
    print("\nPre-building LLM...")
    llm_model = _build_job_discovery_llm(settings=settings)
    print("  Done.")

    # Fetch records from Tencent
    gateway = TencentSmartsheetGateway(token=token)
    results: list[dict[str, Any]] = []

    idx = 0
    for source_key in SOURCE_KEYS:
        print(f"\n{'─'*60}")
        print(f"  Source: {source_key}")
        records = _select_two_records_with_urls(gateway, source_key)
        for record in records:
            urls = extract_discovery_urls(record, source_key)
            url = urls[0]
            idx += 1

            print(f"\n{'='*60}")
            print(f"  [{idx}/4] {source_key}")
            print(f"  URL: {url}")
            print(f"  Type: {'WeChat' if 'mp.weixin.qq.com' in url else 'Career site'}")
            company = _field_text(record, "公司名称") or _field_text(record, "企业名称")
            title = _field_text(record, "招聘岗位")
            if company:
                print(f"  Record: {company} — {title}")
            print(f"{'='*60}")
            sys.stdout.flush()

            try:
                summary = execute_with_strategy_router(
                    url=url,
                    source_key=source_key,
                    record=record,
                    settings=settings,
                    db=db,
                    llm_model=llm_model,
                )
            except Exception as exc:
                print(f"\n  !! CRASHED: {exc}")
                traceback.print_exc()
                summary = _error_result(idx, source_key, url, exc)

            summary["index"] = idx
            summary["source_key"] = source_key
            results.append(summary)

    db.close()

    # -- Final summary --
    print(f"\n\n{'='*60}")
    print(f"  FINAL SUMMARY — {len(results)} URLs")
    print(f"{'='*60}")
    print(f"\n  {'#':<3} {'Type':<12} {'Executor':<20} {'Status':<20} {'Evidence':>8} {'Cand.':>6} {'Time':>7}")
    print(f"  {'-'*3} {'-'*12} {'-'*20} {'-'*20} {'-'*8} {'-'*6} {'-'*7}")
    for r in results:
        print(
            f"  {r['index']:<3} {r.get('url_type', '?'):<12} {r.get('executor_type', '?'):<20} "
            f"{r['status']:<20} {r['evidence_count']:>8} {r['candidate_count']:>6} "
            f"{r['elapsed_sec']:>6.0f}s"
        )

    print(f"\n  {'─'*60}")
    for r in results:
        print(f"\n  [{r['index']}] {r['url'][:100]}")
        print(f"      Executor: {r['executor_type']}")
        print(f"      Status:   {r['status']}")
        print(f"      Evidence: {r['evidence_count']} | Candidates: {r['candidate_count']}")
        if r.get('strategy_matched'):
            print(f"      Strategy: {r['strategy_pattern']}")
        if r.get('summary'):
            print(f"      Summary:  {r['summary'][:200]}")
        if r.get('error'):
            print(f"      Error:    {r['error'][:200]}")

    # Stats
    wechat_ok = sum(1 for r in results if r.get("url_type") == "wechat" and r["evidence_count"] > 0)
    career_ok = sum(1 for r in results if r.get("url_type") == "career_site" and r["evidence_count"] > 0)
    total_candidates = sum(r["candidate_count"] for r in results)
    print(f"\n  WeChat success:  {wechat_ok}/{sum(1 for r in results if r.get('url_type')=='wechat')}")
    print(f"  Career success:  {career_ok}/{sum(1 for r in results if r.get('url_type')=='career_site')}")
    print(f"  Total candidates: {total_candidates}")

    # Write JSON
    out_path = Path(__file__).parent / "_strategy_router_smoke_output.json"
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n  Full results -> {out_path}")


if __name__ == "__main__":
    main()
