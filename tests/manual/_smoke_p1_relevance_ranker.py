"""P1 smoke: RelevanceRanker batched LLM scoring.

One real DeepSeek call on 3 synthetic candidates against the user's stated
preferences (研发 / agent·AI / 北上广深成). Validates the LLM integration +
JSON parsing + ordering. Asserts the Agent-role job scores highest and the
off-target sales job scores lowest.

Run: .venv\\Scripts\\python.exe tests\\manual\\_smoke_p1_relevance_ranker.py
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

from backend.app.config import Settings
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate
from backend.app.services.relevance import build_relevance_llm
from backend.app.services.relevance.relevance_ranker import RelevanceRanker


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        personal_mode=True,
    )


def main() -> None:
    settings = _settings()
    llm = build_relevance_llm(settings)

    candidates = [
        NormalizedJobCandidate(
            title="大模型应用开发工程师(Agent方向)",
            company_name="某AI创业公司",
            locations=["北京"],
            requirements="熟悉 LangChain / LangGraph，有 Agent 开发经验，熟练 Python，硕士优先",
            recruitment_types=["校招"],
            industries=["人工智能"],
        ),
        NormalizedJobCandidate(
            title="区域销售经理",
            company_name="某传统制造企业",
            locations=["杭州"],
            requirements="负责区域客户拜访与销售目标，需频繁出差",
            recruitment_types=["社招"],
            industries=["制造业"],
        ),
        NormalizedJobCandidate(
            title="强化学习算法工程师",
            company_name="元戎启行",
            locations=["深圳"],
            requirements="RL 算法研究与落地，自动驾驶决策规划，控制背景加分",
            recruitment_types=["校招"],
            industries=["自动驾驶", "人工智能"],
        ),
    ]

    profile_summary = {
        "name": "高硕谦",
        "education": ["东北大学 控制科学与工程 硕士"],
        "skills": ["Python", "LangChain", "LangGraph", "机器学习", "控制理论"],
        "experience": ["算法相关实习经历"],
        "projects": [],
        "raw_excerpt": "",
    }
    preferences = {
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

    ranker = RelevanceRanker(llm, batch_size=settings.relevance_batch_size)
    ranked = ranker.rank(candidates, profile_summary=profile_summary, preferences=preferences)

    assert len(ranked) == 3, f"expected 3 results, got {len(ranked)}"
    ordered = sorted(ranked, key=lambda r: r.score, reverse=True)
    for r in ordered:
        print(f"  {r.score:>5}  {r.company_name} / {r.title}  [{', '.join(r.locations)}]")
        print(f"         reason: {r.reason}")
        print(f"         signals: {r.matched_signals}")

    top = ordered[0]
    bottom = ordered[-1]
    assert top.title.startswith("大模型应用开发") or top.title.startswith("强化学习"), top
    assert bottom.title == "区域销售经理", bottom
    assert top.score >= 60, f"top score too low: {top.score}"
    assert bottom.score < top.score, (top.score, bottom.score)

    print(f"\nP1 SMOKE OK: ranker returned 3 scores, top={top.score} ({top.company_name})")


if __name__ == "__main__":
    main()
