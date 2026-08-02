"""Unit tests for the ApplicationTrackingService.

Covers the create / transition (state-machine + optimistic-lock) / read /
update paths and every service error code.  The DB is the shared in-memory
``db_session`` fixture (FK ON); only ``User`` rows are seeded.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from backend.app.db.models import User
from backend.app.domain.application_tracking import ApplicationStatus
from backend.app.services.application_tracking.service import (
    ApplicationInputError,
    ApplicationNotFoundError,
    ApplicationTrackingService,
)
from tests.conftest import settings_override


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


@pytest.fixture()
def service() -> ApplicationTrackingService:
    return ApplicationTrackingService(settings_override(application_tracking_enabled=True))


def test_create_application(service: ApplicationTrackingService, db_session: Session) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session,
        user=user,
        company_name="Acme",
        title="Backend Engineer",
        apply_url="https://acme.example",
        source="manual",
        notes="referral",
    )
    assert record.status == ApplicationStatus.saved
    assert record.state_version == 0
    assert record.company_name == "Acme"
    assert record.applied_at is None


# ----------------------------------------------------------------- transitions


def test_transition_happy_path_stamps_applied(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    result = service.transition(
        db_session, user=user, application_id=record.id, to_status=ApplicationStatus.applied
    )
    assert result.status == ApplicationStatus.applied
    assert result.state_version == 1
    assert result.applied_at is not None


def test_transition_full_pipeline(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    for nxt in (
        ApplicationStatus.applied,
        ApplicationStatus.screening,
        ApplicationStatus.interview,
        ApplicationStatus.offer,
    ):
        service.transition(
            db_session, user=user, application_id=record.id, to_status=nxt
        )
    db_session.refresh(record)
    assert record.status == ApplicationStatus.offer
    assert record.state_version == 4


def test_transition_records_note_in_event(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    service.transition(
        db_session,
        user=user,
        application_id=record.id,
        to_status=ApplicationStatus.applied,
        note="submitted via portal",
    )
    events = service.list_events(db_session, user=user, application_id=record.id)
    assert len(events) == 1
    assert events[0].from_status == "saved"
    assert events[0].to_status == "applied"
    assert events[0].note == "submitted via portal"


def test_transition_invalid_raises(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    with pytest.raises(ApplicationInputError) as exc:
        service.transition(
            db_session,
            user=user,
            application_id=record.id,
            to_status=ApplicationStatus.offer,
        )
    assert exc.value.code == "invalid_transition"


def test_transition_already_terminal_raises(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    service.transition(
        db_session,
        user=user,
        application_id=record.id,
        to_status=ApplicationStatus.withdrawn,
    )
    with pytest.raises(ApplicationInputError) as exc:
        service.transition(
            db_session,
            user=user,
            application_id=record.id,
            to_status=ApplicationStatus.applied,
        )
    assert exc.value.code == "already_terminal"


def test_transition_rejected_is_terminal(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    service.transition(
        db_session, user=user, application_id=record.id, to_status=ApplicationStatus.applied
    )
    service.transition(
        db_session, user=user, application_id=record.id, to_status=ApplicationStatus.rejected
    )
    with pytest.raises(ApplicationInputError) as exc:
        service.transition(
            db_session,
            user=user,
            application_id=record.id,
            to_status=ApplicationStatus.interview,
        )
    assert exc.value.code == "already_terminal"


def test_transition_stale_version_raises(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    with pytest.raises(ApplicationInputError) as exc:
        service.transition(
            db_session,
            user=user,
            application_id=record.id,
            to_status=ApplicationStatus.applied,
            expected_version=99,
        )
    assert exc.value.code == "stale_version"


def test_transition_expected_version_match_succeeds(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    service.transition(
        db_session,
        user=user,
        application_id=record.id,
        to_status=ApplicationStatus.applied,
        expected_version=0,
    )
    db_session.refresh(record)
    assert record.state_version == 1


def test_transition_cross_user_not_found(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session, "alice")
    other = _user(db_session, "bob")
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    with pytest.raises(ApplicationNotFoundError):
        service.transition(
            db_session,
            user=other,
            application_id=record.id,
            to_status=ApplicationStatus.applied,
        )


def test_transition_missing_record_not_found(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    with pytest.raises(ApplicationNotFoundError):
        service.transition(
            db_session,
            user=user,
            application_id=str(uuid.uuid4()),
            to_status=ApplicationStatus.applied,
        )


# ----------------------------------------------------------------------- reads


def test_get_application_found(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    fetched = service.get_application(
        db_session, user=user, application_id=record.id
    )
    assert fetched.id == record.id


def test_get_application_cross_user_not_found(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session, "alice")
    other = _user(db_session, "bob")
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    with pytest.raises(ApplicationNotFoundError):
        service.get_application(db_session, user=other, application_id=record.id)


def test_list_applications_pagination_and_total(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    for i in range(3):
        service.create_application(
            db_session, user=user, company_name=f"C{i}", title="T"
        )
    items, total = service.list_applications(
        db_session, user=user, limit=2, offset=0
    )
    assert len(items) == 2
    assert total == 3
    items2, total2 = service.list_applications(
        db_session, user=user, limit=2, offset=2
    )
    assert len(items2) == 1
    assert total2 == 3


def test_list_applications_status_filter(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    a = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    b = service.create_application(
        db_session, user=user, company_name="B", title="T"
    )
    service.transition(
        db_session, user=user, application_id=b.id, to_status=ApplicationStatus.applied
    )
    items, total = service.list_applications(
        db_session, user=user, status=ApplicationStatus.applied
    )
    assert {i.id for i in items} == {b.id}
    assert total == 1
    # saved count unaffected
    saved_items, saved_total = service.list_applications(
        db_session, user=user, status=ApplicationStatus.saved
    )
    assert {i.id for i in saved_items} == {a.id}
    assert saved_total == 1


def test_list_events_owner_scoped(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session, "alice")
    other = _user(db_session, "bob")
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    service.transition(
        db_session, user=user, application_id=record.id, to_status=ApplicationStatus.applied
    )
    events = service.list_events(db_session, user=user, application_id=record.id)
    assert len(events) == 1
    with pytest.raises(ApplicationNotFoundError):
        service.list_events(db_session, user=other, application_id=record.id)


def test_list_events_missing_record_not_found(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    with pytest.raises(ApplicationNotFoundError):
        service.list_events(
            db_session, user=user, application_id=str(uuid.uuid4())
        )


# --------------------------------------------------------------------- update


def test_update_notes(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    updated = service.update_application(
        db_session, user=user, application_id=record.id, notes="new note"
    )
    assert updated.notes == "new note"


def test_update_apply_url_clears_with_none(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session,
        user=user,
        company_name="A",
        title="T",
        apply_url="https://orig.example",
    )
    updated = service.update_application(
        db_session, user=user, application_id=record.id, apply_url=None
    )
    assert updated.apply_url is None


def test_update_both_fields(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    updated = service.update_application(
        db_session,
        user=user,
        application_id=record.id,
        notes="n",
        apply_url="https://x.example",
    )
    assert updated.notes == "n"
    assert updated.apply_url == "https://x.example"


def test_update_no_fields_raises(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session)
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    with pytest.raises(ApplicationInputError) as exc:
        service.update_application(
            db_session, user=user, application_id=record.id
        )
    assert exc.value.code == "no_fields"


def test_update_cross_user_not_found(
    service: ApplicationTrackingService, db_session: Session
) -> None:
    user = _user(db_session, "alice")
    other = _user(db_session, "bob")
    record = service.create_application(
        db_session, user=user, company_name="A", title="T"
    )
    with pytest.raises(ApplicationNotFoundError):
        service.update_application(
            db_session, user=other, application_id=record.id, notes="x"
        )
