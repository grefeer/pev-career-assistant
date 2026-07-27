"""Inovance (汇川技术) search-box driven re-crawl via the site's list API.

The prior Supervisor crawl got 34 candidates but only ONE carried a JD body
("校园招聘大使" -- not AI), so the ranker honestly returned no_match: the
SPA's listing cards expose no body, and the supervisor never reached the
detail pages. Inovance's front end fetches jobs (with full ``jobDescription``
responsibilities + ``jobRequirement`` requirements inline) from a public
``position/ad/search`` JSON API keyed by a ``keyword`` search param -- exactly
the site search box, but called directly.

So: POST each role keyword to the API, paginate, dedupe by adId, keep only
campus jobs (recruitType "1"; social-recruitment recruitType "2" needs work
experience) that are open to a master's grad (drop 博士-only roles -- the user
is a 硕士), and persist through the worker's ``_persist_candidates`` path. No
Playwright, no detail-page visits; the search box does the filtering the user
asked for.

Targeted for the personal deliverable (user 高硕谦, 控制科学与工程 硕士,
desired roles ai应用开发 / agent开发).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import requests
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT))

from backend.app.db.models import JobDiscoveryTask  # noqa: E402
from backend.app.repositories import job_discovery as repo  # noqa: E402
from backend.app.services.job_discovery.worker import _persist_candidates  # noqa: E402

DB_PATH = _PROJECT / "output" / "personalized_discovery" / "discovery.db"
LIST_API = "https://recruit.inovance.com/prod-portal-api/position/ad/search"
# Detail URL: SPA hash route is #/jobs/{adId} (path param, verified from the
# site's own <a href> links). Host matches source, so validate_application_url
# accepts it; adId pins the specific job and opens its detail page.
DETAIL_URL_TMPL = "https://recruit.inovance.com/#/jobs/{ad_id}"
# recruitType "1" == 校招 (campus); "2" == 社招 (needs work experience).
CAMPUS_RECRUIT_TYPE = "1"
# User is a master's grad; drop 博士-only campus roles.
EXCLUDE_DEGREES = {"博士"}
# Role keyword net for ai应用开发 / agent开发.
KEYWORDS = [
    "AI", "Agent", "大模型", "算法", "智能体", "NLP",
    "深度学习", "AIGC", "LLM", "机器学习", "人工智能",
]
PAGE_SIZE = 50
MAX_PAGES = 10  # cap per keyword
HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Origin": "https://recruit.inovance.com",
    "Referer": "https://recruit.inovance.com/",
    # The SPA injects these two; the API rejects calls missing X-Portal-Id
    # ("X-Portal-Id请求头格式错误"). Both are static site config, not auth.
    "X-Portal-Id": "019daf7d-4d1a-7634-87af-1f089498b6f2",
    "x-brizoo-token": "bearer",
}


def _fetch_page(keyword: str, page_num: int) -> tuple[list[dict], int, bool]:
    body = {
        "keyword": keyword,
        "hotOnly": False,
        "topOnly": False,
        "longTermOnly": False,
        "pageNum": page_num,
        "pageSize": PAGE_SIZE,
        "sortBy": "recommended",
    }
    resp = requests.post(LIST_API, json=body, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        print(f"  [{keyword}] API code={data.get('code')}: {data.get('message')}", flush=True)
        return [], 0, False
    inner = data.get("data") or {}
    records = inner.get("records") or []
    total = int(inner.get("total") or 0)
    has_more = bool(inner.get("hasMore"))
    return records, total, has_more


def _to_candidate(job: dict) -> dict | None:
    ad_id = job.get("adId")
    body = (job.get("jobDescription") or "").strip()
    require = (job.get("jobRequirement") or "").strip()
    title = (job.get("adJobName") or "").strip()
    if not ad_id or not body or not title:
        return None  # body-less -> nothing to recommend
    if str(job.get("recruitType")) != CAMPUS_RECRUIT_TYPE:
        return None  # social recruitment needs work experience
    if (job.get("degreeDesc") or "") in EXCLUDE_DEGREES:
        return None  # 博士-only; user is a master's grad
    loc_names = [w.get("name") for w in (job.get("workLocation") or []) if w.get("name")]
    department = job.get("segment") or None
    apply_url = DETAIL_URL_TMPL.format(ad_id=ad_id)
    full_body = body + ("\n\n任职要求：\n" + require if require else "")
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return {
        "title": title,
        "company_name": "汇川技术",
        "department": department,
        "description_text": None,
        "responsibilities": full_body,
        "requirements": require or None,
        "locations": loc_names,
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
        select(JobDiscoveryTask).where(JobDiscoveryTask.source_url.like("%inovance%"))
    )
    if task is None:
        raise SystemExit("inovance task not found in discovery.db")
    print(f"[inovance] task id={task.id} status={task.status}", flush=True)

    by_id: dict[str, dict] = {}
    for kw in KEYWORDS:
        kw_new = 0
        kw_total = 0
        for pg in range(1, MAX_PAGES + 1):
            records, total, has_more = _fetch_page(kw, pg)
            if not records:
                break
            kw_total = total
            new = 0
            for j in records:
                jid = j.get("adId")
                if jid and jid not in by_id:
                    by_id[jid] = j
                    new += 1
            kw_new += new
            print(f"  [{kw}] page {pg}: {len(records)} returned, {new} new", flush=True)
            if not has_more:
                break
        print(f"  [{kw}] total={kw_total} new={kw_new}", flush=True)

    candidates = [c for c in (_to_candidate(j) for j in by_id.values()) if c]
    print(
        f"[inovance] {len(by_id)} unique jobs, {len(candidates)} campus+master with body",
        flush=True,
    )
    if not candidates:
        print("[inovance] no candidates; leaving task unchanged", flush=True)
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
            "recruit_type": CAMPUS_RECRUIT_TYPE,
            "exclude_degrees": sorted(EXCLUDE_DEGREES),
            "candidate_count": len(candidates),
            "note": "Search-box driven re-crawl via site list API (jobDescription/jobRequirement inline).",
        },
    )
    db.commit()
    print(f"[inovance] persisted {len(candidates)} candidates; task -> succeeded", flush=True)
    db.close()


if __name__ == "__main__":
    main()
