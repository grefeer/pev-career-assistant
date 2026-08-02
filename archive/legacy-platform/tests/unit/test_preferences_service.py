"""Tests for extended user preferences (Task 3)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from backend.app.db.models import User, UserRole
from backend.app.services.preferences_service import (
    get_preferences_summary,
    set_preferences,
)


def _user(db: Session, account: str = "u1") -> User:
    u = User(
        id=account,
        account=account,
        nickname=account,
        password_hash="x",
        role=UserRole.STUDENT,
    )
    db.add(u)
    db.flush()
    return u


def test_extended_preferences_normalize_and_bump_version(db_session: Session) -> None:
    user = _user(db_session)
    first = set_preferences(db_session, user.id, desired_roles=["AI应用开发"])
    first_version = first.version  # snapshot int: the same ORM object is mutated below
    second = set_preferences(
        db_session,
        user.id,
        role_synonyms=["Agent开发", "agent开发"],
        excluded_roles=["销售"],
        personalized_discovery_min_score=72,
    )
    assert second.version == first_version + 1
    summary = get_preferences_summary(db_session, user.id)
    assert summary["role_synonyms"] == ["Agent开发"]
    assert summary["excluded_roles"] == ["销售"]
    assert summary["personalized_discovery_min_score"] == 72.0


def test_desired_roles_are_normalized(db_session: Session) -> None:
    user = _user(db_session)
    set_preferences(
        db_session,
        user.id,
        desired_roles=[" AI应用开发 ", "ai应用开发", "Agent开发"],
    )
    summary = get_preferences_summary(db_session, user.id)
    assert summary["desired_roles"] == ["AI应用开发", "Agent开发"]


def test_blank_role_term_rejected(db_session: Session) -> None:
    user = _user(db_session)
    with pytest.raises(ValueError, match="blank"):
        set_preferences(db_session, user.id, desired_roles=[" "])


@pytest.mark.parametrize("score", [-1, 100.1, -0.01, 100.01])
def test_score_threshold_is_bounded(db_session: Session, score: float) -> None:
    user = _user(db_session)
    with pytest.raises(ValueError, match="0.*100"):
        set_preferences(db_session, user.id, personalized_discovery_min_score=score)


def test_score_threshold_zero_and_one_hundred_allowed(db_session: Session) -> None:
    user = _user(db_session)
    set_preferences(db_session, user.id, personalized_discovery_min_score=0)
    set_preferences(db_session, user.id, personalized_discovery_min_score=100)
    assert get_preferences_summary(db_session, user.id)["personalized_discovery_min_score"] == 100


def test_summary_defaults_when_no_row(db_session: Session) -> None:
    summary = get_preferences_summary(db_session, "nobody")
    assert summary["role_synonyms"] == []
    assert summary["excluded_roles"] == []
    assert summary["personalized_discovery_min_score"] is None
