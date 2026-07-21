"""Live smoke test: Adapter failure → Supervisor takeover.

Verifies:
1. Adapter fails mid-execution → Supervisor takes over
2. Trajectory records the failed step + completed steps
3. Supervisor can still extract JDs (or at least produce evidence)
4. snapshot_context carries completed_steps + failed_step to Supervisor

Usage:
  D:\Python\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe tests\manual\test_adapter_failure_takeover.py

Requires: DEEPSEEK_API_KEY, TENCENT_DOCS_TOKEN
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
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
from backend.app.services.job_discovery.schemas import DiscoveryRunResult, DiscoveryTaskInput
from backend.app.services.job_discovery.strategy.strategy_router import StrategyRouter
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer
from backend.app.services.job_discovery.adapters.base import DomainAdapter
from backend.app.services.job_mappers import BUILTIN_SOURCES, extract_discovery_urls
from backend.app.services.tencent_smartsheet import TencentRecord, TencentSmartsheetGateway
from backend.app.config import _literal_tencent_dotenv_values

# -- Constants --
MAIN_PROJECT_DOTENV = Path("D:/Python/langgraph-multi-agent-career-assistant-main/.env")
RECURSION_LIMIT = 100


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


def _live_tencent_token() -> str | None:
    values = _literal_tencent_dotenv_values(MAIN_PROJECT_DOTENV)
    return (
        os.environ.get("TEST_TENCENT_DOCS_TOKEN")
        or os.environ.get("TENCENT_DOCS_TOKEN")
        or values.get("test_tencent_docs_token")
        or values.get("tencent_docs_token")
    )


def _field_values_for_task(record: TencentRecord) -> list[dict[str, Any]]:
    return record.field_values


# ── Faulty Adapter ──────────────────────────────────────────────────


class FaultyAlibabaAdapter(DomainAdapter):
    """Alibaba adapter that fails at step 2 (evidence extraction).

    Step 1 (browser fetch) succeeds and records a trajectory step.
    Step 2 intentionally raises an exception to simulate a mid-execution failure.
    """

    url_pattern: str = "campus*.alibaba.com/*"

    def execute(self, task, strategy, trajectory):
        from backend.app.services.job_discovery.deepagents_runner import (
            _fetch_alibaba_search_api,
        )

        # Step 1: browser fetch — succeeds
        trajectory.record_step(
            "alibaba_browser_fetch", "ok",
            {"url": task.source_url},
        )
        fetch_result = _fetch_alibaba_search_api(task.source_url)
        trajectory.record_step(
            "alibaba_browser_fetch_done", "ok", {},
            {
                "page_text_len": len(fetch_result.get("page_text", "")),
                "payloads": len(fetch_result.get("payloads", [])),
            },
        )

        # Step 2: deliberate failure
        raise RuntimeError(
            "FAULT_INJECTED: simulated evidence extraction failure "
            "to test Supervisor takeover path"
        )

    def validate(self, url: str) -> bool:
        import fnmatch
        return fnmatch.fnmatch(url, self.url_pattern)


# ── Strategy seeding ────────────────────────────────────────────────


def seed_faulty_strategy(db: Session) -> JobDiscoveryStrategy:
    from sqlalchemy import select

    existing = db.scalars(
        select(JobDiscoveryStrategy).where(
            JobDiscoveryStrategy.url_pattern == "*talent.alibaba.com/*"
        )
    ).first()
    if existing:
        db.delete(existing)
        db.commit()

    strat = JobDiscoveryStrategy(
        url_pattern="*talent.alibaba.com/*",
        site_type="spa",
        description="FAULTY Alibaba adapter (test)",
        plan_yaml="plan: []",
        adapter="tests.manual.test_adapter_failure_takeover.FaultyAlibabaAdapter",
        priority=10,
        degradation_threshold=3,
        recovery_threshold=2,
    )
    db.add(strat)
    db.commit()
    return strat


# ── Full pipeline ───────────────────────────────────────────────────


def run_failure_takeover_test(
    url: str,
    source_key: str,
    record: TencentRecord,
    settings: Settings,
    db: Session,
    llm_model: Any,
) -> dict[str, Any]:
    """Run the full adapter → Supervisor takeover flow and capture everything."""
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
    print(f"  Router match: {time.monotonic() - t0:.2f}s")

    trajectory = TrajectoryBuffer(
        task_id=str(url_hash),
        strategy_id=strategy_record.id if strategy_record else None,
        executor_type="adapter",
    )

    result = None
    snapshot_context = None

    # --- PATH A: Adapter ---
    executor_type = "adapter"
    adapter_exception = None
    print(f"  -> Adapter path: {strategy_record.adapter}")
    try:
        adapter_cls = _load_class(strategy_record.adapter)
        adapter_instance = adapter_cls()
        t_adapter = time.monotonic()
        result = adapter_instance.execute(task_input, strategy_record, trajectory)
        print(f"  Adapter done: {time.monotonic() - t_adapter:.0f}s, status={result.status}")
    except Exception as exc:
        adapter_exception = exc
        print(f"  !! Adapter FAILED: {exc}")
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

    # --- PATH C: Supervisor takeover ---
    if result is None:
        print(f"  -> Supervisor takeover (adapter failed)")
        print(f"  snapshot_context: {json.dumps(snapshot_context, ensure_ascii=False, indent=2)[:500]}")

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
        "url_type": "career_site",
        "executor_type": executor_type,
        "elapsed_sec": round(elapsed, 1),
        "status": result.status if result else "unknown",
        "evidence_count": len(result.evidence) if result and result.evidence else 0,
        "candidate_count": len(result.candidates) if result and result.candidates else 0,
        "summary": (result.summary or "")[:300] if result else "",
        "adapter_exception": str(adapter_exception) if adapter_exception else None,
        "trajectory_steps": len(trajectory.steps),
        "trajectory_failed_step": trajectory.failed_step_index,
        "trajectory_snapshot_context": trajectory.to_snapshot_context(),
        "trajectory_full": trajectory.to_dict(),
    }


def _load_class(dotted_path: str):
    module_path, class_name = dotted_path.rsplit(".", 1)
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _parse_agent_result(raw: dict[str, Any]) -> Any:
    from backend.app.services.job_discovery.schemas import DiscoveryRunResult

    structured = raw.get("structured_response")
    if hasattr(structured, "model_dump"):
        structured = structured.model_dump()
    if isinstance(structured, dict) and "status" in structured:
        return DiscoveryRunResult(**{k: v for k, v in structured.items()
                                     if k in DiscoveryRunResult.__dataclass_fields__})
    if isinstance(raw, dict) and "status" in raw:
        return DiscoveryRunResult(**{k: v for k, v in raw.items()
                                     if k in DiscoveryRunResult.__dataclass_fields__})
    messages = raw.get("messages", [])
    if messages:
        last = messages[-1]
        content = last.content if hasattr(last, "content") else str(last)
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict) and "status" in parsed:
                    return DiscoveryRunResult(**{k: v for k, v in parsed.items()
                                                 if k in DiscoveryRunResult.__dataclass_fields__})
            except (json.JSONDecodeError, TypeError):
                pass
    return DiscoveryRunResult(status="failed", summary="Could not parse agent result")


# ── Main ────────────────────────────────────────────────────────────


def main() -> None:
    token = _live_tencent_token()
    if not token:
        print("ERROR: TENCENT_DOCS_TOKEN not set")
        sys.exit(1)

    print(f"DEEPSEEK_API_KEY: {'set' if os.environ.get('DEEPSEEK_API_KEY') else 'MISSING'}")
    print(f"TENCENT_DOCS_TOKEN: {'set' if token else 'MISSING'}")
    print(f"READGZH_API_KEY: {'set' if os.environ.get('READGZH_API_KEY') else 'MISSING'}")

    settings = Settings(
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        job_discovery_enabled=True,
        job_discovery_task_timeout_seconds=300,
        job_discovery_max_pages_per_task=3,
        job_discovery_ocr_enabled=True,
        job_discovery_strategy_enabled=True,
    )

    # In-memory DB
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)

    # Seed faulty strategy
    strat = seed_faulty_strategy(db)
    print(f"\nSeeded faulty strategy: {strat.url_pattern} (id={strat.id[:8]}...), adapter={strat.adapter}")

    # Pre-build LLM
    print("\nPre-building LLM...")
    llm_model = _build_job_discovery_llm(settings=settings)
    print("  Done.")

    # Fetch one Alibaba record from Tencent
    gateway = TencentSmartsheetGateway(token=token)
    source_key = "tencent-intern-referrals"
    for source in BUILTIN_SOURCES:
        if source.source_key == source_key:
            source_def = source
            break
    else:
        print(f"ERROR: unknown source {source_key}")
        sys.exit(1)

    page = gateway.list_records(source_def.file_id, source_def.sheet_id, offset=0, limit=10)
    record = None
    for r in page.records:
        urls = extract_discovery_urls(r, source_key)
        if urls and "alibaba.com" in urls[0]:
            record = r
            break
    if not record:
        print("ERROR: no Alibaba record found")
        sys.exit(1)

    url = extract_discovery_urls(record, source_key)[0]
    print(f"\n{'='*60}")
    print(f"  Test URL: {url[:120]}")
    print(f"  Source: {source_key}")
    print(f"{'='*60}\n")

    summary = run_failure_takeover_test(
        url=url,
        source_key=source_key,
        record=record,
        settings=settings,
        db=db,
        llm_model=llm_model,
    )

    db.close()

    # ── Report ──
    print(f"\n{'='*60}")
    print(f"  ADAPTER FAILURE TAKEOVER TEST — RESULTS")
    print(f"{'='*60}")
    print(f"  Adapter:          {'FAILED' if summary['adapter_exception'] else 'SUCCEEDED (unexpected)'}")
    print(f"  Executor:         {summary['executor_type']}")
    print(f"  Status:           {summary['status']}")
    print(f"  Elapsed:          {summary['elapsed_sec']}s")
    print(f"  Evidence:         {summary['evidence_count']}")
    print(f"  Candidates:       {summary['candidate_count']}")
    print(f"  Trajectory steps: {summary['trajectory_steps']}")
    print(f"  Failed step idx:  {summary['trajectory_failed_step']}")
    print(f"  Summary:          {summary['summary']}")

    print(f"\n  ── Trajectory steps ──")
    for i, s in enumerate(summary["trajectory_full"]["steps"]):
        status_mark = "✓" if s["status"] == "ok" else "✗"
        result_preview = ""
        if s.get("result"):
            r = s["result"]
            if isinstance(r, dict):
                result_preview = str({k: str(v)[:60] for k, v in list(r.items())[:3]})
            else:
                result_preview = str(r)[:80]
        print(f"  [{i}] {status_mark} {s['tool']}: {s['status']} "
              f"{'→ ' + result_preview if result_preview else ''}")
        if s.get("error"):
            print(f"      error: {s['error'][:200]}")

    print(f"\n  ── snapshot_context passed to Supervisor ──")
    ctx = summary["trajectory_snapshot_context"]
    print(f"  source:         {ctx.get('source')}")
    print(f"  completed_steps: {len(ctx.get('completed_steps', []))}")
    for cs in ctx.get("completed_steps", []):
        print(f"    - {cs['tool']}: {'ok' if cs.get('result') else 'no result'}")
    failed = ctx.get("failed_step")
    if failed:
        print(f"  failed_step:")
        print(f"    tool:   {failed['tool']}")
        print(f"    error:  {failed['error'][:200]}")
        print(f"    type:   {failed['error_type']}")

    # ── Verdict ──
    checks = []
    checks.append(("Adapter failed", summary["adapter_exception"] is not None))
    checks.append(("Supervisor took over", summary["executor_type"] == "supervisor"))
    checks.append(("Trajectory recorded steps", summary["trajectory_steps"] >= 2))
    checks.append(("Failed step index set", summary["trajectory_failed_step"] is not None))
    checks.append(("snapshot_context has completed_steps",
                   len(summary["trajectory_snapshot_context"].get("completed_steps", [])) > 0))
    checks.append(("snapshot_context has failed_step",
                   summary["trajectory_snapshot_context"].get("failed_step") is not None))
    checks.append(("Supervisor produced result", summary["status"] != "unknown"))

    print(f"\n  ── Checks ──")
    all_pass = True
    for label, passed in checks:
        mark = "✅" if passed else "❌"
        if not passed:
            all_pass = False
        print(f"  {mark} {label}")

    print(f"\n  {'ALL CHECKS PASSED' if all_pass else 'SOME CHECKS FAILED'}")

    # Write full report
    out_path = Path(__file__).parent / "_adapter_failure_takeover_output.json"
    out_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\n  Full report -> {out_path}")


if __name__ == "__main__":
    main()
