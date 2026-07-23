"""Quick check: Is the LLM API responding?"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

MAIN_PROJECT_DOTENV = Path("D:/Python/langgraph-multi-agent-career-assistant-main/.env")
try:
    from dotenv import dotenv_values
    vals = dotenv_values(MAIN_PROJECT_DOTENV, interpolate=False)
    for key in ("DEEPSEEK_API_KEY",):
        if key not in os.environ and key in vals and vals[key]:
            os.environ[key] = vals[key]
except ImportError:
    pass

from backend.app.config import Settings
from backend.app.services.job_discovery.deepagents_runner import _build_job_discovery_llm

settings = Settings(
    app_auth_secret="test-secret-with-at-least-32-characters",
    database_url="sqlite+pysqlite:///:memory:",
    redis_url="redis://localhost:6379/15",
    object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    job_discovery_enabled=True,
    job_discovery_task_timeout_seconds=300,
    job_discovery_max_pages_per_task=3,
)

print(f"DEEPSEEK_API_KEY set: {bool(os.environ.get('DEEPSEEK_API_KEY'))}")
print(f"Model: {settings.job_discovery_model}")

llm = _build_job_discovery_llm(settings=settings)
print(f"Model name: {llm.model_name}")
print(f"Base URL: {llm.openai_api_base if hasattr(llm, 'openai_api_base') else 'N/A'}")

# Test 1: Simple invoke
print("\nTest 1: Simple invoke...")
t0 = time.monotonic()
try:
    from langchain_core.messages import HumanMessage
    resp = llm.invoke([HumanMessage(content="Say 'hello' in exactly one word. Output only that word.")])
    print(f"  Response ({time.monotonic()-t0:.1f}s): {resp.content[:100]}")
except Exception as e:
    print(f"  FAILED ({time.monotonic()-t0:.1f}s): {type(e).__name__}: {e}")

# Test 2: Tool calling
print("\nTest 2: Tool calling...")
t0 = time.monotonic()
try:
    from langchain_core.tools import tool
    from langchain_core.messages import HumanMessage

    @tool
    def get_weather(city: str) -> str:
        """Get the weather for a city."""
        return f"Weather in {city}: sunny, 25C"

    llm_with_tools = llm.bind_tools([get_weather])
    resp = llm_with_tools.invoke([HumanMessage(content="What is the weather in Beijing? Answer in Chinese.")])
    elapsed = time.monotonic() - t0
    print(f"  Response ({elapsed:.1f}s):")
    print(f"    content: {resp.content[:200] if resp.content else '<empty>'}")
    print(f"    tool_calls: {len(resp.tool_calls) if resp.tool_calls else 0}")
    if resp.tool_calls:
        for tc in resp.tool_calls:
            print(f"      - {tc.get('name', 'unknown')}: {str(tc.get('args', {}))[:100]}")
except Exception as e:
    print(f"  FAILED ({time.monotonic()-t0:.1f}s): {type(e).__name__}: {e}")

# Test 3: Multi-turn with tool result
print("\nTest 3: Multi-turn (agent-like)...")
t0 = time.monotonic()
try:
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

    # Turn 1: LLM calls tool
    resp1 = llm_with_tools.invoke([HumanMessage(content="What is the weather in Shanghai?")])
    tool_calls = resp1.tool_calls
    if tool_calls:
        tool_msgs = []
        for tc in tool_calls:
            result = get_weather.invoke(tc["args"])
            tool_msgs.append(ToolMessage(content=result, tool_call_id=tc["id"]))

        # Turn 2: LLM processes tool result
        resp2 = llm.invoke([HumanMessage(content="What is the weather in Shanghai?"), resp1] + tool_msgs)
        print(f"  Final response ({time.monotonic()-t0:.1f}s): {resp2.content[:200]}")
    else:
        print(f"  No tool calls ({time.monotonic()-t0:.1f}s)")
except Exception as e:
    print(f"  FAILED ({time.monotonic()-t0:.1f}s): {type(e).__name__}: {e}")

print("\nLLM connectivity check complete.")
