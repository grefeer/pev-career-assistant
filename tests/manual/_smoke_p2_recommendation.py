"""P2 smoke: RecommendationService rank + cache loop.

1 real DeepSeek call on the first score_and_cache; a second call proves the
cache hit (a sentinel LLM that raises if invoked returns the cached scores
without ever calling the model). Then min_score/top_n filtering.

Run: .venv\\Scripts\\python.exe tests\\manual\\_smoke_p2_recommendation.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_DOTENV = _PROJECT_ROOT / ".env"
if _DOTENV.exists():
    try:
        from dotenv import dotenv_values

        vals = dotenv_values(_DOTENV, interpolate=False)
        for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "READGZH_API_KEY"):
            if key not in os.environ and key in vals and vals[key]:
                os.environ[key] = vals[key]
    except ImportError:
        pass

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.repositories import relevance_scores as relevance_repo
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate
from backend.app.services.relevance import build_relevance_llm
from backend.app.services.relevance.relevance_ranker import RelevanceRanker
from backend.app.services.recommendation_service import RecommendationService


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        personal_mode=True,
    )


class _ExplodingLLM:
    """Sentinel: raises if invoke() is called (proves the cache hit path)."""

    def invoke(self, _messages):  # noqa: ANN001
        raise AssertionError("LLM must not be called on a cache hit")


def _candidates() -> list[NormalizedJobCandidate]:
    return [
        NormalizedJobCandidate(
            title="大模型应用开发工程师(Agent方向)",
            company_name="某AI创业公司",
            locations=["北京"],
            requirements="LangChain/LangGraph Agent 开发，Python，硕士优先",
            recruitment_types=["校招"],
            industries=["人工智能"],
        ),
        NormalizedJobCandidate(
            title="区域销售经理",
            company_name="某传统制造企业",
            locations=["杭州"],
            requirements="区域客户拜访，频繁出差",
            recruitment_types=["社招"],
            industries=["制造业"],
        ),
        NormalizedJobCandidate(
            title="算法工程师-强化学习",
            company_name="元戎启行",
            locations=["深圳"],
            requirements="RL 算法，自动驾驶决策，控制背景加分",
            recruitment_types=["校招"],
            industries=["自动驾驶", "人工智能"],
        ),
    ]


def _profile_summary() -> dict:
    return {
        "name": "高硕谦",
        "education": ["东北大学 控制科学与工程 硕士"],
        "skills": ["Python", "LangChain", "LangGraph", "机器学习"],
        "experience": ["算法实习"],
        "projects": [],
        "raw_excerpt": "",
    }


def _preferences() -> dict:
    return {
        "desired_roles": ["研发工程师", "算法工程师", "Agent/AI应用开发"],
        "target_cities": ["北京", "上海", "广州", "深圳", "成都"],
        "excluded_companies": [],
        "excluded_industries": [],
        "preferred_industries": ["人工智能", "自动驾驶"],
        "preferred_recruitment_types": ["校招"],
        "salary_min": None,
        "salary_max": None,
        "work_mode": None,
        "is_active_search": True,
        "notes": "偏向 agent / AI 应用方向",
        "version": 1,
    }


def main() -> None:
    settings = _settings()
    ranker = RelevanceRanker(build_relevance_llm(settings), batch_size=settings.relevance_batch_size)
    service = RecommendationService(ranker)

    candidates = _candidates()
    profile = _profile_summary()
    prefs = _preferences()

    # --- pure rank ---
    ranked = service.rank(candidates, profile_summary=profile, preferences=prefs)
    assert len(ranked) == 3
    ranked.sort(key=lambda r: r.score, reverse=True)
    assert ranked[0].score >= 60, ranked[0].score
    assert ranked[-1].title == "区域销售经理"
    print(f"  rank() OK: top={ranked[0].score} ({ranked[0].company_name})")

    # --- cache loop ---
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    items = [(f"job-{i}", c) for i, c in enumerate(candidates)]

    with Session(engine) as db:
        recs = service.score_and_cache(
            db, "u1", items, profile_summary=profile, preferences=prefs
        )
        db.commit()
        assert len(recs) == 3, recs
        # cache now fully populated for this key
        unscored = relevance_repo.list_unscored_job_ids(
            db, "u1", [jid for jid, _ in items],
            profile_version_id=None, preferences_version=1,
        )
        assert unscored == [], unscored

        top = max(recs, key=lambda r: r.score)
        print(f"  score_and_cache #1 OK: top={top.score} ({top.company_name})")

        # --- cache hit: swap in an exploding LLM; must NOT be invoked ---
        cache_ranker = RelevanceRanker(_ExplodingLLM(), batch_size=30)
        cache_service = RecommendationService(cache_ranker)
        recs2 = cache_service.score_and_cache(
            db, "u1", items, profile_summary=profile, preferences=prefs
        )
        assert len(recs2) == 3
        # identical scores to the first pass (served from cache)
        by_id_1 = {r.job_id: r.score for r in recs}
        by_id_2 = {r.job_id: r.score for r in recs2}
        assert by_id_1 == by_id_2, (by_id_1, by_id_2)
        print("  cache hit OK: second call returned identical scores without LLM")

        # --- filter + sort ---
        filtered = RecommendationService.filter_and_sort(recs2, top_n=2, min_score=60.0)
        assert len(filtered) == 2, filtered
        assert filtered[0].score >= filtered[1].score
        assert all(r.score >= 60.0 for r in filtered)
        print(f"  filter_and_sort OK: {len(filtered)} recs >= 60, top={filtered[0].score}")

    print("\nP2 SMOKE OK: rank + score_and_cache (cache hit) + filter verified")


if __name__ == "__main__":
    main()
