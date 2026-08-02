from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import AnalysisSession, User
from backend.app.repositories.sessions import (
    activate,
    create_for_user,
    get_owned,
    list_for_user,
)


def make_db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def add_user(db: Session, account: str) -> User:
    user = User(account=account, nickname=account, password_hash="unused")
    db.add(user)
    db.flush()
    return user


def test_create_for_user_generates_unique_thread_and_sequential_label() -> None:
    with make_db() as db:
        user = add_user(db, "alice")
        first = create_for_user(db, user.id)
        second = create_for_user(db, user.id)

        assert first.thread_id.startswith("internship-session-")
        assert second.thread_id != first.thread_id
        assert [first.label, second.label] == ["分析会话 1", "分析会话 2"]


def test_get_owned_never_returns_another_users_session() -> None:
    with make_db() as db:
        alice = add_user(db, "alice")
        bob = add_user(db, "bob")
        bob_session = create_for_user(db, bob.id)

        assert get_owned(db, alice.id, bob_session.thread_id) is None
        assert get_owned(db, bob.id, bob_session.thread_id) is bob_session


def test_list_for_user_orders_most_recent_activation_first() -> None:
    with make_db() as db:
        user = add_user(db, "alice")
        older = AnalysisSession(
            user_id=user.id,
            thread_id="older",
            label="分析会话 1",
            activated_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        newer = AnalysisSession(
            user_id=user.id,
            thread_id="newer",
            label="分析会话 2",
            activated_at=datetime.now(timezone.utc),
        )
        db.add_all([older, newer])
        db.flush()

        assert [item.thread_id for item in list_for_user(db, user.id)] == [
            "newer",
            "older",
        ]

        activate(db, older)

        assert list_for_user(db, user.id)[0] is older
