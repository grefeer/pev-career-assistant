"""WeChat-article re-crawl via the ReadGZH proxy for the two wechat URLs.

The prior Supervisor crawl left baichu (柏楚电子) and huajin (华金证券) at
needs_manual_review with block_reason "unknown" -- the WebNavigationAgent hit
its recursion limit before loading content, NOT a real verification wall.
ReadGZH is the sanctioned WeChat proxy (per CLAUDE.md architecture), so this
is a legitimate retry, not a bypass: if ReadGZH hits the verification wall
(环境异常 + 完成验证后即可继续访问) or errors (e.g. 422 = article
deleted/invalid), the honest needs_manual_review outcome stands and the task
summary is updated to record the accurate reason.

For each article ReadGZH CAN fetch: run the deterministic ``extract_jd_candidates``
heuristic on the article text, give each candidate a self-contained
``evidence_refs`` (so the rank-service evidence gate passes) and a non-empty
``responsibilities`` body (so ``_has_jd_body`` passes -- for unstructured
articles the extractor only fills ``description_text``, so we lift the segment
into ``responsibilities``), and persist via ``_persist_candidates``. The ranker
(threshold 50) then decides relevance to ai应用开发 / agent开发 -- a securities
firm with no AI roles ends up no_match, which is the accurate outcome.

Targeted for the personal deliverable (user 高硕谦, desired roles
ai应用开发 / agent开发).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT))

from backend.app.db.models import JobDiscoveryTask  # noqa: E402
from backend.app.db.models import DiscoveryBlockReason  # noqa: E402
from backend.app.repositories import job_discovery as repo  # noqa: E402
from backend.app.services.job_discovery.deepagents_runner import (  # noqa: E402
    _fetch_wechat_via_readgzh,
)
from backend.app.services.job_discovery.tools.jd_extraction import (  # noqa: E402
    extract_jd_candidates,
)
from backend.app.services.job_discovery.worker import _persist_candidates  # noqa: E402

DB_PATH = _PROJECT / "output" / "personalized_discovery" / "discovery.db"

# (slug, company, url) for the two wechat articles.
ARTICLES = [
    ("baichu", "柏楚电子", "https://mp.weixin.qq.com/s/F_ehY3q8Zi3-QV-AwoOF5g"),
    ("huajin", "华金证券", "https://mp.weixin.qq.com/s/rjuqB1qQnl9sy5qX9-Xs3w"),
]


def _build_candidates(text: str, url: str, company: str) -> list:
    """Extract JD candidates from article text and normalize for the rank gate."""
    raw = extract_jd_candidates(text, url)
    out = []
    for cand in raw:
        # Unstructured articles fill description_text but leave responsibilities
        # empty -> _has_jd_body would drop them. Lift the segment into
        # responsibilities so the body is visible to the ranker.
        if not (cand.responsibilities or "").strip():
            cand.responsibilities = (cand.description_text or "").strip() or None
        body = (cand.responsibilities or cand.description_text or "").strip()
        if not body:
            continue  # nothing to show -> skip
        if not (cand.company_name or "").strip():
            cand.company_name = company
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        cand.evidence_refs = [
            {"content_hash": content_hash, "url": url, "type": "job_listing"}
        ]
        # apply_url is the article URL (host = mp.weixin.qq.com = source host,
        # so validate_application_url accepts it).
        cand.apply_url = url
        out.append(cand)
    return out


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

    for slug, company, url in ARTICLES:
        print(f"\n=== {slug} ({company}) ===", flush=True)
        task = db.scalar(
            select(JobDiscoveryTask).where(JobDiscoveryTask.source_url == url)
        )
        if task is None:
            print(f"  task not found for {url}; skipping", flush=True)
            continue
        print(f"  task id={task.id} status={task.status}", flush=True)

        text, title, error = _fetch_wechat_via_readgzh(url, readgzh_timeout=45)
        if error:
            # Genuine failure (422 = article deleted/invalid; wall = anti-bot).
            # Keep needs_manual_review but record the ACCURATE reason.
            print(f"  ReadGZH error: {error}", flush=True)
            print("  -> keeping needs_manual_review (honest failure)", flush=True)
            task.status = "needs_manual_review"
            task.finished_at = None
            repo.mark_task_needs_manual_review(
                db,
                task,
                block_reason=DiscoveryBlockReason.wechat_unavailable,
                result_summary_json={
                    "method": "readgzh_retry",
                    "readgzh_error": str(error),
                    "candidate_count": 0,
                    "note": (
                        "ReadGZH proxy could not fetch the WeChat article "
                        "(422/wall) -- article is inaccessible programmatically."
                    ),
                },
            )
            db.commit()
            continue

        print(f"  ReadGZH ok: title={title!r} len={len(text or '')}", flush=True)
        candidates = _build_candidates(text or "", url, company)
        print(f"  extracted {len(candidates)} candidates with body", flush=True)
        # Clear any stale block_reason from the prior needs_manual_review
        # crawl -- _admit_task returns NEEDS_MANUAL_REVIEW whenever block_reason
        # is non-None, which would mask a successful re-crawl.
        task.block_reason = None
        if not candidates:
            # Article fetched but no JD structure -> honest no_match.
            task.status = "running"
            db.flush()
            repo.mark_task_succeeded(
                db,
                task,
                result_summary_json={
                    "method": "readgzh_extract",
                    "article_title": title,
                    "candidate_count": 0,
                    "note": (
                        "ReadGZH fetched the article but extract_jd_candidates "
                        "found no structured JDs -> no_match."
                    ),
                },
            )
            db.commit()
            print(f"  [{slug}] 0 candidates; task -> succeeded (no_match)", flush=True)
            continue

        task.status = "running"
        db.flush()
        _persist_candidates(db, task, candidates)
        repo.mark_task_succeeded(
            db,
            task,
            result_summary_json={
                "method": "readgzh_extract",
                "article_title": title,
                "candidate_count": len(candidates),
                "note": "ReadGZH fetch + extract_jd_candidates (article text).",
            },
        )
        db.commit()
        print(
            f"  [{slug}] persisted {len(candidates)} candidates; task -> succeeded",
            flush=True,
        )

    db.close()


if __name__ == "__main__":
    main()
