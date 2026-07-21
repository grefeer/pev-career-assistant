"""Minimal agent test with full output tracing."""
import json
import os
import sys
import time

os.environ["FLAGS_use_onednn"] = "0"
os.environ["PYTHONIOENCODING"] = "utf-8"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import dotenv_values
vals = dotenv_values("D:/Python/langgraph-multi-agent-career-assistant-main/.env", interpolate=False)
for key in ("READGZH_API_KEY",):
    if key not in os.environ and key in vals and vals[key]:
        os.environ[key] = vals[key]

from langchain_core.messages import HumanMessage
from dataclasses import asdict

from backend.app.config import Settings
from backend.app.services.job_discovery.deepagents_runner import build_discovery_supervisor_agent
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput

settings = Settings(
    app_auth_secret="test-secret-" + "x" * 20,
    database_url="sqlite+pysqlite:///:memory:",
    redis_url="redis://localhost:6379/15",
    object_encryption_key="A" * 43 + "=",
    job_discovery_enabled=True,
    job_discovery_task_timeout_seconds=600,
    job_discovery_max_pages_per_task=5,
    job_discovery_ocr_enabled=True,
)

# Test URL #2 - try a different WeChat URL
url = "https://mp.weixin.qq.com/s/oTrQQdv8FynVao4hGB0wGQ"
task_input = DiscoveryTaskInput(
    source_id="tencent-27-referrals",
    raw_record_id="test-2",
    external_record_id="test-2",
    source_key="tencent-27-referrals",
    source_url=url,
    url_hash="testhash5678",
    record_fields=[
        {"field": "公司名称", "text_value": {"items": [{"text": "蚂蚁集团"}]}},
        {"field": "招聘岗位", "text_value": {"items": [{"text": ""}]}},
    ],
)

print(f"Building agent for: {url}")
sys.stdout.flush()

agent = build_discovery_supervisor_agent(settings=settings)
msg_content = json.dumps(asdict(task_input), ensure_ascii=False)
agent_input = {"messages": [HumanMessage(content=msg_content)]}

t0 = time.monotonic()
print("Invoking...")
sys.stdout.flush()

raw = agent.invoke(agent_input, config={"recursion_limit": 100})
elapsed = time.monotonic() - t0

# Dump full result
sr = raw.get("structured_response")
if hasattr(sr, "model_dump"):
    sr = sr.model_dump()

print(f"\n=== DONE in {elapsed:.0f}s ===")
print(f"structured_response type: {type(sr).__name__}")
if isinstance(sr, dict):
    print(json.dumps(sr, ensure_ascii=False, indent=2)[:3000])
else:
    # Check messages
    msgs = raw.get("messages", [])
    print(f"Message count: {len(msgs)}")
    for i, m in enumerate(msgs):
        mtype = type(m).__name__
        content_preview = str(m.content)[:200] if hasattr(m, "content") else str(m)[:200]
        print(f"  [{i}] {mtype}: {content_preview}")
        if hasattr(m, "tool_calls"):
            for tc in m.tool_calls:
                print(f"       tool_call: {tc.get('name')} args={str(tc.get('args',''))[:200]}")

# Save full output
out_path = Path(__file__).parent / "_agent_debug_output.json"
out_path.write_text(json.dumps({
    "elapsed": elapsed,
    "structured_response": sr,
    "message_count": len(raw.get("messages", [])),
}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(f"\nFull debug output saved to: {out_path}")
