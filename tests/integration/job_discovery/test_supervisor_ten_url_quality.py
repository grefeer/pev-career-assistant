r"""10-URL supervisor extraction evaluation.

Extracts 10 real recruitment URLs drawn from TWO smart documents and runs the
current PATH C discovery supervisor on each, recording the extracted JDs per
URL so an independent (blank-context) sub-agent can evaluate extraction quality.

Source documents (the "two smart documents"):
  - Doc A: ``docs/superpowers/specs/2026-07-22-personal-assistant-crawl-scoped-plan.md``
           ground truth -> 6 URLs (deeproute / xiaomi / pdd / 柏楚电子[wechat] /
           华金证券[wechat] / 禾赛科技[feishu, out-of-scope needs_manual_review]).
  - Doc B: ``docs/superpowers/specs/2026-07-14-campus-recruitment-career-assistant-design.md``
           core+beta acceptance sites -> 5 URLs (DJI / 小鹏 / 科大讯飞 / 小红书 / 汇川).

禾赛科技 is intentionally excluded (Doc A explicitly marks it out-of-scope:
feishu m-page renders drift + extractor returns 0 + frozen tools cannot be
modified). That leaves exactly 10 URLs (5 from each document).

Hard constraints honored (mirrors test_supervisor_baseline_real_urls.py):
  - NO site adapters for these URLs (exercises the generic supervisor +
    deterministic extraction path only).
  - NO hardcoded job info / counts / pages in prompts (supervisor prompt and
    tools are unchanged from production).
  - For the 3 Doc-A career sites with known ground truth (21 / 151 / 22), the
    objective pass criteria are count-match AND zero duplicates. For the other
    7 sites (no documented ground-truth count) the per-URL JSON records full JD
    bodies + quality signals so a blank-context sub-agent can judge quality.

Each URL run dumps a per-URL JSON record (with full candidate JDs) to
``tests/manual/_ten_url_eval_<slug>.json`` immediately on completion, so a
later crash never loses earlier results. A cross-URL summary is written to
``tests/manual/_ten_url_eval_summary.json``.

Gated by ``RUN_TEN_URL_EVAL=1`` (live LLM + Playwright). Run directly::

    $env:PYTHONUTF8=1; $env:PYTHONIOENCODING='utf-8'; $env:RUN_TEN_URL_EVAL='1'
    .\.venv\Scripts\python.exe tests/integration/job_discovery/test_supervisor_ten_url_eval.py
"""
# ruff: noqa: E402  (sys.path bootstrap must precede project imports)

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
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

# 10 URLs from the two smart documents (see module docstring).
# (slug, company, url, real_count_or_None, source_doc, site_kind)
# real_count is only known for the 3 Doc-A career sites; None elsewhere.
# Order: the 7 NEW (no ground truth) URLs run first so the generalization
# signal lands early; the 3 known Doc-A sites run last as a regression anchor,
# with xiaomi (slowest, ~10 min) last so it never blocks the new-URL signal.
URLS: list[tuple[str, str, str, int | None, str, str]] = [
    # --- Doc B: campus-recruitment-career-assistant-design core+beta sites ---
    ("dji", "大疆",
     "https://app.mokahr.com/m/campus-recruitment/dji/143359?recommendCode=DSYdQvMt#/jobs",
     None, "docB", "mokahr"),
    ("xiaopeng", "小鹏汽车",
     "https://xiaopeng.jobs.feishu.cn/s/Pycfxid-fok", None, "docB", "feishu"),
    ("iflytek", "科大讯飞",
     "https://iflytek.zhiye.com/5/jobs?shareId=42d8a3f4-fb5b-4fbb-b3cd-bb1049ae71c0&shareSource=2",
     None, "docB", "zhiye"),
    ("xiaohongshu", "小红书",
     "https://job.xiaohongshu.com/campus/landing/top_intern?referer_code=LAO7MSQAM4Q3",
     None, "docB", "self_built"),
    ("inovance", "汇川技术",
     "https://recruit.inovance.com/#/jobs?ref=AHPNGR5", None, "docB", "self_built"),
    # --- Doc A: personal-assistant-crawl-scoped-plan ground truth (new wechat) ---
    ("baichu", "柏楚电子",
     "https://mp.weixin.qq.com/s/F_ehY3q8Zi3-QV-AwoOF5g", None, "docA", "wechat"),
    ("huajin", "华金证券",
     "https://mp.weixin.qq.com/s/rjuqB1qQnl9sy5qX9-Xs3w", None, "docA", "wechat"),
    # --- Doc A: known ground-truth career sites (regression anchor) ---
    ("deeproute", "元戎启行",
     "https://app.mokahr.com/campus-recruitment/deeproute/145894#/home", 21, "docA", "mokahr"),
    ("pdd", "拼多多",
     "https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x", 22, "docA", "pdd_spa"),
    ("xiaomi", "小米",
     "https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY", 151, "docA", "mioffice"),
]

_OUT_DIR = Path(__file__).resolve().parents[2] / "manual"
RECURSION_LIMIT = 25
# Cap how many candidates get their full JD body dumped (keeps each JSON file
# bounded for the ~150-job xiaomi case while still exposing real JD quality).
_MAX_FULL_JD_DUMP = 30
# Per-field truncation for the dumped JD body (chars).
_JD_BODY_TRUNC = 1000

_LIVE_ENABLED = bool(os.environ.get("RUN_TEN_URL_EVAL"))


def _settings() -> Settings:
    """Same generous nav budget as the 3-URL baseline (NOT a hardcoded count/page
    injected into a prompt)."""
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
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _loc_signature(locations) -> str:
    """Stable location signature mirroring canonical_job_deduplicator._loc_key
    (including multi-city string splitting, so the test's unique_count mirrors
    production dedup after the loc_key fix)."""
    if not locations:
        return ""
    norms: list[str] = []
    for loc in locations:
        _raw = str(loc or "")
        for _d in ("、", "，", ",", "/", ";", "；"):
            _raw = _raw.replace(_d, "、")
        for part in _raw.split("、"):
            s = part.strip()
            if not s:
                continue
            for suf in ("自治区", "省", "市"):
                if s.endswith(suf) and len(s) > len(suf):
                    s = s[: -len(suf)]
                    break
            norms.append(s)
    return "|".join(sorted(set(norms)))


def _unique_count(candidates: list) -> int:
    """Count unique candidates, mirroring production canonical dedup's split
    identity (full-JD by (title, loc); title-only by title) and its title-only
    echo-drop (a title-only candidate whose title matches a full-JD
    candidate's title is a redundant list-page echo and is dropped). See the
    3-URL baseline and canonical_job_deduplicator._drop_title_only_echoes."""
    full_jd_titles: set = set()
    entries: list[tuple[str, Any]] = []
    for c in candidates:
        title = normalize_title(
            c.get("title") if isinstance(c, dict) else getattr(c, "title", None))
        has_body = bool(
            ((c.get("responsibilities") if isinstance(c, dict)
              else getattr(c, "responsibilities", "")) or "").strip()
            or ((c.get("requirements") if isinstance(c, dict)
                 else getattr(c, "requirements", "")) or "").strip())
        if has_body:
            if title:
                full_jd_titles.add(title)
            locs = (c.get("locations") if isinstance(c, dict)
                    else getattr(c, "locations", None))
            entries.append(("jd", (title, _loc_signature(locs))))
        else:
            entries.append(("title", title))
    seen: set = set()
    for tag, key in entries:
        if tag == "title":
            # Title-only echo of a kept full-JD candidate -> dropped (mirrors
            # production; e.g. mokahr "#/home" list-page titles echoing the
            # "#/job/<uuid>" full JDs).
            if key in full_jd_titles:
                continue
            seen.add(key)
        else:
            seen.add(key)
    return len(seen)


def _has_body(c: Any) -> bool:
    resp = (c.get("responsibilities") if isinstance(c, dict)
            else getattr(c, "responsibilities", "")) or ""
    req = (c.get("requirements") if isinstance(c, dict)
           else getattr(c, "requirements", "")) or ""
    return bool(resp.strip() or req.strip())


def _field(c: Any, key: str):
    """Read a field from either a dict candidate or a dataclass candidate."""
    return c.get(key) if isinstance(c, dict) else getattr(c, key, None)


def _apply_url_is_listpage(apply_url: str | None, source_url: str) -> bool:
    """Heuristic: does apply_url look like the list/landing page rather than a
    per-job detail page? A signal for the eval sub-agent, not a hard verdict."""
    if not apply_url:
        return True  # missing apply_url counts as "not a detail link"
    def _strip(u: str) -> str:
        u = u.split("#")[0].split("?")[0].rstrip("/")
        return u
    if _strip(apply_url) == _strip(source_url):
        return True
    low = apply_url.lower()
    for frag in ("#/home", "#/jobs", "/jobs", "/campus/grad",
                 "/campus/landing", "/landing"):
        if low.rstrip("/").endswith(frag):
            return True
    return False


def _run_supervisor(company: str, url: str, settings: Settings,
                    db: Session, llm_model) -> DiscoveryRunResult:
    """Run the REAL PATH C supervisor on one URL and parse its result."""
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    task_input = DiscoveryTaskInput(
        source_id="ten_url_eval",
        raw_record_id=f"eval-{url_hash}",
        external_record_id=f"eval-{url_hash}",
        source_key="ten_url_eval",
        source_url=url,
        url_hash=url_hash,
        record_fields=[],
    )
    router = StrategyRouter(db)
    router.match(url)  # None for these URLs -> PATH C supervisor

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


def _build_record(slug: str, company: str, url: str, real_count: int | None,
                  source_doc: str, site_kind: str,
                  result: DiscoveryRunResult | None, elapsed: float,
                  error: str | None = None) -> dict:
    """Build the per-URL evaluation record with full JD bodies + quality signals."""
    cands: list = list((result.candidates or []) if result else [])
    raw_count = len(cands)
    unique = _unique_count(cands)
    dup_count = raw_count - unique

    # Duplicate-title groups remaining AFTER dedup (a non-empty list means the
    # canonical dedup left same-titled candidates un-merged -> a quality smell).
    title_counts: dict[str, int] = {}
    for c in cands:
        t = normalize_title(
            c.get("title") if isinstance(c, dict) else getattr(c, "title", None))
        title_counts[t] = title_counts.get(t, 0) + 1
    dup_title_groups = [
        {"title": t, "count": n} for t, n in title_counts.items() if n > 1
    ]

    count_with_body = sum(1 for c in cands if _has_body(c))
    apply_urls = [c.apply_url if not isinstance(c, dict) else c.get("apply_url")
                  for c in cands]
    apply_urls = [u for u in apply_urls if u]
    missing_apply = raw_count - len(apply_urls)
    listpage_apply = sum(
        1 for u in apply_urls if _apply_url_is_listpage(u, url))

    def _trunc(s: str | None) -> str:
        s = s or ""
        return s if len(s) <= _JD_BODY_TRUNC else s[:_JD_BODY_TRUNC] + "…<truncated>"

    # Compact per-candidate view for ALL candidates (title + body flag + loc + url).
    titles_all = []
    for c in cands:
        titles_all.append({
            "title": _field(c, "title"),
            "has_body": _has_body(c),
            "locations": list(_field(c, "locations") or []),
            "apply_url": _field(c, "apply_url"),
            "recruitment_types": list(_field(c, "recruitment_types") or []),
        })

    # Full JD body dump for the first N candidates (cap to bound file size).
    candidates_sample = []
    for c in cands[:_MAX_FULL_JD_DUMP]:
        candidates_sample.append({
            "title": _field(c, "title"),
            "company_name": _field(c, "company_name"),
            "department": _field(c, "department"),
            "locations": list(_field(c, "locations") or []),
            "recruitment_types": list(_field(c, "recruitment_types") or []),
            "apply_url": _field(c, "apply_url"),
            "deadline_text": _field(c, "deadline_text"),
            "has_body": _has_body(c),
            "responsibilities": _trunc(_field(c, "responsibilities")),
            "requirements": _trunc(_field(c, "requirements")),
            "description_text": _trunc(_field(c, "description_text")),
            "normalization_warnings": list(_field(c, "normalization_warnings") or []),
        })

    # Objective verdict for the 3 known ground-truth URLs.
    if real_count is not None:
        objective_pass = (unique == real_count and dup_count == 0)
        objective_reason = (
            f"unique {unique} vs real {real_count}, dups {dup_count}")
    else:
        objective_pass = None
        objective_reason = "no documented ground-truth count (qualitative eval)"

    return {
        "company": company, "slug": slug, "url": url,
        "source_url": url, "source_doc": source_doc, "site_kind": site_kind,
        "real_count": real_count,
        "status": (result.status if result else "crashed"),
        "block_reason": (getattr(result, "block_reason", None) if result else None),
        "raw_count": raw_count, "unique_count": unique,
        "duplicate_count": dup_count,
        "evidence_count": len((result.evidence or []) if result else []),
        "elapsed_sec": round(elapsed, 1),
        "summary": ((result.summary or "")[:600] if result else ""),
        # quality signals
        "count_with_body": count_with_body,
        "count_title_only": raw_count - count_with_body,
        "count_missing_apply_url": missing_apply,
        "count_apply_url_is_listpage": listpage_apply,
        "duplicate_title_groups_after_dedup": dup_title_groups,
        "objective_pass": objective_pass,
        "objective_reason": objective_reason,
        # full data for the eval sub-agent
        "titles_all": titles_all,
        "candidates_sample": candidates_sample,
        "apply_urls_sample": apply_urls[:15],
        "error": error,
    }


def _dump_record(record: dict) -> Path:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _OUT_DIR / f"_ten_url_eval_{record['slug']}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return out


def _eval_one(slug: str, company: str, url: str, real_count: int | None,
              source_doc: str, site_kind: str,
              settings: Settings, db: Session, llm_model) -> dict:
    print(f"\n{'='*70}\n  {company}  (real={real_count}, kind={site_kind}, "
          f"doc={source_doc})\n  {url}\n{'='*70}", flush=True)
    t0 = time.monotonic()
    result: DiscoveryRunResult | None = None
    error: str | None = None
    try:
        result = _run_supervisor(company, url, settings, db, llm_model)
    except Exception as exc:  # noqa: BLE001 - record crash, keep going
        error = f"{type(exc).__name__}: {exc}\n" + traceback.format_exc()[-800:]
        print(f"  !! CRASHED: {exc}", flush=True)
    elapsed = time.monotonic() - t0
    record = _build_record(slug, company, url, real_count, source_doc, site_kind,
                           result, elapsed, error)
    _dump_record(record)
    print(
        f"  -> status={record['status']} raw={record['raw_count']} "
        f"unique={record['unique_count']} dups={record['duplicate_count']} "
        f"evidence={record['evidence_count']} "
        f"body={record['count_with_body']}/{record['raw_count']} "
        f"listpage_apply={record['count_apply_url_is_listpage']} "
        f"elapsed={record['elapsed_sec']}s\n"
        f"  objective: {record['objective_pass']} ({record['objective_reason']})",
        flush=True)
    return record


@pytest.mark.skipif(not _LIVE_ENABLED,
                    reason="needs live LLM + Playwright (set RUN_TEN_URL_EVAL=1)")
@pytest.mark.parametrize(
    ("slug", "company", "url", "real_count", "source_doc", "site_kind"),
    URLS,
    ids=[u[0] for u in URLS],
)
def test_supervisor_ten_url_eval(
    slug: str, company: str, url: str, real_count: int | None,
    source_doc: str, site_kind: str,
) -> None:
    """Run the supervisor on one URL and dump its full-JD eval record.

    Lets ``pytest -k <slug>`` re-run a single URL during an optimization round
    instead of re-running all 10. Per-URL JSON is written on completion (and on
    crash), so partial progress always survives.
    """
    settings = _settings()
    db = _setup_db()
    llm_model = _build_job_discovery_llm(settings=settings)
    record = _eval_one(slug, company, url, real_count, source_doc, site_kind,
                      settings, db, llm_model)
    # Objective gate only where ground truth is documented.
    if real_count is not None:
        assert record["unique_count"] == real_count, (
            f"{company}: unique {record['unique_count']} != real {real_count}")
        assert record["duplicate_count"] == 0, (
            f"{company}: {record['duplicate_count']} duplicates remain")
    else:
        # For qualitative URLs just ensure the run did not outright crash.
        assert record["status"] != "crashed", (
            f"{company}: supervisor crashed: {record['error']}")


if __name__ == "__main__":
    if not _LIVE_ENABLED:
        os.environ["RUN_TEN_URL_EVAL"] = "1"
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    settings = _settings()
    db = _setup_db()
    llm_model = _build_job_discovery_llm(settings=settings)
    rows = []
    for slug, company, url, real_count, source_doc, site_kind in URLS:
        rec = _eval_one(slug, company, url, real_count, source_doc, site_kind,
                        settings, db, llm_model)
        rows.append({k: rec[k] for k in (
            "company", "slug", "url", "source_doc", "site_kind", "real_count",
            "status", "raw_count", "unique_count", "duplicate_count",
            "evidence_count", "count_with_body", "count_apply_url_is_listpage",
            "objective_pass", "objective_reason", "elapsed_sec", "error")})

    known = [r for r in rows if r["real_count"] is not None]
    known_pass = sum(1 for r in known if r["objective_pass"])
    summary = {
        "total": len(rows),
        "known_ground_truth": len(known),
        "known_pass": known_pass,
        "rows": rows,
    }
    (_OUT_DIR / "_ten_url_eval_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 70)
    print(f"  SUMMARY: {len(rows)} URLs | known {known_pass}/{len(known)} pass")
    print("=" * 70)
    for r in rows:
        print(f"  {r['company']:<8} status={r['status']:<20} "
              f"raw={r['raw_count']:<4} unique={r['unique_count']:<4} "
              f"dups={r['duplicate_count']:<3} body={r['count_with_body']:<4} "
              f"listpage={r['count_apply_url_is_listpage']:<3} "
              f"obj={r['objective_pass']}")
