"""Unit tests for the interview-prep pydantic schemas (DTOs)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.app.api.interview_prep_schemas import (
    CreateInterviewPrepRequest,
    InterviewPrepKitResponse,
    InterviewPrepListResponse,
)
from backend.app.domain.interview_prep import AGENT_VERSION_MAX_LENGTH

# ---------------------------------------------------------------------------
# CreateInterviewPrepRequest
# ---------------------------------------------------------------------------


def test_request_strips_whitespace_from_match_report_id() -> None:
    req = CreateInterviewPrepRequest(match_report_id="  mr-123  ")
    assert req.match_report_id == "mr-123"


def test_request_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        CreateInterviewPrepRequest(
            match_report_id="mr-1",
            unexpected="boom",  # type: ignore[call-arg]
        )


@pytest.mark.parametrize("value", ["", "   "])
def test_request_rejects_empty_match_report_id(value: str) -> None:
    with pytest.raises(ValidationError):
        CreateInterviewPrepRequest(match_report_id=value)


# ---------------------------------------------------------------------------
# InterviewPrepKitResponse datetime normalization + projection
# ---------------------------------------------------------------------------


def _response(**overrides) -> InterviewPrepKitResponse:
    base = dict(
        id="k1",
        user_id="u1",
        target_job_id="job-1",
        profile_version_id="cv-1",
        agent_version="1.0.0",
        status="ready",
        content={"technical_questions": ["q1"]},
        preferences={"desired_roles": []},
        match_analysis={"strengths": []},
        created_at=datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 29, 12, 5, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return InterviewPrepKitResponse(**base)


def test_response_normalizes_aware_datetimes() -> None:
    res = _response(
        started_at=datetime(2026, 7, 29, 12, 1, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 7, 29, 12, 2, 0, tzinfo=timezone.utc),
    )
    assert res.created_at.tzinfo is not None
    assert res.started_at is not None and res.started_at.tzinfo is not None
    assert res.finished_at is not None and res.finished_at.tzinfo is not None


def test_response_normalizes_naive_datetimes() -> None:
    res = _response(
        created_at=datetime(2026, 7, 29, 12, 0, 0),  # naive
        updated_at=datetime(2026, 7, 29, 12, 5, 0),  # naive
        started_at=datetime(2026, 7, 29, 12, 1, 0),  # naive
    )
    assert res.created_at.tzinfo is not None
    assert res.updated_at.tzinfo is not None
    assert res.started_at is not None and res.started_at.tzinfo is not None


def test_response_accepts_none_optional_fields() -> None:
    res = _response(
        target_job_id=None,
        profile_version_id=None,
        content=None,
        preferences=None,
        match_analysis=None,
        started_at=None,
        finished_at=None,
    )
    assert res.target_job_id is None
    assert res.profile_version_id is None
    assert res.content is None
    assert res.preferences is None
    assert res.match_analysis is None
    assert res.started_at is None
    assert res.finished_at is None


def test_response_ignores_extra_fields() -> None:
    res = InterviewPrepKitResponse(
        id="k1",
        user_id="u1",
        agent_version="1.0.0",
        status="ready",
        created_at=datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 29, 12, 5, 0, tzinfo=timezone.utc),
        future_column="ignored",  # type: ignore[call-arg]
    )
    assert not hasattr(res, "future_column")


def test_response_clamps_oversized_agent_version() -> None:
    long_version = "v" * (AGENT_VERSION_MAX_LENGTH + 10)
    res = _response(agent_version=long_version)
    assert len(res.agent_version) == AGENT_VERSION_MAX_LENGTH


def test_list_response_shape() -> None:
    lst = InterviewPrepListResponse(items=[_response()], total=1)
    assert lst.total == 1
    assert lst.items[0].id == "k1"
