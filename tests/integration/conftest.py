"""Fixtures for integration tests using SQLite in-memory database."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import User, UserRole


@pytest.fixture
def db_session():
    """Provide a clean SQLite in-memory session with all tables created."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def test_user(db_session):
    """Provide a User persisted in the in-memory database."""
    user = User(
        id=str(uuid.uuid4()),
        account="integration-tester",
        nickname="Integration Tester",
        password_hash="argon2-placeholder",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    return user
