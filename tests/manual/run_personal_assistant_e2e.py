"""End-to-end personal-application-assistant run.

Pipeline:
  1. Parse the user's resume PDF -> resume text -> evidence candidates ->
     profile_summary (reuses profile_parser + relevance.build_profile_summary).
  2. Seed structured preferences to the in-memory memory layer
     (preferences_service -> user_preferences) and load the summary.
  3. Run discovery on the 6 reference URLs as isolated subprocesses
     (reuses test_non_alibaba_urls.py --single --candidates) so one hung site
     cannot block the run; capture each URL's NormalizedJobCandidate list.
  4. Rank every captured candidate through RecommendationService.score_and_cache
     (batched cheap ranker, cached in job_relevance_scores) using the profile
     summary + preferences.
  5. Filter (min_score) + sort -> write suitable jobs to a UTF-8 JSON file.

Run (long, ~10-60 min depending on sites):
  .venv\\Scripts\\python.exe tests\\manual\\run_personal_assistant_e2e.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Windows console defaults to GBK; force UTF-8 so Chinese progress output
# (and candidate text excerpts) don't crash mid-run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# --- bootstrap .env so spawned subprocesses inherit READGZH_API_KEY etc. ---
_DOTENV = _PROJECT_ROOT / ".env"
if _DOTENV.exists():
    try:
        from dotenv import dotenv_values

        vals = dotenv_values(_DOTENV, interpolate=False)
        for key, val in vals.items():
            if val and key not in os.environ:
                os.environ[key] = val
    except ImportError:
        pass

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.domain.preferences import WorkModePreference
from backend.app.repositories import preferences as preferences_repo
from backend.app.services.preferences_service import set_preferences, get_preferences_summary
from backend.app.services.profile_parser import (
    extract_evidence_candidates,
    extract_resume_document,
)
from backend.app.services.relevance import build_relevance_llm
from backend.app.services.relevance.relevance_ranker import (
    RelevanceRanker,
    build_profile_summary,
)
from backend.app.services.recommendation_service import RecommendationService

# --- Inputs ----------------------------------------------------------------

RESUME_PDF = _PROJECT_ROOT / "data" / "高硕谦-东北大学-控制科学与工程-硕士-男-简历 .pdf"
DISCOVERY_SCRIPT = _PROJECT_ROOT / "tests" / "manual" / "test_non_alibaba_urls.py"
RESULT_JSON = _PROJECT_ROOT / "tests" / "manual" / "_personal_assistant_result.json"
PER_URL_TIMEOUT = 600  # seconds (matches the reference test's hard cap)
MIN_SCORE = 60.0       # "suitable" threshold
TOP_N = 40

USER_ID = "personal-user"  # seeded single user (personal_mode)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        personal_mode=True,
    )


def parse_resume() -> dict:
    """Step 1: resume PDF -> profile_summary dict for the ranker."""
    _log(f"[1/5] Parsing resume: {RESUME_PDF.name}")
    raw = RESUME_PDF.read_bytes()
    doc = extract_resume_document(RESUME_PDF.name, raw)
    if doc.needs_manual_entry or not doc.text:
        _log(f"  WARNING: resume parse error ({doc.error_code}); ranking will rely on preferences only")
    evidence = extract_evidence_candidates(doc.text or "")
    summary = build_profile_summary(doc.text, evidence)
    _log(f"  name={summary.get('name')!r} skills={summary.get('skills')[:8]}")  # type: ignore[index]
    _log(f"  education={summary.get('education')[:3]} experience_lines={len(summary.get('experience', []))}")  # type: ignore[index]
    return summary


def seed_preferences(db: Session) -> dict:
    """Step 2: persist structured preferences (the memory layer)."""
    _log("[2/5] Seeding preferences (研发 / agent·AI / 北上广深成)")
    set_preferences(
        db,
        USER_ID,
        desired_roles=[
            "研发工程师", "算法工程师", "Agent开发工程师",
            "AI应用开发工程师", "大模型应用开发", "后端开发",
        ],
        target_cities=["北京", "上海", "广州", "深圳", "成都"],
        preferred_industries=["人工智能", "自动驾驶", "互联网", "AI", "机器人"],
        preferred_recruitment_types=["校招", "校园招聘", "应届生"],
        excluded_industries=[],
        work_mode=WorkModePreference.HYBRID,
        is_active_search=True,
        notes="研发岗位，偏向 agent / AI 应用方向；意愿地：北京、上海、广州、深圳、成都",
    )
    db.commit()
    return get_preferences_summary(db, USER_ID)


def run_discovery() -> list[dict]:
    """Step 3: run the 6 URLs as subprocesses, capturing candidates each."""
    _log(f"[3/5] Running 6-URL discovery (per-URL cap {PER_URL_TIMEOUT}s)")
    # Import TEST_URLS for company labels / count without running discovery.
    sys.path.insert(0, str(DISCOVERY_SCRIPT.parent))
    from test_non_alibaba_urls import TEST_URLS  # type: ignore

    all_candidates: list[dict] = []
    for i, entry in enumerate(TEST_URLS):
        _log(f"  [{i+1}/{len(TEST_URLS)}] {entry['company']} ({entry['url_type']}) ...")
        t0 = time.monotonic()
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="_e2e_url_")
        os.close(tmp_fd)
        try:
            try:
                subprocess.run(
                    [sys.executable, str(DISCOVERY_SCRIPT), "--single", str(i),
                     "--candidates", "--out", tmp_path],
                    timeout=PER_URL_TIMEOUT,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                _log(f"      TIMEOUT after {PER_URL_TIMEOUT}s")
            elapsed = time.monotonic() - t0
            try:
                result = json.loads(Path(tmp_path).read_text(encoding="utf-8"))
            except Exception:
                result = {"company": entry["company"], "status": "no_json", "candidates": []}
            cands = result.get("candidates") or []
            for c in cands:
                c["_source_company"] = entry["company"]
                c["_source_url"] = entry["url"]
            all_candidates.extend(cands)
            _log(f"      done {elapsed:.0f}s status={result.get('status')} candidates={len(cands)}")
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
    _log(f"  total candidates captured: {len(all_candidates)}")
    return all_candidates


def rank_and_output(db: Session, candidates: list[dict], profile: dict, prefs: dict) -> None:
    """Steps 4-5: rank (cache loop) + filter + write results."""
    _log(f"[4/5] Ranking {len(candidates)} candidates (batched LLM, cached)")
    items = [(f"disc-{i}", c) for i, c in enumerate(candidates)]
    ranker = RelevanceRanker(build_relevance_llm(_settings()), batch_size=_settings().relevance_batch_size)
    service = RecommendationService(ranker)
    recs = service.score_and_cache(
        db, USER_ID, items, profile_summary=profile, preferences=prefs,
    )
    db.commit()
    suitable = RecommendationService.filter_and_sort(recs, top_n=TOP_N, min_score=MIN_SCORE)
    _log(f"  scored={len(recs)} suitable(>={MIN_SCORE})={len(suitable)}")

    out = {
        "user": USER_ID,
        "preferences_version": prefs.get("version"),
        "resume": RESUME_PDF.name,
        "candidate_count": len(candidates),
        "scored_count": len(recs),
        "min_score": MIN_SCORE,
        "suitable": [asdict(r) for r in suitable],
    }
    RESULT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"[5/5] Wrote {len(suitable)} suitable jobs -> {RESULT_JSON.name}")
    _log("\n=== SUITABLE JOBS (top, score desc) ===")
    for r in suitable:
        loc = "/".join(r.locations) if r.locations else "?"
        _log(f"  {r.score:>5}  {r.company_name} / {r.title}  [{loc}]")
        if r.reason:
            _log(f"         {r.reason}")
    _log(f"\nDONE. Full results: {RESULT_JSON}")


def main() -> None:
    _log("=== Personal Application Assistant — end-to-end ===")
    _log(f"DEEPSEEK_API_KEY: {'set' if os.environ.get('DEEPSEEK_API_KEY') else 'MISSING (windows user env fallback)'}")
    _log(f"READGZH_API_KEY: {'set' if os.environ.get('READGZH_API_KEY') else 'MISSING'}")

    profile = parse_resume()

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        prefs = seed_preferences(db)
        candidates = run_discovery()
        if not candidates:
            _log("  !! No candidates captured from any URL. Aborting ranking.")
            return
        rank_and_output(db, candidates, profile, prefs)


if __name__ == "__main__":
    main()
