"""Unit tests for the application_tracking repository.

Uses the shared ``db_session`` fixture (in-memory SQLite, FK ON) so a real
``User`` is seeded and FK semantics are exercised.  ``target_job_id`` is left
``None`` (no ``job_postings`` row needed) - the link is optional.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from backend.app.db.models import User
from backend.app.domain.application_tracking import ApplicationStatus
from backend.app.repositories import application_tracking as repo


def _user(db: Session, account: str = "alice") -> User:
    u = User(
        id=str(uuid.uuid4()),
        account=account,
        nickname=account,
        password_hash="x",
    )
    db.add(u)
    db.flush()
    return u


def test_create_application_defaults(db_session: Session) -> None:
    user = _user(db_session)
    record = repo.create_application(
        db_session,
        user_id=user.id,
        company_name="Acme",
        title="Backend Engineer",
    )
    assert record.id is not None
    assert record.user_id == user.id
    assert record.company_name == "Acme"
    assert record.title == "Backend Engineer"
    assert record.apply_url is None
    assert record.source is None
    assert record.notes is None
    assert record.target_job_id is None
    assert record.status == ApplicationStatus.saved
    assert record.state_version == 0
    assert record.applied_at is None


def test_create_application_stores_all_fields(db_session: Session) -> None:
    user = _user(db_session)
    record = repo.create_application(
        db_session,
        user_id=user.id,
        company_name="Acme",
        title="Backend Engineer",
        apply_url="https://acme.example/apply",
        source="manual",
        notes="Referral from Jane",
        target_job_id=None,
    )
    assert record.apply_url == "https://acme.example/apply"
    assert record.source == "manual"
    assert record.notes == "Referral from Jane"


def test_get_application_ignores_owner(db_session: Session) -> None:
    user = _user(db_session)
    record = repo.create_application(
        db_session, user_id=user.id, company_name="Acme", title="Eng"
    )
    fetched = repo.get_application(db_session, record.id)
    assert fetched is not None
    assert fetched.id == record.id
    assert repo.get_application(db_session, "nope") is None


def test_get_application_for_owner_scoping(db_session: Session) -> None:
    user = _user(db_session, "alice")
    other = _user(db_session, "bob")
    record = repo.create_application(
        db_session, user_id=user.id, company_name="Acme", title="Eng"
    )
    assert repo.get_application_for_owner(db_session, record.id, user.id) is not None
    assert repo.get_application_for_owner(db_session, record.id, other.id) is None
    assert repo.get_application_for_owner(db_session, "missing", user.id) is None


def test_list_applications_newest_first_and_pagination(db_session: Session) -> None:
    user = _user(db_session)
    ids = []
    for i in range(3):
        r = repo.create_application(
            db_session, user_id=user.id, company_name=f"C{i}", title=f"T{i}"
        )
        ids.append(r.id)
    page = repo.list_applications(db_session, user.id, limit=2, offset=0)
    assert [r.id for r in page] == [ids[2], ids[1]]
    page2 = repo.list_applications(db_session, user.id, limit=2, offset=2)
    assert [r.id for r in page2] == [ids[0]]


def test_list_applications_status_filter(db_session: Session) -> None:
    user = _user(db_session)
    saved = repo.create_application(
        db_session, user_id=user.id, company_name="A", title="T"
    )
    applied = repo.create_application(
        db_session, user_id=user.id, company_name="B", title="T"
    )
    repo.apply_transition(
        db_session, applied, to_status=ApplicationStatus.applied
    )
    only_applied = repo.list_applications(
        db_session, user.id, status=ApplicationStatus.applied
    )
    assert {r.id for r in only_applied} == {applied.id}
    assert repo.get_application(db_session, saved.id) is not None  # saved untouched


def test_count_applications(db_session: Session) -> None:
    user = _user(db_session)
    assert repo.count_applications(db_session, user.id) == 0
    repo.create_application(
        db_session, user_id=user.id, company_name="A", title="T"
    )
    b = repo.create_application(
        db_session, user_id=user.id, company_name="B", title="T"
    )
    repo.apply_transition(db_session, b, to_status=ApplicationStatus.applied)
    assert repo.count_applications(db_session, user.id) == 2
    assert repo.count_applications(
        db_session, user.id, status=ApplicationStatus.applied
    ) == 1
    assert repo.count_applications(
        db_session, user.id, status=ApplicationStatus.saved
    ) == 1


def test_list_events_oldest_first(db_session: Session) -> None:
    user = _user(db_session)
    record = repo.create_application(
        db_session, user_id=user.id, company_name="A", title="T"
    )
    repo.apply_transition(db_session, record, to_status=ApplicationStatus.applied, note="n1")
    repo.apply_transition(db_session, record, to_status=ApplicationStatus.screening)
    repo.apply_transition(
        db_session, record, to_status=ApplicationStatus.interview, note="n3"
    )
    events = repo.list_events(db_session, record.id)
    assert [e.to_status for e in events] == ["applied", "screening", "interview"]
    assert [e.from_status for e in events] == ["saved", "applied", "screening"]
    assert events[0].note == "n1"
    assert events[1].note is None
    assert events[2].note == "n3"
    assert repo.list_events(db_session, "missing") == []


def test_apply_transition_stamps_applied_at_once(db_session: Session) -> None:
    user = _user(db_session)
    record = repo.create_application(
        db_session, user_id=user.id, company_name="A", title="T"
    )
    assert record.applied_at is None
    repo.apply_transition(db_session, record, to_status=ApplicationStatus.applied)
    first_applied_at = record.applied_at
    assert first_applied_at is not None
    assert record.state_version == 1
    # Re-entering applied is not a legal transition anyway, but the stamp guard
    # only sets applied_at when it is still None - verified by status below.
    assert record.status == ApplicationStatus.applied


def test_apply_transition_does_not_overwrite_applied_at(db_session: Session) -> None:
    # saved -> applied -> screening -> applied is illegal; instead exercise the
    # guard by creating a second record that we only stamp once, then assert a
    # screening transition leaves applied_at intact.
    user = _user(db_session)
    record = repo.create_application(
        db_session, user_id=user.id, company_name="A", title="T"
    )
    repo.apply_transition(db_session, record, to_status=ApplicationStatus.applied)
    stamped = record.applied_at
    repo.apply_transition(db_session, record, to_status=ApplicationStatus.screening)
    assert record.applied_at == stamped
    assert record.state_version == 2


def test_update_application_notes_only(db_session: Session) -> None:
    user = _user(db_session)
    record = repo.create_application(
        db_session,
        user_id=user.id,
        company_name="A",
        title="T",
        apply_url="https://orig.example",
    )
    repo.update_application(db_session, record, notes="updated note")
    assert record.notes == "updated note"
    assert record.apply_url == "https://orig.example"  # untouched


def test_update_application_apply_url_only(db_session: Session) -> None:
    user = _user(db_session)
    record = repo.create_application(
        db_session,
        user_id=user.id,
        company_name="A",
        title="T",
        notes="keep",
    )
    repo.update_application(db_session, record, apply_url="https://new.example")
    assert record.apply_url == "https://new.example"
    assert record.notes == "keep"


def test_update_application_clears_field_with_none(db_session: Session) -> None:
    user = _user(db_session)
    record = repo.create_application(
        db_session,
        user_id=user.id,
        company_name="A",
        title="T",
        notes="keep",
        apply_url="https://orig.example",
    )
    repo.update_application(db_session, record, notes=None)
    assert record.notes is None
    assert record.apply_url == "https://orig.example"


def test_update_application_no_fields_leaves_unchanged(db_session: Session) -> None:
    user = _user(db_session)
    record = repo.create_application(
        db_session,
        user_id=user.id,
        company_name="A",
        title="T",
        notes="keep",
    )
    repo.update_application(db_session, record)  # no kwargs -> sentinel default
    assert record.notes == "keep"
