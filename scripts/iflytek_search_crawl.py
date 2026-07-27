"""iFlytek (zhiye) search-box driven re-crawl via the site's list API.

The prior Supervisor crawl got 7 program-level entries with no JD body:
``/5/jobs`` lists recruitment *programs* (飞凡计划 …), and the program titles
are generic, so the supervisor never reached the real per-job JD. iFlytek's
front end fetches jobs from a public ``GetJobAdPageList`` JSON API keyed by a
``KeyWords`` search param (the site search box, called directly) that returns
every job with full ``Duty`` (responsibilities) + ``Require`` (requirements)
inline -- across ALL categories, not just the CategoryId the share link pins.

So: POST each role keyword to the API, paginate, dedupe by job Id, keep only
campus-relevant categories (校园招聘 / 飞凡计划 -- social-recruitment senior
roles require work experience, internships are for undergrads), and persist
through the worker's ``_persist_candidates`` path. This is the search-box
approach the user asked for, applied to a site whose top-level listing was
shallow.

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
LIST_API = "https://iflytek.zhiye.com/api/Jobad/GetJobAdPageList"
SHARE_ID = "42d8a3f4-fb5b-4fbb-a72b-2dab55e3a5f2"
# Per-job detail URL (SPA renders the job client-side from the path Id).
DETAIL_URL_TMPL = "https://iflytek.zhiye.com/5/jobs/{id}"
# Campus-relevant category display names. Exclude 社会招聘 (senior, needs
# experience) and 实习生/YOUNG实习生 (undergrad internships).
INCLUDE_CATEGORIES = {"校园招聘", "飞凡计划"}
# Role keyword net for ai应用开发 / agent开发.
KEYWORDS = [
    "AI", "Agent", "大模型", "算法", "智能体", "NLP",
    "深度学习", "AIGC", "LLM", "机器学习", "人工智能",
]
PAGE_SIZE = 50
MAX_PAGES = 5  # cap per keyword (largest hit "AI" = 264 -> 6 pages)
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Origin": "https://iflytek.zhiye.com",
    "Referer": "https://iflytek.zhiye.com/5/jobs",
}


def _fetch_page(keyword: str, page_index: int) -> tuple[list[dict], int]:
    body = {
        "PageIndex": page_index,
        "PageSize": PAGE_SIZE,
        "Category": [],
        "KeyWords": keyword,
        "SpecialType": 0,
        "PortalId": "",
        "DisplayFields": [
            "Category", "Kind", "LocId", "PostDate", "ClassificationOne",
        ],
    }
    resp = requests.post(LIST_API, json=body, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("Code") != 200:
        print(f"  [{keyword}] API Code={data.get('Code')}", flush=True)
        return [], 0
    return data.get("Data") or [], int(data.get("Count") or 0)


def _to_candidate(job: dict) -> dict | None:
    jid = job.get("Id")
    duty = (job.get("Duty") or "").strip()
    require = (job.get("Require") or "").strip()
    title = (job.get("JobAdName") or "").strip()
    category = job.get("Category") or ""
    if not jid or not title or not duty:
        return None  # no body -> nothing to recommend
    if category not in INCLUDE_CATEGORIES:
        return None  # social recruitment / internship: skip for this grad
    loc_names = job.get("LocNames") or []
    apply_url = DETAIL_URL_TMPL.format(id=jid)
    body = duty + ("\n\n任职要求：\n" + require if require else "")
    content_hash = hashlib.sha256(duty.encode("utf-8")).hexdigest()
    return {
        "title": title,
        "company_name": "科大讯飞",
        "department": job.get("ClassificationOne") or category,
        "description_text": None,
        "responsibilities": body,
        "requirements": require or None,
        "locations": loc_names,
        "recruitment_types": ["校招"] if category == "校园招聘" else ["校招"],
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
        select(JobDiscoveryTask).where(JobDiscoveryTask.source_url.like("%iflytek%zhiye%"))
    )
    if task is None:
        raise SystemExit("iflytek task not found in discovery.db")
    print(f"[iflytek] task id={task.id} status={task.status}", flush=True)

    by_id: dict[str, dict] = {}
    for kw in KEYWORDS:
        kw_total = 0
        for pg in range(MAX_PAGES):
            jobs, count = _fetch_page(kw, pg)
            if not jobs:
                break
            new = 0
            for j in jobs:
                jid = j.get("Id")
                if jid and jid not in by_id:
                    by_id[jid] = j
                    new += 1
            kw_total += new
            print(f"  [{kw}] page {pg}: {len(jobs)} returned, {new} new", flush=True)
            if len(jobs) < PAGE_SIZE:
                break  # last page
        print(f"  [{kw}] total new={kw_total}", flush=True)

    candidates = [c for c in (_to_candidate(j) for j in by_id.values()) if c]
    print(
        f"[iflytek] {len(by_id)} unique jobs, {len(candidates)} campus + with-body",
        flush=True,
    )
    if not candidates:
        print("[iflytek] no candidates; leaving task unchanged", flush=True)
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
            "categories": sorted(INCLUDE_CATEGORIES),
            "candidate_count": len(candidates),
            "note": "Search-box driven re-crawl via site list API (Duty/Require inline).",
        },
    )
    db.commit()
    print(f"[iflytek] persisted {len(candidates)} candidates; task -> succeeded", flush=True)
    db.close()


if __name__ == "__main__":
    main()
