"""Q2 verification: Adapter vs Supervisor JD count parity.

Runs the same Alibaba URL through:
  A) Normal adapter (fast, deterministic) → N candidates
  B) Supervisor takeover after forced adapter failure → M candidates

Reports whether the Supervisor can produce comparable output.
Exact match is NOT expected (different code paths), but M > 0 is required.

Usage:
  D:\Python\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe tests\manual\_verify_q2_parity.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

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
from backend.app.services.tencent_smartsheet import TencentSmartsheetGateway, TencentRecord
from backend.app.config import _literal_tencent_dotenv_values

MAIN_DOTENV = Path("D:/Python/langgraph-multi-agent-career-assistant-main/.env")
RECURSION_LIMIT = 100

URL = "https://campus-talent.alibaba.com/campus/position?campusShareCode=hmSBcg4U%2FkdbfPRQ42bRwC3dLpJCDq50vHKAzrnY_3c%3D&batchId=100000540002"


def _bootstrap():
    if not MAIN_DOTENV.exists():
        return
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(MAIN_DOTENV, interpolate=False)
        for key in ("READGZH_API_KEY",):
            if key not in os.environ and key in vals and vals[key]:
                os.environ[key] = vals[key]
    except ImportError:
        pass


_bootstrap()

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

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
db = Session(engine)

# Seed real Alibaba strategy
strat = JobDiscoveryStrategy(
    url_pattern="*talent.alibaba.com/*",
    site_type="spa",
    description="Q2 parity test",
    plan_yaml="plan: []",
    adapter="backend.app.services.job_discovery.adapters.alibaba_spa.AlibabaSPAAdapter",
    priority=10,
    degradation_threshold=3,
    recovery_threshold=2,
)
db.add(strat)
db.commit()

print(f"Strategy: {strat.url_pattern} (id={strat.id[:8]}...)")
print(f"URL: {URL[:100]}...")
print()

# ── Path A: Normal adapter ────────────────────────────────────────

from backend.app.services.job_discovery.adapters.alibaba_spa import AlibabaSPAAdapter

url_hash = hashlib.sha256(URL.encode()).hexdigest()[:16]
task_input = DiscoveryTaskInput(
    source_id="test",
    raw_record_id="test",
    external_record_id="test",
    source_key="test",
    source_url=URL,
    url_hash=url_hash,
    record_fields=[],
)

adapter = AlibabaSPAAdapter()
trajectory_a = TrajectoryBuffer(task_id=url_hash, strategy_id=strat.id, executor_type="adapter")

t0 = time.monotonic()
print("--- Path A: Normal Adapter ---")
try:
    result_a = adapter.execute(task_input, strat, trajectory_a)
    elapsed_a = time.monotonic() - t0
    print(f"  Status:     {result_a.status}")
    print(f"  Candidates: {len(result_a.candidates)}")
    print(f"  Evidence:   {len(result_a.evidence)}")
    print(f"  Time:       {elapsed_a:.0f}s")
    if result_a.candidates:
        for i, c in enumerate(result_a.candidates[:3]):
            print(f"    [{i}] {getattr(c, 'title', '?')} @ {getattr(c, 'company_name', '?')}")
        if len(result_a.candidates) > 3:
            print(f"    ... and {len(result_a.candidates) - 3} more")
except Exception as exc:
    print(f"  FAILED: {exc}")
    result_a = None
    elapsed_a = 0

# ── Path C: Supervisor (simulating takeover after adapter failure) ──

# Force failure: remove adapter from strategy
strat.adapter = None
db.commit()

print()
print("--- Path C: Supervisor Takeover ---")

trajectory_c = TrajectoryBuffer(task_id=url_hash, strategy_id=strat.id, executor_type="supervisor")
trajectory_c.record_step(
    tool="adapter", status="failed",
    params={"adapter": "mock_failed"}, result=None,
    error=Exception("Simulated adapter failure for Q2 parity test"),
)

snapshot_context = trajectory_c.to_snapshot_context()
llm_model = _build_job_discovery_llm(settings=settings)
agent = build_discovery_supervisor_agent(
    settings=settings, model=llm_model, snapshot_context=snapshot_context,
)

msg_content = json.dumps(asdict(task_input), ensure_ascii=False)
from langchain_core.messages import HumanMessage
agent_input = {"messages": [HumanMessage(content=msg_content)]}

t0 = time.monotonic()
try:
    config = {"recursion_limit": RECURSION_LIMIT}
    raw = agent.invoke(agent_input, config=config)
except TypeError:
    raw = agent.invoke(agent_input)
elapsed_c = time.monotonic() - t0

# Parse Supervisor result
from backend.app.services.job_discovery.schemas import DiscoveryRunResult as DRR

structured = raw.get("structured_response")
if hasattr(structured, "model_dump"):
    structured = structured.model_dump()
if isinstance(structured, dict) and "status" in structured:
    result_c = DRR(**{k: v for k, v in structured.items() if k in DRR.__dataclass_fields__})
elif isinstance(raw, dict) and "status" in raw:
    result_c = DRR(**{k: v for k, v in raw.items() if k in DRR.__dataclass_fields__})
else:
    result_c = DRR(status="failed", summary="Could not parse")
    msgs = raw.get("messages", [])
    if msgs:
        last = msgs[-1]
        content = last.content if hasattr(last, "content") else str(last)
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    result_c = DRR(**{k: v for k, v in parsed.items() if k in DRR.__dataclass_fields__})
            except (json.JSONDecodeError, TypeError):
                pass

print(f"  Status:     {result_c.status}")
print(f"  Candidates: {len(result_c.candidates)}")
print(f"  Evidence:   {len(result_c.evidence)}")
print(f"  Time:       {elapsed_c:.0f}s")
if result_c.candidates:
    for i, c in enumerate(result_c.candidates[:3]):
        print(f"    [{i}] {getattr(c, 'title', '?')} @ {getattr(c, 'company_name', '?')}")
    if len(result_c.candidates) > 3:
        print(f"    ... and {len(result_c.candidates) - 3} more")
print(f"  Summary:    {result_c.summary[:200]}")

db.close()

# ── Report ──
print()
print("=" * 50)
print("  Q2 VERIFICATION")
print("=" * 50)
n_a = len(result_a.candidates) if result_a else 0
n_c = len(result_c.candidates) if result_c else 0
print(f"  Adapter candidates:    {n_a}")
print(f"  Supervisor candidates: {n_c}")
print(f"  Supervisor produced > 0: {'PASS' if n_c > 0 else 'FAIL'}")

# They use different code paths, so exact parity is not expected.
# The key question: does the Supervisor produce useful output?
if n_c > 0:
    print("  Verdict: Supervisor successfully produced JD candidates after takeover.")
elif n_a > 0 and n_c == 0:
    print("  Verdict: Supervisor found 0 candidates — investigation needed.")
else:
    print("  Verdict: Both paths found 0 candidates (URL may be inaccessible).")
