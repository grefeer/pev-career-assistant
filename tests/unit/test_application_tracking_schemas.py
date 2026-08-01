"""Unit tests for the application-tracking Pydantic schemas."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.app.api.application_tracking_schemas import (
    ApplicationEventListResponse,
    ApplicationEventResponse,
    ApplicationListResponse,
    ApplicationRecordResponse,
    CreateApplicationRequest,
    TransitionRequest,
    UpdateApplicationRequest,
)
from backend.app.domain.application_tracking import ApplicationStatus

_NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------- create request


def test_create_request_strips_required() -> None:
    req = CreateApplicationRequest(company_name="  Acme  ", title="  Eng  ")
    assert req.company_name == "Acme"
    assert req.title == "Eng"
    assert req.apply_url is None
    assert req.source is None
    assert req.notes is None
    assert req.target_job_id is None


def test_create_request_optional_strips_and_blanks_to_none() -> None:
    req = CreateApplicationRequest(
        company_name="Acme",
        title="Eng",
        apply_url="   https://x.example   ",
        source="   ",
        notes="   ",
        target_job_id="   ",
    )
    assert req.apply_url == "https://x.example"
    assert req.source is None
    assert req.notes is None
    assert req.target_job_id is None


def test_create_request_explicit_none_optional_fields() -> None:
    # Explicit JSON null hits the ``value is None`` branch of the strip validator.
    req = CreateApplicationRequest(
        company_name="Acme",
        title="Eng",
        apply_url=None,
        source=None,
        notes=None,
        target_job_id=None,
    )
    assert req.apply_url is None
    assert req.source is None
    assert req.notes is None
    assert req.target_job_id is None


def test_create_request_rejects_empty_required() -> None:
    with pytest.raises(ValidationError):
        CreateApplicationRequest(company_name="", title="Eng")
    with pytest.raises(ValidationError):
        CreateApplicationRequest(company_name="   ", title="Eng")
    with pytest.raises(ValidationError):
        CreateApplicationRequest(company_name="Acme", title="")


def test_create_request_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        CreateApplicationRequest(company_name="A", title="T", bogus="x")  # type: ignore[call-arg]


def test_create_request_enforces_max_lengths() -> None:
    with pytest.raises(ValidationError):
        CreateApplicationRequest(company_name="c" * 201, title="T")
    with pytest.raises(ValidationError):
        CreateApplicationRequest(company_name="A", title="t" * 201)
    with pytest.raises(ValidationError):
        CreateApplicationRequest(company_name="A", title="T", apply_url="u" * 1025)
    with pytest.raises(ValidationError):
        CreateApplicationRequest(company_name="A", title="T", notes="n" * 2001)


# --------------------------------------------------------- transition request


def test_transition_request_valid_status() -> None:
    req = TransitionRequest(to_status="applied", note="  hi  ")
    assert req.to_status is ApplicationStatus.applied
    assert req.note == "hi"
    assert req.expected_version is None


def test_transition_request_blank_note_to_none() -> None:
    req = TransitionRequest(to_status="applied", note="   ")
    assert req.note is None


def test_transition_request_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        TransitionRequest(to_status="bogus")


def test_transition_request_rejects_negative_version() -> None:
    with pytest.raises(ValidationError):
        TransitionRequest(to_status="applied", expected_version=-1)


def test_transition_request_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        TransitionRequest(to_status="applied", bogus="x")  # type: ignore[call-arg]


def test_transition_request_enforces_note_max_length() -> None:
    with pytest.raises(ValidationError):
        TransitionRequest(to_status="applied", note="n" * 2001)


# ------------------------------------------------------------ update request


def test_update_request_strips_and_blanks() -> None:
    req = UpdateApplicationRequest(notes="  keep  ", apply_url="  https://x  ")
    assert req.notes == "keep"
    assert req.apply_url == "https://x"


def test_update_request_blank_to_none() -> None:
    req = UpdateApplicationRequest(notes="   ", apply_url="   ")
    assert req.notes is None
    assert req.apply_url is None


def test_update_request_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        UpdateApplicationRequest(notes="x", bogus="y")  # type: ignore[call-arg]


def test_update_request_enforces_max_lengths() -> None:
    with pytest.raises(ValidationError):
        UpdateApplicationRequest(notes="n" * 2001)
    with pytest.raises(ValidationError):
        UpdateApplicationRequest(apply_url="u" * 1025)


def test_update_request_exclude_unset_distinguishes_omitted() -> None:
    # The route relies on this for correct PATCH semantics.
    req = UpdateApplicationRequest(notes="x")
    assert req.model_dump(exclude_unset=True) == {"notes": "x"}
    req2 = UpdateApplicationRequest(apply_url=None)
    assert req2.model_dump(exclude_unset=True) == {"apply_url": None}


# ------------------------------------------------------------- record response


def _record_dict(**overrides: object) -> dict:
    base: dict = {
        "id": "r1",
        "user_id": "u1",
        "target_job_id": None,
        "company_name": "Acme",
        "title": "Eng",
        "apply_url": None,
        "source": None,
        "status": "saved",
        "applied_at": None,
        "notes": None,
        "state_version": 0,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return base


def test_record_response_normalizes_aware_datetime() -> None:
    resp = ApplicationRecordResponse(**_record_dict())
    assert resp.created_at == _NOW
    assert resp.created_at.tzinfo is timezone.utc


def test_record_response_normalizes_naive_datetime() -> None:
    naive = datetime(2026, 7, 29, 12, 0, 0)
    resp = ApplicationRecordResponse(**_record_dict(created_at=naive, updated_at=naive))
    assert resp.created_at.tzinfo is timezone.utc
    assert resp.updated_at.tzinfo is timezone.utc


def test_record_response_none_applied_at() -> None:
    resp = ApplicationRecordResponse(**_record_dict(applied_at=None))
    assert resp.applied_at is None


def test_record_response_ignores_unknown_fields() -> None:
    data = _record_dict()
    data["future_column"] = "ignored"
    resp = ApplicationRecordResponse(**data)
    assert resp.id == "r1"
    assert not hasattr(resp, "future_column")


def test_record_response_status_string() -> None:
    resp = ApplicationRecordResponse(**_record_dict(status="applied", state_version=3))
    assert resp.status == "applied"
    assert resp.state_version == 3


# -------------------------------------------------------------- event response


def test_event_response_normalizes_datetime() -> None:
    naive = datetime(2026, 7, 29, 12, 0, 0)
    resp = ApplicationEventResponse(
        id=1,
        application_id="r1",
        from_status="saved",
        to_status="applied",
        note="hi",
        created_at=naive,
    )
    assert resp.created_at.tzinfo is timezone.utc
    assert resp.note == "hi"


def test_event_response_none_note() -> None:
    resp = ApplicationEventResponse(
        id=1,
        application_id="r1",
        from_status="saved",
        to_status="applied",
        note=None,
        created_at=_NOW,
    )
    assert resp.note is None


def test_list_responses_shape() -> None:
    record = ApplicationRecordResponse(**_record_dict())
    lr = ApplicationListResponse(items=[record], total=1)
    assert lr.total == 1
    assert len(lr.items) == 1
    ev = ApplicationEventResponse(
        id=1,
        application_id="r1",
        from_status="saved",
        to_status="applied",
        note=None,
        created_at=_NOW,
    )
    el = ApplicationEventListResponse(items=[ev])
    assert len(el.items) == 1
