"""Pytest fixtures for Match API tests."""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from httpx import ASGITransport

from backend.app.api.router import api_router
from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import ApplicationTaskStatus, User, UserRole
from backend.app.services.auth import AuthService


def pytest_configure(config):
    config.addinivalue_line("markers", "api: marks tests as API-level integration tests")


@pytest.fixture(scope="session")
def settings():
    return Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        checkpoint_backend="sqlite",
    )


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db_session(engine):
    session = Session(engine)
    yield session
    session.close()


@pytest.fixture
def test_user(db_session):
    user = User(
        id=str(uuid.uuid4()),
        account="test-user",
        nickname="Test User",
        password_hash="argon2-placeholder",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def other_user(db_session):
    user = User(
        id=str(uuid.uuid4()),
        account="other-user",
        nickname="Other User",
        password_hash="argon2-placeholder",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _make_fake_draft(draft_id: str = "mock-draft-id"):
    """Build a MagicMock that quacks like a ResumeDraft for _to_draft_response."""
    draft = MagicMock()
    draft.id = draft_id
    draft.match_report_id = "mock-match-report-id"
    draft.target_job_id = "mock-job-id"
    draft.diffs = [
        {
            "op": "rephrase",
            "section": "skills",
            "before": "Python, Java",
            "after": "Python (advanced), Java",
            "fact_ref": "abc123",
            "evidence_ids": ["ev1", "ev2"],
        },
    ]
    draft.status = "draft"
    draft.error_code = None
    draft.state_version = 1
    draft.created_at = datetime.now(timezone.utc)
    draft.approved_at = None
    return draft


def _make_fake_arv(arv_id: str = "mock-arv-id"):
    """Build a MagicMock that quacks like an ApprovedResumeVersion."""
    arv = MagicMock()
    arv.id = arv_id
    arv.draft_id = "mock-draft-id"
    arv.approved_at = datetime.now(timezone.utc)
    return arv


def _make_fake_report(match_id: str = "mock-match-id"):
    """Build a MagicMock that quacks like a MatchReport for _to_response."""
    report = MagicMock()
    report.id = match_id
    report.analysis_session_id = "mock-session-id"
    report.job_id = "job-001"
    report.profile_version_id = "pv-001"
    report.status = "completed"
    report.score = 85
    report.score_components = None
    report.strengths = None
    report.gaps = None
    report.unknowns = None
    report.risks = None
    report.application_priority = None
    report.recommendation = None
    report.error_code = None
    report.scoring_rule_version = "1.0"
    report.model_version = "1.0"
    report.prompt_version = "1.0"
    report.output_schema_version = "1.0"
    now = datetime.now(timezone.utc)
    report.created_at = now
    report.started_at = None
    report.completed_at = None
    return report


def _make_fake_snapshot(snapshot_id: str = "mock-snapshot-id"):
    """Build a MagicMock that quacks like an ApplicationSnapshot."""
    snapshot = MagicMock()
    snapshot.id = snapshot_id
    snapshot.job_id = "job-001"
    snapshot.approved_resume_version_id = "arv-001"
    snapshot.profile_version_id = "pv-001"
    snapshot.job_snapshot = {
        "company_name": "Test Corp",
        "title": "Software Engineer",
        "description_text": "Job description",
        "locations": ["Shanghai"],
        "recruitment_types": ["campus"],
        "industries": ["tech"],
        "apply_url": "https://example.com/apply",
    }
    snapshot.profile_facts = {"name": "Test User", "skills": ["Python"]}
    snapshot.gui_eligible = True
    snapshot.job_status_at_snapshot = "verified"
    snapshot.job_review_version_at_snapshot = 2
    snapshot.created_at = datetime.now(timezone.utc)
    snapshot.schema_version = "1.0"
    return snapshot


def _make_fake_task(task_id: str = "mock-task-id"):
    """Build a MagicMock that quacks like an ApplicationTask."""
    task = MagicMock()
    task.id = task_id
    task.user_id = "user-001"
    task.snapshot_id = "mock-snapshot-id"
    task.target_job_id = "job-001"
    task.device_id = None
    task.status = ApplicationTaskStatus.CREATED
    task.state_version = 0
    task.task_kind = "application"
    return task


@pytest.fixture
def mock_match_service():
    service = MagicMock()
    service.repo = MagicMock()
    service.create_match.return_value = _make_fake_report()
    service.repo.get_by_id.return_value = _make_fake_report()
    service.repo.list_by_session.return_value = [_make_fake_report()]
    return service


@pytest.fixture
def mock_draft_service():
    service = MagicMock()
    service.repo = MagicMock()
    service.create_draft.return_value = _make_fake_draft()
    service.repo.get_by_id.return_value = _make_fake_draft()
    service.repo.list_by_user.return_value = [_make_fake_draft()]
    service.approve_draft.return_value = _make_fake_arv()
    service.reject_draft.return_value = _make_fake_draft()
    return service


@pytest.fixture
def app(settings, session_factory, test_user, mock_match_service, mock_draft_service):
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.redis = MagicMock()
    app.state.match_service = mock_match_service
    app.state.draft_service = mock_draft_service
    app.state.object_store = MagicMock()
    app.include_router(api_router, prefix="/api")
    return app


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def auth_headers(settings, test_user):
    auth_service = AuthService(settings)
    token = auth_service.issue_user_token(test_user)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def other_user_headers(settings, other_user):
    auth_service = AuthService(settings)
    token = auth_service.issue_user_token(other_user)
    return {"Authorization": f"Bearer {token}"}
