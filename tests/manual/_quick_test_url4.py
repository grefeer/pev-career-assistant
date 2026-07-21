"""Quick test: URL #4 only with full agent."""
import json, os, sys, time, hashlib

os.environ["FLAGS_use_onednn"] = "0"
os.environ["PYTHONIOENCODING"] = "utf-8"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import dotenv_values
vals = dotenv_values("D:/Python/langgraph-multi-agent-career-assistant-main/.env", interpolate=False)
if "READGZH_API_KEY" in vals:
    os.environ["READGZH_API_KEY"] = vals["READGZH_API_KEY"]

from langchain_core.messages import HumanMessage
from dataclasses import asdict
from backend.app.config import Settings
from backend.app.services.job_discovery.deepagents_runner import build_discovery_supervisor_agent
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput

settings = Settings(
    app_auth_secret="test-secret-with-at-least-32-characters",
    database_url="sqlite+pysqlite:///:memory:",
    redis_url="redis://localhost:6379/15",
    object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    job_discovery_enabled=True,
    job_discovery_task_timeout_seconds=300,
    job_discovery_max_pages_per_task=5,
    job_discovery_ocr_enabled=True,
)

url = "https://campus-talent.alibaba.com/campus/position?campusShareCode=hmSBcg4U%2FkdbfPRQ42bRwLY0mzmlH0rkc43WRcGbDgg%3D&batchId=100000540002"
url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
task_input = DiscoveryTaskInput(
    source_id="tencent-intern-referrals",
    raw_record_id="ra9WWq",
    external_record_id="ra9WWq",
    source_key="tencent-intern-referrals",
    source_url=url,
    url_hash=url_hash,
    record_fields=[
        {"field": "企业名称", "text_value": {"items": [{"text": "阿里云"}]}},
        {"field": "招聘岗位", "text_value": {"items": [{"text": "研发、算法、安全、数据、产品、运营等"}]}},
    ],
)

agent = build_discovery_supervisor_agent(settings=settings)
msg_content = json.dumps(asdict(task_input), ensure_ascii=False)
t0 = time.monotonic()
raw = agent.invoke({"messages": [HumanMessage(content=msg_content)]}, config={"recursion_limit": 100})
elapsed = time.monotonic() - t0

sr = raw.get("structured_response")
if hasattr(sr, "model_dump"):
    sr = sr.model_dump()
print(f"Elapsed: {elapsed:.0f}s")
print(f"structured_response: {json.dumps(sr, ensure_ascii=False, indent=2)[:2000] if sr else 'None'}")
