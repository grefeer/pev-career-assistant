"""Dump xiaomi job-list XHR payload structure to see the `name`/`title` fields
and whether the department is concatenated into the name (noisy titles) or a
separate field.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from backend.app.services.job_discovery.deepagents_runner import (  # noqa: E402
    _NAV_USER_AGENT,
)
from playwright.sync_api import sync_playwright  # noqa: E402

URL = "https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY"
captured = []
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_page(user_agent=_NAV_USER_AGENT)
    def on_resp(resp):
        u = resp.url
        if "/api/" not in u:
            return
        ct = (resp.headers or {}).get("content-type", "")
        if "json" not in ct:
            return
        try:
            data = json.loads(resp.text())
        except Exception:
            return
        captured.append((u, data))
    p.on("response", on_resp)
    p.goto(URL, wait_until="domcontentloaded", timeout=30_000)
    try:
        p.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        p.wait_for_timeout(3_000)
    b.close()

# find the job-list payload (has a list of job objects)
print(f"captured {len(captured)} json responses", flush=True)
for u, data in captured:
    # walk for job-like objects
    def walk(obj, depth=0):
        if depth > 5: return []
        r = []
        if isinstance(obj, dict):
            kl = {k.lower() for k in obj.keys() if isinstance(k, str)}
            ind = kl & {"name","title","position","positionname","jobname","description","requirement","responsibilities","company","companyname","employer","location","locations","worklocations","city","department","category","categoryname","id","positionid","jobid","requisitionid"}
            if len(ind) >= 2:
                r.append(obj)
            for k,v in obj.items():
                r.extend(walk(v, depth+1))
        elif isinstance(obj, list):
            for it in obj[:200]:
                r.extend(walk(it, depth+1))
        return r
    jobs = walk(data)
    if jobs:
        print(f"\n=== {u[:80]} : {len(jobs)} job-like objects ===", flush=True)
        for j in jobs[:3]:
            print("  keys:", list(j.keys()), flush=True)
            for k in j.keys():
                vl = str(j[k])[:60]
                if any(kw in k.lower() for kw in ("name","title","position","depart","categ","description","require","location","city")):
                    print(f"    {k} = {vl!r}", flush=True)
        break
