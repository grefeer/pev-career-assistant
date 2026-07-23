"""P0 smoke: personal-memory data layer (preferences + relevance scores).

In-memory sqlite via Base.metadata.create_all; exercises repository CRUD
without needing live services or parent rows (sqlite FK enforcement off).
Run: .venv\\Scripts\\python.exe tests\\manual\\_smoke_p0_personal_memory.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.repositories import preferences as preferences_repo
from backend.app.repositories import relevance_scores as relevance_repo


def main() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        # --- preferences: insert then update bumps version ---
        pref = preferences_repo.upsert(
            db,
            user_id="u1",
            desired_roles=["研发工程师", "算法工程师"],
            target_cities=["北京", "上海"],
            preferred_industries=["人工智能"],
        )
        db.commit()
        assert pref.version == 1, f"expected v1, got {pref.version}"

        pref = preferences_repo.upsert(db, user_id="u1", desired_roles=["Agent/AI应用开发"])
        db.commit()
        assert pref.version == 2, f"expected v2, got {pref.version}"
        assert "Agent/AI应用开发" in pref.desired_roles
        assert "上海" in pref.target_cities  # untouched field preserved

        summary = preferences_repo.to_summary(preferences_repo.get_for_user(db, "u1"))
        assert summary["version"] == 2
        assert "北京" in summary["target_cities"]
        assert preferences_repo.get_version(db, "u1") == 2
        assert preferences_repo.get_version(db, "missing") == 0

        # --- relevance scores: insert, refresh, top-N, unscored ---
        row = relevance_repo.upsert(
            db,
            user_id="u1",
            job_id="j1",
            profile_version_id=None,
            preferences_version=2,
            score=88.0,
            reason="研发+北京",
            matched_signals=["研发岗位", "北京"],
        )
        db.commit()
        assert row.score == 88.0

        # Refresh same key -> updates in place, no duplicate.
        row = relevance_repo.upsert(
            db,
            user_id="u1",
            job_id="j1",
            profile_version_id=None,
            preferences_version=2,
            score=91.0,
            reason="研发+北京+AI方向",
        )
        db.commit()
        assert row.score == 91.0

        relevance_repo.upsert(
            db, user_id="u1", job_id="j2", profile_version_id=None,
            preferences_version=2, score=72.0,
        )
        db.commit()

        top = relevance_repo.list_top_for_user(
            db, "u1", profile_version_id=None, preferences_version=2
        )
        assert [r.job_id for r in top] == ["j1", "j2"], top
        assert top[0].score == 91.0

        unscored = relevance_repo.list_unscored_job_ids(
            db, "u1", ["j1", "j2", "j3"],
            profile_version_id=None, preferences_version=2,
        )
        assert unscored == ["j3"], unscored

        # New preferences_version -> old scores are invisible (cache key differs).
        unscored_new = relevance_repo.list_unscored_job_ids(
            db, "u1", ["j1", "j2"],
            profile_version_id=None, preferences_version=3,
        )
        assert unscored_new == ["j1", "j2"], unscored_new

    print("P0 SMOKE OK: preferences + relevance_scores CRUD verified")


if __name__ == "__main__":
    main()
