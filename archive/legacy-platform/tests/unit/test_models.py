from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import AnalysisSession, User, UserRole


def test_user_and_session_are_relational_and_active_session_is_derived() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            account="alice",
            nickname="Alice",
            password_hash="argon",
            role=UserRole.STUDENT,
        )
        db.add(user)
        db.flush()
        first = AnalysisSession(
            user_id=user.id, thread_id="session-1", label="分析会话 1"
        )
        second = AnalysisSession(
            user_id=user.id, thread_id="session-2", label="分析会话 2"
        )
        db.add_all([first, second])
        db.commit()
        rows = db.scalars(
            select(AnalysisSession).where(AnalysisSession.user_id == user.id)
        ).all()
    assert {row.thread_id for row in rows} == {"session-1", "session-2"}
    assert user.role is UserRole.STUDENT
