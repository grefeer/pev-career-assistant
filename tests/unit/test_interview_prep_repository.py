"""Unit tests for the interview-prep repository (data access only)."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from backend.app.db.models import User, UserRole
from backend.app.domain.interview_prep import InterviewPrepKitStatus
from backend.app.repositories import interview_prep as repository

_CONTENT = {"technical_questions": ["q1"]}


def _make_user(db: Session, account: str = "alice") -> User:
    user = User(
        account=account,
        nickname=account,
        password_hash="x",
        role=UserRole.STUDENT,
    )
    db.add(user)
    db.flush()
    return user


def _create(db: Session, user_id: str, *, agent_version: str = "1.0.0") -> object:
    return repository.create_kit(
        db,
        user_id=user_id,
        job_snapshot={"title": "Engineer"},
        agent_version=agent_version,
    )


def test_create_kit_defaults_to_generating(db_session: Session) -> None:
    user = _make_user(db_session)
    kit = _create(db_session, user.id)
    assert kit.status == InterviewPrepKitStatus.generating
    assert kit.agent_version == "1.0.0"
    assert kit.job_snapshot == {"title": "Engineer"}
    assert kit.target_job_id is None
    assert kit.profile_version_id is None
    assert kit.content_json is None
    assert kit.preferences_summary_json is None
    assert kit.match_analysis_json is None
    assert kit.error_code is None
    assert kit.last_error is None
    assert kit.started_at is not None
    assert kit.finished_at is None


def test_create_kit_stores_optional_context(db_session: Session) -> None:
    user = _make_user(db_session)
    # FK-bearing columns (target_job_id / profile_version_id) are omitted here
    # because the db_session fixture enforces FKs and no real job/profile rows
    # exist; this test is about the JSON context columns, exercised below in
    # the service tests against a full seed.
    kit = repository.create_kit(
        db_session,
        user_id=user.id,
        job_snapshot={"title": "Engineer"},
        agent_version="1.0.0",
        preferences_summary_json={"desired_roles": ["Backend"]},
        match_analysis_json={"strengths": [{"area": "Python"}]},
    )
    assert kit.target_job_id is None
    assert kit.profile_version_id is None
    assert kit.preferences_summary_json == {"desired_roles": ["Backend"]}
    assert kit.match_analysis_json == {"strengths": [{"area": "Python"}]}


def test_get_kit_and_owner_scoping(db_session: Session) -> None:
    user = _make_user(db_session)
    other = _make_user(db_session, "bob")
    kit = _create(db_session, user.id)
    assert repository.get_kit(db_session, kit.id).id == kit.id
    assert (
        repository.get_kit_for_owner(db_session, kit.id, user.id).id == kit.id
    )
    assert repository.get_kit_for_owner(db_session, kit.id, other.id) is None
    assert repository.get_kit(db_session, "missing") is None


def test_list_kits_pages_newest_first(db_session: Session) -> None:
    user = _make_user(db_session)
    first = _create(db_session, user.id)
    second = _create(db_session, user.id)
    rows = repository.list_kits(db_session, user.id, limit=10)
    assert [r.id for r in rows] == [second.id, first.id]


def test_list_kits_respects_limit_offset(db_session: Session) -> None:
    user = _make_user(db_session)
    for _ in range(3):
        _create(db_session, user.id)
    page = repository.list_kits(db_session, user.id, limit=2, offset=1)
    assert len(page) == 2


def test_complete_kit_ready_writes_content_and_finished_at(db_session: Session) -> None:
    user = _make_user(db_session)
    kit = _create(db_session, user.id)
    updated = repository.complete_kit(
        db_session,
        kit.id,
        status=InterviewPrepKitStatus.ready,
        content_json=_CONTENT,
    )
    assert updated is not None
    assert updated.status == InterviewPrepKitStatus.ready
    assert updated.content_json == _CONTENT
    assert updated.error_code is None
    assert updated.finished_at is not None


def test_complete_kit_failed_writes_error(db_session: Session) -> None:
    user = _make_user(db_session)
    kit = _create(db_session, user.id)
    updated = repository.complete_kit(
        db_session,
        kit.id,
        status=InterviewPrepKitStatus.failed,
        error_code="interview_prep_parse_error",
        last_error="bad json",
    )
    assert updated is not None
    assert updated.status == InterviewPrepKitStatus.failed
    assert updated.error_code == "interview_prep_parse_error"
    assert updated.last_error == "bad json"
    # content_json was None at creation and not overwritten.
    assert updated.content_json is None


def test_complete_kit_missing_returns_none(db_session: Session) -> None:
    result = repository.complete_kit(
        db_session,
        str(uuid.uuid4()),
        status=InterviewPrepKitStatus.failed,
        error_code="x",
    )
    assert result is None
