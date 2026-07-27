"""Xiaomi (mioffice) search-box driven re-crawl.

The legacy Supervisor crawls every listing on mioffice (151 jobs) and then
visits each detail page, exhausting its recursion budget. The user's insight
for large flat-listing sites: use the site's own search box to filter to
relevant roles FIRST, then extract the inline JD bodies straight from the
listing page -- no detail-page visits, no full crawl.

mioffice exposes search via URL params (``?keywords=...&current=N&limit=10``)
and the listing page already embeds each job's responsibilities body in the
``<a>`` link text. This script searches the user's role keywords, paginates
the filtered results, parses the inline bodies, and persists them through
the same ``_persist_candidates`` path the worker uses -- so the existing
idempotency / similarity-key / evidence / URL-safety gates all still apply.

Targeted for the personal deliverable (user 高硕谦, desired roles
ai应用开发 / agent开发). This is a search-box-driven re-crawl, not a
supervisor run; it holds the SQLite write lock only briefly.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT))

from backend.app.db.base import Base  # noqa: E402
from backend.app.db.models import JobDiscoveryTask  # noqa: E402
from backend.app.repositories import job_discovery as repo  # noqa: E402
from backend.app.services.job_discovery.worker import _persist_candidates  # noqa: E402

DB_PATH = _PROJECT / "output" / "personalized_discovery" / "discovery.db"
XIAOMI_HOST = "xiaomi.jobs.f.mioffice.cn"
BASE_LIST_URL = f"https://{XIAOMI_HOST}/toptalent/position/list"
# share_token is stable across redirects from the /s/kJVnd58xtWY share link.
SHARE_TOKEN = "MzsxNzgwNjQ1MjYyMTUyOzc2MjA3NzQ1MTg3MTQwNzU0MTk7MDsxLzI"
# User's desired roles: ai应用开发 / agent开发. Search the role net + its
# close synonyms so recall is broad; the relevance ranker (threshold 50)
# filters non-AI results back out.
KEYWORDS = ["AI", "Agent", "大模型"]
MAX_PAGES_PER_KEYWORD = 12  # safety cap (91 results / 10 per page = 10 pages)


def _search_url(keyword: str, page: int) -> str:
    return (
        f"{BASE_LIST_URL}?keywords={quote(keyword)}"
        f"&category=&location=&project=&type=&job_hot_flag="
        f"&current={page}&limit=10&functionCategory=&tag="
        f"&share_token={SHARE_TOKEN}"
    )


def _parse_listings(page) -> list[dict]:
    """Extract one page of mioffice search results into candidate dicts.

    Each result is an ``<a href="/toptalent/position/{id}/detail?...">`` whose
    innerText is::

        <title>
        [optional tag line, e.g. "AI人才专项"]
        <meta> e.g. "北京校招正式软件研发类"   (location + 校招 + 正式 + category)
        <body>  e.g. "1、GPU 国产卡的适配开发；\\n2、..."
    """
    raw = page.evaluate(
        """() => Array.from(
            document.querySelectorAll('a[href*="/position/"][href*="detail"]')
        ).map(a => ({ href: a.getAttribute('href'), text: a.innerText || '' }))"""
    )
    out: list[dict] = []
    for item in raw:
        href = item.get("href") or ""
        text = item.get("text") or ""
        if not href or not text:
            continue
        apply_url = f"https://{XIAOMI_HOST}{href}"
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            continue
        title = lines[0]
        meta_idx = next((i for i, ln in enumerate(lines) if "校招" in ln), None)
        if meta_idx is None:
            # No meta line: treat everything after the title as body.
            body = "\n".join(lines[1:]).strip()
            location, category = "", None
        else:
            meta = lines[meta_idx]
            location = meta[: meta.index("校招")] if "校招" in meta else ""
            category = meta.split("正式", 1)[1] if "正式" in meta else None
            body = "\n".join(lines[meta_idx + 1 :]).strip()
        if not body:
            continue  # title-only listing: nothing to recommend
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        out.append(
            {
                "title": title,
                "company_name": "小米",
                "department": category,
                "description_text": None,
                "responsibilities": body,
                "requirements": None,
                "locations": [location] if location else [],
                "recruitment_types": ["校招"],
                "industries": [],
                "apply_url": apply_url,
                # Self-contained evidence: the listing text IS the evidence for
                # the JD body. A non-empty evidence_refs list satisfies the
                # rank step's evidence-backed gate; content_hash feeds the
                # candidate idempotency key.
                "evidence_refs": [
                    {"content_hash": content_hash, "url": apply_url, "type": "job_listing"}
                ],
            }
        )
    return out


def _find_xiaomi_task(db: Session) -> JobDiscoveryTask:
    task = db.scalar(
        select(JobDiscoveryTask).where(JobDiscoveryTask.source_url.like("%mioffice%"))
    )
    if task is None:
        raise SystemExit("xiaomi task not found in discovery.db")
    return task


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
    task = _find_xiaomi_task(db)
    print(f"[xiaomi] task id={task.id} status={task.status} url={task.source_url}", flush=True)

    all_candidates: list[dict] = []
    seen_apply: set[str] = set()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        for kw in KEYWORDS:
            kw_count = 0
            for pg in range(1, MAX_PAGES_PER_KEYWORD + 1):
                url = _search_url(kw, pg)
                page.goto(url, wait_until="networkidle", timeout=45000)
                # Listings render client-side; give the SPA a beat if empty.
                try:
                    page.wait_for_selector(
                        'a[href*="/position/"][href*="detail"]', timeout=10000
                    )
                except Exception:
                    pass
                items = _parse_listings(page)
                if not items:
                    print(f"  [{kw}] page {pg}: 0 listings (stop)", flush=True)
                    break
                new = [c for c in items if c["apply_url"] not in seen_apply]
                for c in new:
                    seen_apply.add(c["apply_url"])
                all_candidates.extend(new)
                kw_count += len(new)
                print(
                    f"  [{kw}] page {pg}: {len(items)} listings, {len(new)} new "
                    f"(kw total {kw_count})",
                    flush=True,
                )
                if len(items) < 10:
                    break  # last page
        browser.close()

    print(f"[xiaomi] parsed {len(all_candidates)} unique candidates", flush=True)
    if not all_candidates:
        print("[xiaomi] no candidates parsed; leaving task unchanged", flush=True)
        db.close()
        return

    # Persist through the worker's path so idempotency / similarity / URL gates
    # apply identically. Mark the task succeeded so the rank step admits it
    # (provisional tier: block_reason=None + allow_provisional -> eligible).
    task.status = "running"
    db.flush()
    _persist_candidates(db, task, all_candidates)
    repo.mark_task_succeeded(
        db,
        task,
        result_summary_json={
            "method": "search_box",
            "keywords": KEYWORDS,
            "candidate_count": len(all_candidates),
            "note": "Search-box driven re-crawl; inline JD bodies from listing pages.",
        },
    )
    db.commit()
    print(
        f"[xiaomi] persisted {len(all_candidates)} candidates; task -> succeeded",
        flush=True,
    )
    db.close()


if __name__ == "__main__":
    main()
