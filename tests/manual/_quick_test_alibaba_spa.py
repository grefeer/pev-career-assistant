"""Quick test: Alibaba SPA career URLs with SPA shortcut (no LLM agent)."""
import json, os, sys, time

os.environ["FLAGS_use_onednn"] = "0"
os.environ["PYTHONIOENCODING"] = "utf-8"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.services.job_discovery.deepagents_runner import run_web_navigation
from backend.app.config import Settings

settings = Settings(
    app_auth_secret="test-secret-with-at-least-32ch!!",
    database_url="sqlite+pysqlite:///:memory:",
    redis_url="redis://localhost:6379/15",
    object_encryption_key="A"*43+"=",
    job_discovery_enabled=True,
    job_discovery_task_timeout_seconds=120,
    job_discovery_max_pages_per_task=5,
)

urls = [
    "https://campus-talent.alibaba.com/campus/position?campusShareCode=hmSBcg4U%2FkdbfPRQ42bRwC3dLpJCDq50vHKAzrnY_3c%3D&batchId=100000540002",
    "https://campus-talent.alibaba.com/campus/position?campusShareCode=hmSBcg4U%2FkdbfPRQ42bRwLY0mzmlH0rkc43WRcGbDgg%3D&batchId=100000540002",
]

for i, url in enumerate(urls):
    t0 = time.monotonic()
    print(f"\n[{i+1}/2] {url[:80]}...")
    result = run_web_navigation(url, settings=settings)
    elapsed = time.monotonic() - t0
    evidence = result.get("evidence_pages") or []
    print(f"  Elapsed: {elapsed:.1f}s")
    print(f"  Evidence: {len(evidence)} items")
    for j, ev in enumerate(evidence[:5]):
        print(f"    [{j}] type={ev.get('evidence_type')} title={str(ev.get('title',''))[:60]}")
        meta = ev.get("metadata") or {}
        if meta.get("positions_count"):
            print(f"         positions_count={meta['positions_count']}")
    if len(evidence) > 5:
        print(f"    ... and {len(evidence)-5} more")
    if result.get("error"):
        print(f"  Error: {result['error']}")
    print(f"  Total: {len(evidence)} positions found in {elapsed:.0f}s")

print("\nDone.")
