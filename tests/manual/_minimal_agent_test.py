"""Minimal agent test: single WeChat URL with timeout."""
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

# Load env
from dotenv import dotenv_values
vals = dotenv_values("D:/Python/langgraph-multi-agent-career-assistant-main/.env", interpolate=False)
for key in ("READGZH_API_KEY",):
    if key not in os.environ and key in vals and vals[key]:
        os.environ[key] = vals[key]
        print(f"[env] Loaded {key}")

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
    job_discovery_task_timeout_seconds=300,
    job_discovery_max_pages_per_task=5,
    job_discovery_ocr_enabled=True,
)

url = "https://mp.weixin.qq.com/s/6tGCObeYzmSwYDn3d3L3Hg"
task_input = DiscoveryTaskInput(
    source_id="tencent-27-referrals",
    raw_record_id="test-1",
    external_record_id="test-1",
    source_key="tencent-27-referrals",
    source_url=url,
    url_hash="testhash1234",
    record_fields=[
        {"field": "公司名称", "text_value": {"items": [{"text": "航天科技集团五院"}]}},
        {"field": "招聘岗位", "text_value": {"items": [{"text": ""}]}},
    ],
)

print(f"Building agent for: {url}")
print(f"Model: {settings.job_discovery_model}")
sys.stdout.flush()

agent = build_discovery_supervisor_agent(settings=settings)

msg_content = json.dumps(asdict(task_input), ensure_ascii=False)
agent_input = {"messages": [HumanMessage(content=msg_content)]}

t0 = time.monotonic()
print("Invoking agent... (timeout: 120s)")
sys.stdout.flush()

try:
    raw = agent.invoke(agent_input, config={"recursion_limit": 100})
    elapsed = time.monotonic() - t0
    print(f"Done in {elapsed:.0f}s")

    # Parse result
    sr = raw.get("structured_response")
    if hasattr(sr, "model_dump"):
        sr = sr.model_dump()
    if isinstance(sr, dict):
        print(f"Status: {sr.get('status')}")
        print(f"Evidence: {len(sr.get('evidence', []))}")
        print(f"Candidates: {len(sr.get('candidates', []))}")
        for c in sr.get("candidates", [])[:5]:
            print(f"  - {c.get('company_name')} / {c.get('title')} (conf={c.get('confidence')})")
        print(f"Summary: {str(sr.get('summary', ''))[:300]}")
    else:
        print(f"Raw result keys: {list(raw.keys())}")
        msgs = raw.get("messages", [])
        if msgs:
            last = msgs[-1]
            print(f"Last msg type: {type(last).__name__}")
            print(f"Last msg content (first 500): {str(last.content)[:500]}")
except Exception as e:
    elapsed = time.monotonic() - t0
    print(f"FAILED after {elapsed:.0f}s: {e}")
    import traceback
    traceback.print_exc()
