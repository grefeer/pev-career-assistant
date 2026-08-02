"""Phase 6: discovery supervisor baseline against real career-site counts.

Gated by ``RUN_SUPERVISOR_BASELINE=1`` (live LLM + Playwright). Measures
whether the PATH C discovery supervisor extracts the real job count
(21 / 151 / 22) with no duplicates for the 3 corrected career URLs.

Hard constraints honored:
  - NO site adapters for these 3 URLs (exercises the generic supervisor +
    deterministic extraction path only).
  - NO hardcoded job info / counts / pages in prompts (the supervisor prompt
    and tools are unchanged from production).
  - Count match + zero duplicates are the two pass criteria (per the active
    /goal). Pagination depth and apply_url are recorded as diagnostics only.

Each parametrized case runs the REAL supervisor (``build_discovery_supervisor_agent``
+ ``invoke_supervisor_agent`` + ``parse_agent_result`` + ``enforce_result_invariants``)
and dumps a per-URL JSON record to ``tests/manual/_supervisor_baseline_<slug>.json``.
"""
# ruff: noqa: E402  (sys.path bootstrap must precede project imports)

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.services.job_discovery.deepagents_runner import (
    _build_job_discovery_llm,
    build_discovery_supervisor_agent,
    invoke_supervisor_agent,
)
from backend.app.services.job_discovery.normalization.jd_normalizer import (
    normalize_title,
)
from backend.app.services.job_discovery.result_contract import (
    AgentResultParseError,
    enforce_result_invariants,
    parse_agent_result,
)
from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    DiscoveryTaskInput,
)
from backend.app.services.job_discovery.strategy.strategy_router import StrategyRouter

# Ground truth (user-provided, corrected URLs).
# (slug, company, url, real_count, real_pages)
URLS: list[tuple[str, str, str, int, int]] = [
    ("deeproute", "元戎启行",
     "https://app.mokahr.com/campus-recruitment/deeproute/145894#/home", 21, 1),
    ("xiaomi", "小米",
     "https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY", 151, 16),
    ("pdd", "拼多多",
     "https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x", 22, 3),
]

_OUT_DIR = Path(__file__).resolve().parents[2] / "manual"
RECURSION_LIMIT = 25

# Live LLM marker (skips unless explicitly enabled).
_LIVE_ENABLED = bool(os.environ.get("RUN_SUPERVISOR_BASELINE"))


def _settings() -> Settings:
    """Build settings mirroring the manual smoke runner, but with a generous
    page-fetch budget (30) so the supervisor is not artificially capped by the
    nav page-fetch counter. This is a test setting, NOT a hardcoded count/page
    injected into a prompt."""
    return Settings(
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        job_discovery_enabled=True,
        job_discovery_task_timeout_seconds=480,
        job_discovery_max_pages_per_task=30,
        job_discovery_ocr_enabled=True,
        job_discovery_strategy_enabled=True,
    )


def _setup_db() -> Session:
    """In-memory DB. The 3 career URLs match no seeded strategy -> PATH C."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _loc_signature(locations) -> str:
    """Stable location signature mirroring canonical_job_deduplicator._loc_key.

    A role advertised in two cities is two DISTINCT postings (a city variant),
    so the signature includes the city. Two re-captures of the same listing
    share the same city -> same signature. Empty/missing -> "".
    """
    if not locations:
        return ""
    norms: list[str] = []
    for loc in locations:
        s = str(loc or "").strip()
        if not s:
            continue
        for suf in ("自治区", "省", "市"):
            if s.endswith(suf) and len(s) > len(suf):
                s = s[: -len(suf)]
                break
        norms.append(s)
    return "|".join(sorted(set(norms)))


def _unique_count(candidates: list) -> int:
    """Count unique candidates, mirroring the production dedup's split identity.

    Full-JD candidates (those with a responsibilities/requirements body) are
    counted by ``(normalized_title, location_signature)`` - each (role, city) is
    a distinct listing the site counts separately (e.g. xiaomi's 151). Title-only
    candidates (no JD body) are counted by normalized title alone - a position
    advertised in several cities is ONE position the site counts once (e.g.
    PDD's 22). This is an independent check that the dedup collapsed true
    re-captures (same title + same city for full-JD, or same title for
    title-only) without splitting city variants the site counts as one. See
    ``canonical_job_deduplicator._identity_key`` for the matching production
    logic.
    """
    seen: set = set()
    for c in candidates:
        title = normalize_title(
            c.get("title") if isinstance(c, dict) else getattr(c, "title", None))
        has_body = bool(
            ((c.get("responsibilities") if isinstance(c, dict)
              else getattr(c, "responsibilities", "")) or "").strip()
            or ((c.get("requirements") if isinstance(c, dict)
                 else getattr(c, "requirements", "")) or "").strip())
        if has_body:
            locs = (c.get("locations") if isinstance(c, dict)
                    else getattr(c, "locations", None))
            seen.add((title, _loc_signature(locs)))
        else:
            seen.add(title)
    return len(seen)


def _run_supervisor(company: str, url: str, settings: Settings,
                    db: Session, llm_model) -> DiscoveryRunResult:
    """Run the REAL PATH C supervisor on one URL and parse its result."""
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    task_input = DiscoveryTaskInput(
        source_id="baseline",
        raw_record_id=f"base-{url_hash}",
        external_record_id=f"base-{url_hash}",
        source_key="baseline",
        source_url=url,
        url_hash=url_hash,
        record_fields=[],
    )
    router = StrategyRouter(db)
    router.match(url)  # None for these career URLs -> PATH C supervisor

    agent = build_discovery_supervisor_agent(
        settings=settings, model=llm_model, snapshot_context=None)
    msg = json.dumps(asdict(task_input), ensure_ascii=False)
    agent_input = {"messages": [HumanMessage(content=msg)]}
    try:
        raw = invoke_supervisor_agent(
            agent, agent_input, config={"recursion_limit": RECURSION_LIMIT})
    except TypeError:
        raw = invoke_supervisor_agent(agent, agent_input)
    try:
        result = parse_agent_result(raw)
    except AgentResultParseError:
        result = DiscoveryRunResult(status="failed", summary="parse_failed")
    return enforce_result_invariants(result)


@pytest.mark.skipif(not _LIVE_ENABLED,
                    reason="needs live LLM + Playwright (set RUN_SUPERVISOR_BASELINE=1)")
@pytest.mark.parametrize(
    ("slug", "company", "url", "real_count", "real_pages"),
    URLS,
    ids=[u[0] for u in URLS],
)
def test_supervisor_extracts_match_real(
    slug: str, company: str, url: str, real_count: int, real_pages: int,
) -> None:
    settings = _settings()
    db = _setup_db()
    llm_model = _build_job_discovery_llm(settings=settings)

    print(f"\n{'='*70}\n  {company}  (real={real_count}, pages={real_pages})\n  {url}\n{'='*70}",
          flush=True)
    t0 = time.monotonic()
    result = _run_supervisor(company, url, settings, db, llm_model)
    elapsed = time.monotonic() - t0

    cands = result.candidates or []
    raw_count = len(cands)
    unique = _unique_count(cands)
    dup_count = raw_count - unique
    apply_urls = [c.apply_url for c in cands if getattr(c, "apply_url", None)][:5]
    title_sample = [c.title for c in cands][:12]

    # Full diagnostic dump so a single live run reveals whether an over-count
    # is true duplicates (same title twice) or genuinely distinct titles, and
    # whether title-only vs full-JD identity splitting is the cause.
    all_titles_with_body: list[dict] = []
    for c in cands:
        title = c.get("title") if isinstance(c, dict) else getattr(c, "title", None)
        has_body = bool(
            ((c.get("responsibilities") if isinstance(c, dict)
              else getattr(c, "responsibilities", "")) or "").strip()
            or ((c.get("requirements") if isinstance(c, dict)
                 else getattr(c, "requirements", "")) or "").strip())
        locs = (c.get("locations") if isinstance(c, dict)
                else getattr(c, "locations", None))
        all_titles_with_body.append(
            {"title": title, "has_body": has_body,
             "locations": list(locs or [])})

    record = {
        "company": company,
        "slug": slug,
        "url": url,
        "real_count": real_count,
        "real_pages": real_pages,
        "status": result.status,
        "raw_count": raw_count,
        "unique_count": unique,
        "duplicate_count": dup_count,
        "evidence_count": len(result.evidence or []),
        "elapsed_sec": round(elapsed, 1),
        "apply_url_sample": apply_urls,
        "title_sample": title_sample,
        "all_titles_with_body": all_titles_with_body,
        "summary": (result.summary or "")[:300],
    }
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = _OUT_DIR / f"_supervisor_baseline_{slug}.json"
    out_file.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    print(
        f"  -> status={result.status} raw={raw_count} unique={unique} "
        f"dups={dup_count} evidence={record['evidence_count']} "
        f"elapsed={record['elapsed_sec']}s\n"
        f"  titles: {title_sample}",
        flush=True)

    # Goal pass criteria: count matches real AND no duplicates remain.
    assert unique == real_count, (
        f"{company}: unique_count {unique} != real {real_count} "
        f"(raw={raw_count}, dups={dup_count}, status={result.status})")
    assert dup_count == 0, (
        f"{company}: {dup_count} duplicate candidates remain after dedup "
        f"(raw={raw_count}, unique={unique})")


if __name__ == "__main__":
    # Direct run: execute all 3 URLs and print a summary table.
    if not _LIVE_ENABLED:
        os.environ["RUN_SUPERVISOR_BASELINE"] = "1"
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    settings = _settings()
    db = _setup_db()
    llm_model = _build_job_discovery_llm(settings=settings)
    rows = []
    for slug, company, url, real_count, real_pages in URLS:
        print(f"\n{'='*70}\n  {company} (real={real_count}, pages={real_pages})\n{'='*70}",
              flush=True)
        try:
            result = _run_supervisor(company, url, settings, db, llm_model)
        except Exception as exc:  # noqa: BLE001 - surface in summary, keep going
            print(f"  !! CRASHED: {exc}", flush=True)
            rows.append({"company": company, "slug": slug, "real_count": real_count,
                         "status": "crashed", "unique_count": 0, "raw_count": 0,
                         "duplicate_count": 0, "error": str(exc)[:200]})
            continue
        cands = result.candidates or []
        unique = _unique_count(cands)
        rec = {
            "company": company, "slug": slug, "url": url,
            "real_count": real_count, "real_pages": real_pages,
            "status": result.status, "raw_count": len(cands),
            "unique_count": unique, "duplicate_count": len(cands) - unique,
            "evidence_count": len(result.evidence or []),
            "block_reason": getattr(result, "block_reason", None),
            "summary": (result.summary or "")[:400],
        }
        rows.append(rec)
        (_OUT_DIR / f"_supervisor_baseline_{slug}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  -> {rec}", flush=True)

    summary = {
        "rows": rows,
        "pass": all(r.get("unique_count") == r.get("real_count")
                    and r.get("duplicate_count") == 0 for r in rows),
    }
    (_OUT_DIR / "_supervisor_baseline_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 70)
    print(f"  SUMMARY: {'PASS' if summary['pass'] else 'FAIL'}")
    print("=" * 70)
    for r in rows:
        print(f"  {r['company']:<8} real={r.get('real_count')} "
              f"unique={r.get('unique_count')} dups={r.get('duplicate_count')} "
              f"status={r.get('status')}")
