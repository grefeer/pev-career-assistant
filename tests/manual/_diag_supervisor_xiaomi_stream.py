"""Diagnostic: stream the PATH C supervisor on xiaomi, printing each step.

Confirms two things for the streaming-recovery fix in ``invoke_supervisor_agent``:
  1. The supervisor actually calls ``run_web_navigation`` (its candidates must be
     in the partial state for recovery to work).
  2. On the recursion crash, ``parse_agent_result`` recovers those candidates
     from the streamed partial state instead of discarding them.

Standalone (no pytest). Run:
  $env:RUN_SUPERVISOR_BASELINE='1'; python tests/manual/_diag_supervisor_xiaomi_stream.py
"""
# ruff: noqa: E402
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from langchain_core.messages import HumanMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.services.job_discovery.deepagents_runner import (
    _build_job_discovery_llm,
    build_discovery_supervisor_agent,
)
from backend.app.services.job_discovery.result_contract import (
    enforce_result_invariants,
    parse_agent_result,
)
from backend.app.services.job_discovery.normalization.jd_normalizer import normalize_title
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput

URL = "https://xiaomi.jobs.f.mioffce.cn/s/kJVnd58xtWY"
COMPANY = "小米"
RECURSION_LIMIT = 25
REAL = 151


def _settings() -> Settings:
    return Settings(
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        job_discovery_enabled=True,
        job_discovery_task_timeout_seconds=480,
        job_discovery_max_pages_per_task=30,
        job_discovery_ocr_enabled=True,
        job_discovery_strategy_enabled=True,
    )


def _msg_summary(msg) -> str:
    mtype = type(msg).__name__
    name = getattr(msg, "name", None) or ""
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        clen = len(content)
    elif content is None:
        clen = 0
    else:
        clen = len(str(content))
    # Count tool_calls if present
    tc = getattr(msg, "tool_calls", None) or []
    tc_names = ",".join(t.get("name", "?") if isinstance(t, dict) else getattr(t, "name", "?") for t in tc)
    extra = f" tool_calls=[{tc_names}]" if tc_names else ""
    return f"{mtype} name={name!r} content_len={clen}{extra}"


def main() -> None:
    settings = _settings()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    llm = _build_job_discovery_llm(settings=settings)

    url_hash = hashlib.sha256(URL.encode()).hexdigest()[:16]
    task_input = DiscoveryTaskInput(
        source_id="baseline", raw_record_id=f"base-{url_hash}",
        external_record_id=f"base-{url_hash}", source_key="baseline",
        source_url=URL, url_hash=url_hash, record_fields=[],
    )
    agent = build_discovery_supervisor_agent(
        settings=settings, model=llm, snapshot_context=None)
    agent_input = {"messages": [HumanMessage(content=json.dumps(asdict(task_input), ensure_ascii=False))]}
    config = {"recursion_limit": RECURSION_LIMIT}

    print(f"\n{'='*70}\n  {COMPANY} (real={REAL}) streaming supervisor, recursion_limit={RECURSION_LIMIT}\n{'='*70}",
          flush=True)
    t0 = time.monotonic()
    last_state = None
    step = 0
    try:
        for state in agent.stream(agent_input, stream_mode="values", config=config):
            last_state = state
            step += 1
            msgs = state.get("messages", []) if isinstance(state, dict) else []
            last = msgs[-1] if msgs else None
            elapsed = time.monotonic() - t0
            print(f"  [step {step:02d} {elapsed:6.1f}s msgs={len(msgs)}] "
                  f"{_msg_summary(last) if last else '(empty)'}", flush=True)
            # Dump full content of tool outputs so we can see exactly what
            # run_web_navigation / finish_with_manual_review returned.
            if last is not None and type(last).__name__ == "ToolMessage":
                content = getattr(last, "content", "")
                name = getattr(last, "name", "")
                print(f"      >> TOOL[{name}] content: {content!r}", flush=True)
        print(f"\n  supervisor COMPLETED normally after {step} steps", flush=True)
    except Exception as exc:
        print(f"\n  supervisor RAISED {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        print(f"  (partial state has {step} streamed steps; last_state={'yes' if last_state else 'no'})",
              flush=True)

    if last_state is None:
        print("  NO partial state captured.", flush=True)
        return
    try:
        result = parse_agent_result(last_state)
        result = enforce_result_invariants(result)
    except Exception as exc:
        print(f"  parse_agent_result FAILED: {exc}", flush=True)
        return
    cands = result.candidates or []
    seen = set()
    for c in cands:
        seen.add(normalize_title(getattr(c, "title", None)))
    print(f"\n  RESULT: status={result.status} raw={len(cands)} unique={len(seen)} "
          f"(real={REAL}) elapsed={time.monotonic()-t0:.1f}s", flush=True)
    print(f"  summary: {(result.summary or '')[:200]}", flush=True)
    if cands:
        print(f"  first 8 titles: {[getattr(c,'title',None) for c in cands[:8]]}", flush=True)


if __name__ == "__main__":
    if not os.environ.get("RUN_SUPERVISOR_BASELINE"):
        os.environ["RUN_SUPERVISOR_BASELINE"] = "1"
    main()
