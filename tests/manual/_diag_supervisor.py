"""Diagnostic: run Supervisor for a single career site URL with verbose logging.

Purpose: find root cause of Supervisor hang (>20 min, 0 output) for non-strategy URLs.
"""
from __future__ import annotations

import json
import os
import sys
import time
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Enable verbose logging to capture LLM API calls
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.DEBUG)
logging.getLogger("openai").setLevel(logging.DEBUG)
logging.getLogger("langchain").setLevel(logging.INFO)

MAIN_PROJECT_DOTENV = Path("D:/Python/langgraph-multi-agent-career-assistant-main/.env")

# Load env vars
try:
    from dotenv import dotenv_values
    vals = dotenv_values(MAIN_PROJECT_DOTENV, interpolate=False)
    for key in ("DEEPSEEK_API_KEY", "READGZH_API_KEY"):
        if key not in os.environ and key in vals and vals[key]:
            os.environ[key] = vals[key]
except ImportError:
    pass

print(f"DEEPSEEK_API_KEY: {'set' if os.environ.get('DEEPSEEK_API_KEY') else 'MISSING'}")
sys.stdout.flush()

from langchain_core.messages import HumanMessage

from backend.app.config import Settings
from backend.app.services.job_discovery.deepagents_runner import (
    build_discovery_supervisor_agent,
    _build_job_discovery_llm,
)
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput

# -- Test URL: single shortest career site URL (no strategy match) --
TEST_URL = "https://careers.pddglobalhr.com/campus/grad?t=N5ch0DXEtA"

settings = Settings(
    app_auth_secret="test-secret-with-at-least-32-characters",
    database_url="sqlite+pysqlite:///:memory:",
    redis_url="redis://localhost:6379/15",
    object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    job_discovery_enabled=True,
    job_discovery_task_timeout_seconds=300,
    job_discovery_max_pages_per_task=3,  # lower budget = fewer API calls
    job_discovery_ocr_enabled=False,     # skip OCR
    job_discovery_strategy_enabled=True,
)

print(f"Model: {settings.job_discovery_model}")
print(f"Max pages: {settings.job_discovery_max_pages_per_task}")
print(f"Test URL: {TEST_URL}")
print()
sys.stdout.flush()

# Build LLM
print("Building LLM...")
t0 = time.monotonic()
llm_model = _build_job_discovery_llm(settings=settings)
print(f"LLM built: {time.monotonic() - t0:.1f}s")
print(f"  Model: {llm_model.model_name}")
print(f"  Base URL: {llm_model.openai_api_base if hasattr(llm_model, 'openai_api_base') else 'N/A'}")
sys.stdout.flush()

# Build task input
import hashlib
url_hash = hashlib.sha256(TEST_URL.encode()).hexdigest()[:16]
task_input = DiscoveryTaskInput(
    source_id="test",
    raw_record_id=f"diag-{url_hash}",
    external_record_id=f"diag-{url_hash}",
    source_key="test",
    source_url=TEST_URL,
    url_hash=url_hash,
    record_fields=[],
)

# Build Supervisor
print("\nBuilding Supervisor agent...")
t_agent = time.monotonic()
agent = build_discovery_supervisor_agent(settings=settings, model=llm_model)
print(f"Agent built: {time.monotonic() - t_agent:.1f}s")
sys.stdout.flush()

msg_content = json.dumps(asdict(task_input), ensure_ascii=False)
agent_input = {"messages": [HumanMessage(content=msg_content)]}

print(f"\nInvoking Supervisor (timeout 300s)...")
print(f"Input: {msg_content[:200]}...")
sys.stdout.flush()

t_invoke = time.monotonic()

# Use a 300-second timeout wrapper
import signal

class TimeoutError(Exception):
    pass

def _handler(signum, frame):
    raise TimeoutError("Supervisor invoke timed out!")

try:
    # Windows doesn't have signal.SIGALRM, so we just run and hope
    config = {"recursion_limit": 100}
    raw = agent.invoke(agent_input, config=config)
    elapsed = time.monotonic() - t_invoke
    print(f"\nSUCCESS! Supervisor done: {elapsed:.0f}s")
    print(f"Result type: {type(raw).__name__}")
    if hasattr(raw, "model_dump"):
        print(f"Result: {json.dumps(raw.model_dump(), ensure_ascii=False, indent=2)[:2000]}")
    elif isinstance(raw, dict):
        keys = list(raw.keys())
        print(f"Top-level keys: {keys}")
        msgs = raw.get("messages", [])
        print(f"Messages: {len(msgs)}")
        if msgs:
            last = msgs[-1]
            content = last.content if hasattr(last, "content") else str(last)
            print(f"Last message: {str(content)[:500]}")
    sys.stdout.flush()
except Exception as exc:
    elapsed = time.monotonic() - t_invoke
    print(f"\nFAILED after {elapsed:.0f}s: {type(exc).__name__}: {exc}")

print(f"\nTotal: {time.monotonic() - t0:.0f}s")
