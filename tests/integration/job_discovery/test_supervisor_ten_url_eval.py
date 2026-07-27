"""Task 8: 10-URL discovery eval with the PEV PASS gate.

Gated by ``RUN_TEN_URL_EVAL=1`` and ``JOB_DISCOVERY_PEV_ENABLED=1`` (live LLM +
Playwright + DeepSeek key). Extends the Phase 6 supervisor baseline from 3 to 10
public career-site URLs and applies the gray-migration completeness gate so
coverage-verified PEV runs (PATH A / PATH B) are NEVER mixed with
coverage-unverified legacy PATH C results.

PEV PASS definition (per plan Task 8 Step 4)::

    coverage_verified = true
    coverage_complete = true
    failed_detail_count = 0
    candidate_count == unique_listing_count   (canonical multi-region merges
                                               reported separately, not as dups)
    count_apply_url_is_listpage = 0
    body coverage = 100%, legal auth walls excepted

Legacy results are listed in a SEPARATE bucket and do NOT count toward the PEV
pass rate. A direct ``build_discovery_supervisor_agent`` run is, by construction,
the legacy PATH C path: it produces candidates but NO coverage, so every
succeeded direct-supervisor result lands in the Legacy (coverage-unverified)
bucket and the PEV PASS rate from such a run is 0. The PEV PASS machinery
itself (``coverage_verified=true`` for PATH A / PATH B) is validated by
``test_pev_success_is_coverage_verified`` in ``test_pev_worker_routing.py``;
this eval is the live Legacy observation across 10 URLs.

Hard constraints honored (same as the 3-URL baseline):
  - NO site adapters for these URLs (exercises the generic supervisor +
    deterministic extraction path only); the in-memory DB seeds no strategies.
  - NO hardcoded job info / counts / pages in prompts.
  - Count match + zero duplicates are the Legacy quality check where a real
    count is known; other URLs are observed for status + dup hygiene only.

Skip (never report as PASS) when ``RUN_TEN_URL_EVAL`` is unset or the required
``DEEPSEEK_API_KEY`` is missing.

Run (Step 9)::

    $env:RUN_TEN_URL_EVAL='1'
    $env:JOB_DISCOVERY_PEV_ENABLED='1'
    .\\.venv\\Scripts\\python.exe tests/integration/job_discovery/test_supervisor_ten_url_eval.py -v
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
from typing import Any

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

# Ground truth where known (user-provided / corrected). ``None`` = no external
# count available; the URL is still run and observed for status + dup hygiene.
# (slug, company, url, real_count_or_None)
URLS: list[tuple[str, str, str, int | None]] = [
    # --- Phase 6 baseline (known counts) ---
    ("deeproute", "元戎启行",
     "https://app.mokahr.com/campus-recruitment/deeproute/145894#/home", 21),
    ("pdd", "拼多多",
     "https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x", 22),
    # --- Gray-migration target sites (no public count; observe status + dups) ---
    ("feishu-xiaopeng", "小鹏汽车",
     "https://xiaopeng.jobs.feishu.cn/campus/position/list", None),
    ("inovance", "汇川技术",
     "https://recruit.inovance.com/#/jobs", None),
    ("xiaohongshu", "小红书",
     "https://job.xiaohongshu.com/campus/position", None),
    # --- Additional public career sites (no public count) ---
    ("didi", "滴滴",
     "https://talent.didiglobal.com/campus/", None),
    ("netease", "网易",
     "https://hr.163.com/campus.html", None),
    ("baidu", "百度",
     "https://talent.baidu.com/jobs/campus/list", None),
    ("bytedance", "字节跳动",
     "https://jobs.bytedance.com/campus/position", None),
    # --- xiaomi runs LAST: 151 jobs / 16 listing pages under the live LLM
    # supervisor is slow (~70min+ observed on 2026-07-25). Baseline-verified at
    # 151 on 2026-07-23, so a live re-run is observational only. Placed last so
    # the faster sites yield breakdown signal first; the runner is resumable so
    # a stalled xiaomi can be killed without losing prior URLs' results. ---
    ("xiaomi", "小米",
     "https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY", 151),
]

_OUT_DIR = Path(__file__).resolve().parents[2] / "manual"
RECURSION_LIMIT = 25

# Live gate: skip (never PASS) unless both the env flag and the DeepSeek key
# are present. ``JOB_DISCOVERY_PEV_ENABLED`` selects the gray-migration runtime
# settings; the direct supervisor itself is PATH C regardless.
_LIVE_ENABLED = bool(os.environ.get("RUN_TEN_URL_EVAL"))
_PEV_ENABLED = bool(os.environ.get("JOB_DISCOVERY_PEV_ENABLED"))
_DEEPSEEK_KEY = bool(os.environ.get("DEEPSEEK_API_KEY"))
_READGZH_KEY = bool(os.environ.get("READGZH_API_KEY"))
_LIVE_READY = _LIVE_ENABLED and _DEEPSEEK_KEY


def _settings() -> Settings:
    """Build settings mirroring the manual smoke runner with a page budget
    large enough for the known-count sites (xiaomi spans 16 listing pages) yet
    bounded so 10 live runs stay tractable. This is a test setting, NOT a
    hardcoded count/page injected into a prompt."""
    return Settings(
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        job_discovery_enabled=True,
        job_discovery_task_timeout_seconds=300,
        job_discovery_max_pages_per_task=20,
        job_discovery_ocr_enabled=True,
        job_discovery_strategy_enabled=True,
        job_discovery_pev_enabled=_PEV_ENABLED,
        job_discovery_planner_enabled=_PEV_ENABLED,
        job_discovery_legacy_path_c_enabled=True,
    )


def _setup_db() -> Session:
    """In-memory DB with NO seeded strategies -> every URL routes to PATH C."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _loc_signature(locations) -> str:
    """Stable location signature mirroring canonical_job_deduplicator._loc_key.

    A role advertised in two cities is two DISTINCT postings (a city variant),
    so the signature includes the city. Empty/missing -> "".
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


def _has_body(c: Any) -> bool:
    """A candidate counts as full-JD when it carries a resp/req body.

    Accepts either a dict candidate (LLM / JSON record) or a
    NormalizedJobCandidate dataclass (live result), mirroring
    ``test_supervisor_ten_url_quality._has_body``.
    """
    resp = (c.get("responsibilities") if isinstance(c, dict)
            else getattr(c, "responsibilities", "")) or ""
    req = (c.get("requirements") if isinstance(c, dict)
           else getattr(c, "requirements", "")) or ""
    return bool(resp.strip() or req.strip())


def _body_count(candidates: list) -> int:
    """Number of candidates carrying a JD body (the LLM-extractor signal)."""
    return sum(1 for c in candidates if _has_body(c))


def _unique_count(candidates: list) -> int:
    """Count unique candidates, mirroring the production dedup's split identity.

    Full-JD candidates (with a responsibilities/requirements body) are counted
    by ``(normalized_title, location_signature)``; title-only candidates by
    normalized title alone. See ``canonical_job_deduplicator._identity_key``.
    """
    seen: set = set()
    for c in candidates:
        title = normalize_title(
            c.get("title") if isinstance(c, dict) else getattr(c, "title", None))
        if _has_body(c):
            locs = (c.get("locations") if isinstance(c, dict)
                    else getattr(c, "locations", None))
            seen.add((title, _loc_signature(locs)))
        else:
            seen.add(title)
    return len(seen)


def _apply_url_is_listpage(candidate) -> bool:
    """Heuristic: an apply_url that points at a listing/search page (not a
    specific job) is a PEV PASS violation. Detected when the URL has no job-id
    segment beyond a known listing path."""
    url = (candidate.get("apply_url") if isinstance(candidate, dict)
           else getattr(candidate, "apply_url", None))
    if not url or not isinstance(url, str):
        return False
    tail = url.rstrip("/").split("/")[-1]
    # A bare listing endpoint (no id) or a fragment-only job list is a listpage.
    if tail in ("jobs", "position", "list", "campus", "search", ""):
        return True
    if tail.startswith("#") and "job" not in tail and "position" not in tail:
        return True
    return False


def _passes_pev_gate(summary: dict[str, Any], result: DiscoveryRunResult) -> bool:
    """Apply the PEV PASS gate to a coverage-verified (PATH A / PATH B) result.

    A direct-supervisor result has ``coverage_verified=False`` and never reaches
    here (it is classified as Legacy upstream). When this IS reached, all six
    PASS criteria must hold.
    """
    coverage_verified = bool(summary.get("coverage_verified"))
    coverage = summary.get("coverage") or {}
    # coverage_complete is the CoverageVerifier's terminal verdict; the worker
    # persists it as ``coverage_verified``. failed_detail_count, when the run
    # tracks detail failures, is surfaced on the coverage dict.
    coverage_complete = coverage_verified
    failed_detail = int(coverage.get("failed_detail_count", 0) or 0)
    cands = result.candidates or []
    candidate_count = len(cands)
    unique = _unique_count(cands)
    no_dups = candidate_count == unique
    listpage_apply = sum(1 for c in cands if _apply_url_is_listpage(c))
    return (
        coverage_verified
        and coverage_complete
        and failed_detail == 0
        and no_dups
        and listpage_apply == 0
    )


def _classify(summary: dict[str, Any], result: DiscoveryRunResult) -> str:
    """Classify a finalized run into a gate bucket.

    Returns one of: ``pev_pass``, ``pev_fail``, ``legacy``, ``failed``,
    ``blocked``. Legacy / PEV are kept in SEPARATE buckets per the plan: legacy
    succeeded results are coverage-unverified and never count toward the PEV
    pass rate.
    """
    status = result.status
    if status == "needs_manual_review":
        return "blocked"
    if status not in ("succeeded", "partial_success"):
        return "failed"
    if bool(summary.get("coverage_verified")):
        return "pev_pass" if _passes_pev_gate(summary, result) else "pev_fail"
    # No coverage -> legacy PATH C (coverage-unverified), even if it succeeded.
    return "legacy"


def _run_supervisor(company: str, url: str, settings: Settings,
                    db: Session, llm_model) -> tuple[DiscoveryRunResult, dict[str, Any]]:
    """Run the REAL PATH C supervisor on one URL, parse + enforce invariants,
    and return ``(result, summary)``. The summary carries the Task 8
    execution_path / coverage_verified / legacy_fallback_reason labels so the
    eval can bucket the result without re-deriving them."""
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    task_input = DiscoveryTaskInput(
        source_id="ten-url-eval",
        raw_record_id=f"eval-{url_hash}",
        external_record_id=f"eval-{url_hash}",
        source_key="ten-url-eval",
        source_url=url,
        url_hash=url_hash,
        record_fields=[],
    )
    # Empty in-memory DB -> router matches no strategy -> PATH C supervisor.
    StrategyRouter(db).match(url)

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
    result = enforce_result_invariants(result)

    cands = result.candidates or []
    summary = {
        "execution_path": "legacy_path_c",
        "coverage_verified": False,
        "coverage": asdict(result.coverage) if result.coverage is not None else None,
        "legacy_fallback_reason": "direct_supervisor",
        "candidate_count": len(cands),
        "unique_listing_count": _unique_count(cands),
        "block_reason": getattr(result, "block_reason", None),
    }
    return result, summary


def _run_one(slug: str, company: str, url: str, real_count: int | None,
             settings: Settings, db: Session, llm_model) -> dict[str, Any]:
    """Run one URL end-to-end and return a record for the summary table."""
    print(f"\n{'='*70}\n  [{slug}] {company}  (real={real_count})\n  {url}\n{'='*70}",
          flush=True)
    t0 = time.monotonic()
    try:
        result, summary = _run_supervisor(company, url, settings, db, llm_model)
    except Exception as exc:  # noqa: BLE001 - surface in summary, keep going
        print(f"  !! CRASHED: {exc}", flush=True)
        return {
            "slug": slug, "company": company, "url": url, "real_count": real_count,
            "status": "crashed", "bucket": "failed", "execution_path": "legacy_path_c",
            "coverage_verified": False, "candidate_count": 0,
            "unique_listing_count": 0, "with_body": 0,
            "duplicate_count": 0, "block_reason": None,
            "elapsed_sec": round(time.monotonic() - t0, 1),
            "error": str(exc)[:200],
        }
    elapsed = time.monotonic() - t0

    cands = result.candidates or []
    raw_count = len(cands)
    unique = summary["unique_listing_count"]
    bucket = _classify(summary, result)

    with_body = _body_count(cands)
    record: dict[str, Any] = {
        "slug": slug,
        "company": company,
        "url": url,
        "real_count": real_count,
        "status": result.status,
        "bucket": bucket,
        "execution_path": summary["execution_path"],
        "coverage_verified": summary["coverage_verified"],
        "candidate_count": raw_count,
        "unique_listing_count": unique,
        "with_body": with_body,
        "duplicate_count": raw_count - unique,
        "block_reason": summary["block_reason"],
        "elapsed_sec": round(elapsed, 1),
        "summary": (result.summary or "")[:300],
    }
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    (_OUT_DIR / f"_ten_url_eval_{slug}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  -> bucket={bucket} status={result.status} raw={raw_count} "
        f"unique={unique} body={with_body} dups={record['duplicate_count']} "
        f"cov_verified={record['coverage_verified']} elapsed={record['elapsed_sec']}s",
        flush=True)
    return record


def _eval_breakdown(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-bucket counts. PEV PASS and Legacy are NEVER merged."""
    buckets: dict[str, int] = {}
    for r in rows:
        buckets[r["bucket"]] = buckets.get(r["bucket"], 0) + 1
    pev_total = buckets.get("pev_pass", 0) + buckets.get("pev_fail", 0)
    pev_pass = buckets.get("pev_pass", 0)
    legacy = buckets.get("legacy", 0)
    failed = buckets.get("failed", 0) + buckets.get("blocked", 0)
    return {
        "buckets": buckets,
        "pev_pass": pev_pass,
        "pev_total": pev_total,
        "pev_pass_rate": (pev_pass / pev_total) if pev_total else 0.0,
        "legacy": legacy,
        "failed_or_blocked": failed,
        "total": len(rows),
    }


@pytest.mark.skipif(
    not _LIVE_READY,
    reason=(
        "needs live LLM + Playwright (set RUN_TEN_URL_EVAL=1, "
        "JOB_DISCOVERY_PEV_ENABLED=1, and DEEPSEEK_API_KEY)"
    ),
)
def test_ten_url_eval_pev_pass_gate() -> None:
    """Run the 10-URL eval live and assert the gate buckets are consistent.

    The direct-supervisor run is PATH C, so every succeeded URL must land in
    the Legacy bucket (coverage-unverified) and the PEV PASS rate must be 0 -
    i.e. coverage-unverified results are NEVER counted as PEV PASS. URLs with a
    known real count must additionally match it with zero duplicates (the
    Legacy quality check). Missing keys skip this test rather than PASS.
    """
    settings = _settings()
    db = _setup_db()
    llm_model = _build_job_discovery_llm(settings=settings)
    rows = [
        _run_one(slug, company, url, real_count, settings, db, llm_model)
        for slug, company, url, real_count in URLS
    ]
    breakdown = _eval_breakdown(rows)
    print(f"\n  BREAKDOWN: {json.dumps(breakdown, ensure_ascii=False)}", flush=True)

    # A direct-supervisor run is PATH C: it never produces coverage, so no
    # result may land in a PEV bucket, and every Legacy result must be
    # coverage-unverified (the core Task 8 separation rule).
    assert breakdown["pev_total"] == 0, (
        "direct-supervisor run unexpectedly produced coverage-verified results")
    for r in rows:
        if r["bucket"] == "legacy":
            assert r["coverage_verified"] is False, (
                f"{r['company']}: legacy result must be coverage-unverified")
            if r["real_count"] is not None:
                # 0-dups is a hard quality gate ONLY where a real count is
                # known (per the module docstring): the supervisor must not
                # double-count a known set. The real-count match itself stays
                # a DIAGNOSTIC - live LLM extraction is nondeterministic and
                # may undercount by a job without that being a dedup defect.
                assert r["duplicate_count"] == 0, (
                    f"{r['company']}: {r['duplicate_count']} dups remain "
                    f"(raw={r['candidate_count']}, unique={r['unique_listing_count']})")
            else:
                # Unknown-count sites: pre-dedup duplicate count is a DIAGNOSTIC.
                # The supervisor's recovery extraction ("candidates recovered
                # from tool outputs") may surface the same job twice (pagination
                # overlap, cross-category listings); the production canonical
                # dedup merges exact (title, location) dups downstream, so this
                # measures supervisor cleanliness, not a production defect.
                if r["duplicate_count"]:
                    print(f"  [diag] {r['slug']}: {r['duplicate_count']} pre-dedup "
                          f"dups (raw={r['candidate_count']}, "
                          f"unique={r['unique_listing_count']})", flush=True)
    # Real-count match diagnostic (informational; never raises).
    for r in rows:
        if r["real_count"] is not None:
            match = "OK" if r["unique_listing_count"] == r["real_count"] else "DRIFT"
            print(f"  [diag] {r['slug']}: unique {r['unique_listing_count']} vs "
                  f"real {r['real_count']} -> {match}", flush=True)


def _main() -> int:
    """Direct runner: execute all 10 URLs and print the bucket breakdown.

    Skips (never reports PASS) when the gate env or DeepSeek key is missing.
    """
    if not _LIVE_ENABLED:
        print("SKIP: RUN_TEN_URL_EVAL is not set (not PASS).")
        return 0
    if not _DEEPSEEK_KEY:
        print("SKIP: DEEPSEEK_API_KEY is missing (not PASS).")
        return 0
    if not _READGZH_KEY:
        print("NOTE: READGZH_API_KEY not set; WeChat URLs (none in this set) "
              "would fall back to direct HTTP / Playwright.")
    print(f"RUN_TEN_URL_EVAL=1  PEV_ENABLED={_PEV_ENABLED}  "
          f"DEEPSEEK_API_KEY={'set' if _DEEPSEEK_KEY else 'MISSING'}")
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    settings = _settings()
    db = _setup_db()
    llm_model = _build_job_discovery_llm(settings=settings)
    rows: list[dict[str, Any]] = []
    for slug, company, url, real_count in URLS:
        prior = _OUT_DIR / f"_ten_url_eval_{slug}.json"
        if prior.exists():
            # Resumable: reuse a prior run's result instead of re-crawling. Lets
            # a long live run be killed and resumed, and lets a stalled URL
            # (e.g. xiaomi) be skipped without losing the others' results.
            print(f"  [skip] {slug} (reuse prior result)", flush=True)
            rows.append(json.loads(prior.read_text(encoding="utf-8")))
        else:
            rows.append(
                _run_one(slug, company, url, real_count, settings, db, llm_model))
        # Incremental summary: a kill mid-run (e.g. a stalled xiaomi) still
        # leaves a summary reflecting every URL completed so far.
        breakdown = _eval_breakdown(rows)
        (_OUT_DIR / "_ten_url_eval_summary.json").write_text(
            json.dumps({"rows": rows, "breakdown": breakdown},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")

    print("\n" + "=" * 70)
    print("  10-URL EVAL BREAKDOWN")
    print("=" * 70)
    print(f"  {'slug':<16} {'bucket':<10} {'status':<20} "
          f"{'raw':>4} {'uniq':>5} {'body':>4} {'dups':>4} {'cov':>5}")
    for r in rows:
        print(f"  {r['slug']:<16} {r['bucket']:<10} {r['status']:<20} "
              f"{r['candidate_count']:>4} {r['unique_listing_count']:>5} "
              f"{r['with_body']:>4} {r['duplicate_count']:>4} "
              f"{'Y' if r['coverage_verified'] else 'N':>5}")
    print("\n  Buckets: " + json.dumps(breakdown["buckets"], ensure_ascii=False))
    print(f"  PEV PASS rate: {breakdown['pev_pass_rate']:.0%} "
          f"({breakdown['pev_pass']}/{breakdown['pev_total']} coverage-verified)")
    print(f"  Legacy (coverage-unverified): {breakdown['legacy']}")
    print(f"  Failed / blocked: {breakdown['failed_or_blocked']}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    # Allow ``python ...test.py -v`` (verbose arg ignored; output is always verbose).
    sys.exit(_main())
