"""PDD (pddglobalhr) search-box driven re-crawl via the site's list API.

The prior Supervisor crawl got 22 title-only candidates (no JD body): the
SPA's listing cards expose no body and no clickable href, and the supervisor
never reached the detail pages. But PDD's own front end fetches jobs (with
full ``jobDuty`` bodies) from a public ``position/list`` JSON API keyed by a
``name`` (title keyword) search param -- exactly the site search box, but
called directly.

So: POST each role keyword to the list API, collect every returned job (which
already carries its full responsibilities body inline), dedupe by position id,
and persist through the worker's ``_persist_candidates`` path. No Playwright,
no detail-page visits; the search box does the filtering the user asked for.

Targeted for the personal deliverable (user 高硕谦, desired roles
ai应用开发 / agent开发).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import requests
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT))

from backend.app.db.models import JobDiscoveryTask  # noqa: E402
from backend.app.repositories import job_discovery as repo  # noqa: E402
from backend.app.services.job_discovery.worker import _persist_candidates  # noqa: E402

DB_PATH = _PROJECT / "output" / "personalized_discovery" / "discovery.db"
LIST_API = "https://careers.pddglobalhr.com/api/careers/api/recruit/position/list"
SHARE_TOKEN = "AOT9z6aa0x"
DETAIL_URL_TMPL = (
    "https://careers.pddglobalhr.com/campus/grad/detail?positionId={id}&t={token}"
)
# Role keyword net for ai应用开发 / agent开发. Each is one API call; dedupe
# by position id afterwards. The relevance ranker (threshold 50) filters any
# non-AI drift back out.
KEYWORDS = [
    "AI", "Agent", "大模型", "算法", "LLM", "AIGC",
    "人工智能", "智能体", "应用开发", "机器学习", "深度学习", "NLP",
]
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Origin": "https://careers.pddglobalhr.com",
    "Referer": "https://careers.pddglobalhr.com/campus/grad",
}


def _fetch(keyword: str) -> list[dict]:
    body = {"name": keyword, "page": 1, "pageSize": 100, "t": SHARE_TOKEN}
    resp = requests.post(LIST_API, json=body, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        print(f"  [{keyword}] API success=false: {data.get('errorMsg')}", flush=True)
        return []
    return data.get("result", {}).get("list", []) or []


def _to_candidate(job: dict) -> dict | None:
    jid = job.get("id")
    body = (job.get("jobDuty") or "").strip()
    title = (job.get("name") or "").strip()
    if not jid or not body or not title:
        return None  # title-only or body-less: nothing to recommend
    location = job.get("workLocationName") or ""
    category = job.get("jobName") or None
    apply_url = DETAIL_URL_TMPL.format(id=jid, token=SHARE_TOKEN)
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return {
        "title": title,
        "company_name": "拼多多",
        "department": category,
        "description_text": None,
        "responsibilities": body,
        "requirements": None,
        "locations": [location] if location else [],
        "recruitment_types": ["校招"],
        "industries": [],
        "apply_url": apply_url,
        "evidence_refs": [
            {"content_hash": content_hash, "url": apply_url, "type": "job_listing"}
        ],
    }


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"discovery.db not found: {DB_PATH}")
    engine = create_engine(
        f"sqlite+pysqlite:///{DB_PATH.as_posix()}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()

    task = db.scalar(
        select(JobDiscoveryTask).where(JobDiscoveryTask.source_url.like("%pddglobalhr%"))
    )
    if task is None:
        raise SystemExit("pdd task not found in discovery.db")
    print(f"[pdd] task id={task.id} status={task.status}", flush=True)

    by_id: dict[str, dict] = {}
    for kw in KEYWORDS:
        jobs = _fetch(kw)
        new = 0
        for j in jobs:
            jid = j.get("id")
            if jid and jid not in by_id:
                by_id[jid] = j
                new += 1
        print(f"  [{kw}] returned={len(jobs)} new={new}", flush=True)

    candidates = [c for c in (_to_candidate(j) for j in by_id.values()) if c]
    print(f"[pdd] {len(by_id)} unique jobs, {len(candidates)} with body", flush=True)
    if not candidates:
        print("[pdd] no candidates; leaving task unchanged", flush=True)
        db.close()
        return

    task.status = "running"
    db.flush()
    _persist_candidates(db, task, candidates)
    repo.mark_task_succeeded(
        db,
        task,
        result_summary_json={
            "method": "search_api",
            "keywords": KEYWORDS,
            "candidate_count": len(candidates),
            "note": "Search-box driven re-crawl via site list API (jobDuty inline).",
        },
    )
    db.commit()
    print(f"[pdd] persisted {len(candidates)} candidates; task -> succeeded", flush=True)
    db.close()


if __name__ == "__main__":
    main()
