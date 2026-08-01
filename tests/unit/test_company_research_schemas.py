"""Unit tests for the company-research pydantic schemas (DTOs)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.app.api.company_research_schemas import (
    CompanyResearchListResponse,
    CompanyResearchReportResponse,
    CreateCompanyResearchRequest,
)


# ---------------------------------------------------------------------------
# CreateCompanyResearchRequest
# ---------------------------------------------------------------------------


def test_request_strips_and_normalizes_valid_input() -> None:
    req = CreateCompanyResearchRequest(
        company_name="  Acme  ",
        source_url="  https://careers.acme.example  ",
    )
    assert req.company_name == "Acme"
    assert req.source_url == "https://careers.acme.example"


def test_request_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        CreateCompanyResearchRequest(
            company_name="Acme",
            source_url="https://x.example",
            unexpected="boom",  # type: ignore[call-arg]
        )


@pytest.mark.parametrize(
    "company_name, source_url",
    [
        ("", "https://x.example"),  # empty name
        ("   ", "https://x.example"),  # whitespace name
        ("Acme", "ftp://x.example"),  # bad scheme
        ("Acme", "not-a-url"),  # no scheme
        ("Acme", ""),  # empty url (rejected by min_length before validator)
        ("Acme", "   "),  # whitespace url -> strips to empty inside validator
    ],
)
def test_request_rejects_invalid_input(company_name: str, source_url: str) -> None:
    with pytest.raises(ValidationError):
        CreateCompanyResearchRequest(company_name=company_name, source_url=source_url)


# ---------------------------------------------------------------------------
# CompanyResearchReportResponse datetime normalization
# ---------------------------------------------------------------------------


def _response(**overrides) -> CompanyResearchReportResponse:
    base = dict(
        id="r1",
        user_id="u1",
        company_name="Acme",
        source_url="https://careers.acme.example",
        agent_version="1.0.0",
        status="succeeded",
        created_at=datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 29, 12, 5, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return CompanyResearchReportResponse(**base)


def test_response_normalizes_aware_datetimes() -> None:
    res = _response(
        started_at=datetime(2026, 7, 29, 12, 1, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 7, 29, 12, 2, 0, tzinfo=timezone.utc),
    )
    assert res.created_at.tzinfo is not None
    assert res.started_at is not None and res.started_at.tzinfo is not None
    assert res.finished_at is not None and res.finished_at.tzinfo is not None


def test_response_normalizes_naive_datetimes() -> None:
    # Naive datetimes are assumed UTC by the ``_as_utc`` normalizer.
    res = _response(
        created_at=datetime(2026, 7, 29, 12, 0, 0),  # naive
        updated_at=datetime(2026, 7, 29, 12, 5, 0),  # naive
        started_at=datetime(2026, 7, 29, 12, 1, 0),  # naive
    )
    assert res.created_at.tzinfo is not None
    assert res.updated_at.tzinfo is not None
    assert res.started_at is not None and res.started_at.tzinfo is not None


def test_response_accepts_none_optional_datetimes() -> None:
    res = _response(started_at=None, finished_at=None)
    assert res.started_at is None
    assert res.finished_at is None


def test_response_ignores_extra_fields() -> None:
    res = CompanyResearchReportResponse(
        id="r1",
        user_id="u1",
        company_name="Acme",
        source_url="https://careers.acme.example",
        agent_version="1.0.0",
        status="succeeded",
        created_at=datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 29, 12, 5, 0, tzinfo=timezone.utc),
        future_column="ignored",  # type: ignore[call-arg]
    )
    assert not hasattr(res, "future_column")


def test_list_response_shape() -> None:
    lst = CompanyResearchListResponse(items=[_response()], total=1)
    assert lst.total == 1
    assert lst.items[0].id == "r1"
