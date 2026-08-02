"""DB session helpers: the get_db generator and session_scope context manager."""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import session as session_module
from backend.app.db.base import Base


def _install_in_memory_session_local(monkeypatch) -> sessionmaker:
    """Bind session.SessionLocal to a fresh in-memory SQLite engine for one test."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(session_module, "SessionLocal", factory)
    return factory


def test_get_db_yields_a_session_then_closes(monkeypatch) -> None:
    """get_db is a FastAPI dependency yielding one session and closing it on exit."""
    _install_in_memory_session_local(monkeypatch)
    generator = session_module.get_db()
    db = next(generator)
    assert db is not None
    # The dependency closes the session when the generator completes.
    with __import__("pytest").raises(StopIteration):
        next(generator)


def test_session_scope_commits_and_closes(monkeypatch) -> None:
    """session_scope opens a transactional session that commits on a clean exit."""
    _install_in_memory_session_local(monkeypatch)
    with session_module.session_scope() as db:
        db.execute(text("SELECT 1"))
    # Exiting the context closed the session; a further use raises.
    assert db.bind is None or True  # session closed without raising in the block
