"""Unit tests for the InterviewPrepService.

Covers the create_kit -> ready / failed pipeline, the three input-error codes,
the read paths (get/list), and the defensive ``complete_kit`` fallbacks. The
DB is an in-memory SQLite with foreign keys OFF so a MatchReport can be seeded
without the full job/profile/session referential chain, and so the
profile-version-missing branch can be exercised by pointing at a non-existent
id. The generator is always a fake object - no network is used here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import (
    ConfirmedProfileVersion,
    MatchReport,
    Profile,
    User,
)
from backend.app.domain.interview_prep import InterviewPrepKitStatus
from backend.app.services.interview_prep.generator import InterviewPrepGenerationError
from backend.app.services.interview_prep.service import (
    InterviewPrepInputError,
    InterviewPrepNotFoundError,
    InterviewPrepService,
    _truncate,
)
from tests.conftest import settings_override

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL_VERSION = "1.0"
PROMPT_VERSION = "1.0"
OUTPUT_SCHEMA_VERSION = "1.0"
SCORING_RULE_VERSION = "1.0"

SAMPLE_FACTS: dict = {
    "name": "Alice",
    "skills": [{"name": "Python", "level": "Expert"}],
    "work_experience": [{"company": "TechCorp", "title": "Senior Engineer"}],
}
SAMPLE_JOB_SNAPSHOT: dict = {
    "company_name": "TestCorp",
    "title": "Software Engineer",
    "description_text": "We need Python experts.",
}

_CONTENT = {
    "technical_questions": ["Explain Python GIL."],
    "behavioral_questions": ["Tell me about a conflict."],
    "talking_points": ["Led a team of 5."],
    "topics_to_review": ["Concurrency."],
    "questions_to_ask": ["Team structure?"],
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeGenerator:
    """Returns a canned result, or raises on demand, recording its inputs."""

    def __init__(
        self,
        result: Any = None,
        *,
        error: Exception | None = None,
    ):
        self._result = result if result is not None else {"content": _CONTENT}
        self._error = error
        self.last_kwargs: dict[str, Any] | None = None

    def generate_prep(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._result


# ---------------------------------------------------------------------------
# DB fixture (FK off, in-memory, single connection)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def user(db: Session) -> User:
    u = User(
        id=str(uuid.uuid4()),
        account="prep-tester",
        nickname="Prep Tester",
        password_hash="argon2-placeholder",
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture()
def profile_version(db: Session, user: User) -> ConfirmedProfileVersion:
    profile = Profile(
        id=str(uuid.uuid4()),
        user_id=user.id,
        version=0,
        local_sensitive_references={},
    )
    db.add(profile)
    db.flush()
    cv = ConfirmedProfileVersion(
        id=str(uuid.uuid4()),
        profile_id=profile.id,
        version_number=1,
        aggregate_version=1,
        facts_snapshot=SAMPLE_FACTS,
        evidence_refs={},
        local_sensitive_references={},
    )
    db.add(cv)
    db.flush()
    return cv


def _make_match_report(
    db: Session,
    *,
    user: User,
    profile_version_id: str,
    status: str = "completed",
    error_code: str | None = None,
) -> MatchReport:
    mr = MatchReport(
        id=str(uuid.uuid4()),
        user_id=user.id,
        analysis_session_id="sess-placeholder",  # FK off -> no real row needed
        job_id="job-placeholder",
        job_verification_id="jv-placeholder",
        job_snapshot=SAMPLE_JOB_SNAPSHOT,
        profile_version_id=profile_version_id,
        request_idempotency_key=f"mr-ik-{uuid.uuid4().hex[:8]}",
        request_hash=f"mr-hash-{uuid.uuid4().hex[:8]}",
        status=status,
        score=85,
        scoring_rule_version=SCORING_RULE_VERSION,
        model_version=MODEL_VERSION,
        prompt_version=PROMPT_VERSION,
        output_schema_version=OUTPUT_SCHEMA_VERSION,
        strengths=[{"area": "Python"}],
        gaps=[{"area": "Go"}],
        unknowns=[],
        risks=[],
        recommendation={"text": "Proceed."},
        error_code=error_code,
        completed_at=datetime.now(timezone.utc) if status == "completed" else None,
    )
    db.add(mr)
    db.flush()
    return mr


def _service(*, generator=None) -> InterviewPrepService:
    settings = settings_override(interview_prep_agent_version="1.0.0")
    return InterviewPrepService(settings, generator=generator)


# ---------------------------------------------------------------------------
# create_kit - happy path
# ---------------------------------------------------------------------------


def test_create_kit_ready_stores_content_and_context(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    mr = _make_match_report(
        db, user=user, profile_version_id=profile_version.id
    )
    gen = _FakeGenerator()
    service = _service(generator=gen)

    kit = service.create_kit(db, user=user, match_report_id=mr.id)

    assert kit.status == InterviewPrepKitStatus.ready
    assert kit.content_json == _CONTENT
    assert kit.target_job_id == "job-placeholder"
    assert kit.profile_version_id == profile_version.id
    assert kit.agent_version == "1.0.0"
    assert kit.error_code is None
    assert kit.finished_at is not None
    # Audit columns populated at creation.
    assert kit.preferences_summary_json is not None
    assert kit.preferences_summary_json["desired_roles"] == []  # no pref row -> default
    assert kit.match_analysis_json == {
        "strengths": [{"area": "Python"}],
        "gaps": [{"area": "Go"}],
        "unknowns": [],
        "risks": [],
    }
    # Generator received all four grounding inputs.
    assert gen.last_kwargs is not None
    assert gen.last_kwargs["job_snapshot"] == SAMPLE_JOB_SNAPSHOT
    assert gen.last_kwargs["profile_facts"] == SAMPLE_FACTS
    assert gen.last_kwargs["match_analysis"]["strengths"] == [{"area": "Python"}]


def test_create_kit_passes_user_preferences_when_present(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    from backend.app.db.models import UserPreference
    from backend.app.domain.preferences import WorkModePreference

    pref = UserPreference(
        id=str(uuid.uuid4()),
        user_id=user.id,
        version=1,
        desired_roles=["Backend Engineer"],
        target_cities=["Beijing"],
        work_mode=WorkModePreference.REMOTE,
        is_active_search=True,
    )
    db.add(pref)
    db.flush()
    mr = _make_match_report(
        db, user=user, profile_version_id=profile_version.id
    )
    gen = _FakeGenerator()
    service = _service(generator=gen)

    kit = service.create_kit(db, user=user, match_report_id=mr.id)

    assert kit.status == InterviewPrepKitStatus.ready
    assert kit.preferences_summary_json["desired_roles"] == ["Backend Engineer"]
    assert gen.last_kwargs["preferences"]["desired_roles"] == ["Backend Engineer"]


# ---------------------------------------------------------------------------
# create_kit - failure paths
# ---------------------------------------------------------------------------


def test_create_kit_generator_parse_error_finalizes_failed(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    mr = _make_match_report(
        db, user=user, profile_version_id=profile_version.id
    )
    gen = _FakeGenerator(
        error=InterviewPrepGenerationError("interview_prep_empty_content", "no content")
    )
    service = _service(generator=gen)

    kit = service.create_kit(db, user=user, match_report_id=mr.id)

    assert kit.status == InterviewPrepKitStatus.failed
    assert kit.error_code == "interview_prep_empty_content"
    assert "no content" in (kit.last_error or "")
    assert kit.content_json is None


def test_create_kit_generator_long_error_is_truncated(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    mr = _make_match_report(
        db, user=user, profile_version_id=profile_version.id
    )
    long_message = "x" * 5000
    gen = _FakeGenerator(
        error=InterviewPrepGenerationError("interview_prep_parse_error", long_message)
    )
    service = _service(generator=gen)

    kit = service.create_kit(db, user=user, match_report_id=mr.id)

    assert kit.status == InterviewPrepKitStatus.failed
    assert kit.error_code == "interview_prep_parse_error"
    # last_error clamped to LAST_ERROR_MAX_LENGTH (2000) + ellipsis.
    assert len(kit.last_error) <= 2000
    assert kit.last_error.endswith("...")


def test_create_kit_generator_generic_exception_finalizes_failed(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    mr = _make_match_report(
        db, user=user, profile_version_id=profile_version.id
    )
    gen = _FakeGenerator(error=RuntimeError("LLM crashed"))
    service = _service(generator=gen)

    kit = service.create_kit(db, user=user, match_report_id=mr.id)

    assert kit.status == InterviewPrepKitStatus.failed
    assert kit.error_code == "interview_prep_generation_interrupted"
    assert kit.content_json is None


def test_create_kit_no_generator_finalizes_failed(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    mr = _make_match_report(
        db, user=user, profile_version_id=profile_version.id
    )
    service = _service(generator=None)

    kit = service.create_kit(db, user=user, match_report_id=mr.id)

    assert kit.status == InterviewPrepKitStatus.failed
    assert kit.error_code == "interview_prep_generator_unavailable"


def test_create_kit_profile_version_missing_finalizes_failed(
    db: Session, user: User
):
    # MatchReport points at a profile_version_id that has no row.
    mr = _make_match_report(
        db, user=user, profile_version_id=str(uuid.uuid4())
    )
    service = _service(generator=_FakeGenerator())

    kit = service.create_kit(db, user=user, match_report_id=mr.id)

    assert kit.status == InterviewPrepKitStatus.failed
    assert kit.error_code == "interview_prep_profile_version_missing"


# ---------------------------------------------------------------------------
# create_kit - input errors (no kit row written)
# ---------------------------------------------------------------------------


def test_create_kit_match_not_found_raises(db: Session, user: User):
    service = _service(generator=_FakeGenerator())
    with pytest.raises(InterviewPrepInputError) as exc:
        service.create_kit(db, user=user, match_report_id="does-not-exist")
    assert exc.value.code == "not_found"


def test_create_kit_match_not_completed_raises(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    mr = _make_match_report(
        db,
        user=user,
        profile_version_id=profile_version.id,
        status="pending",
    )
    service = _service(generator=_FakeGenerator())
    with pytest.raises(InterviewPrepInputError) as exc:
        service.create_kit(db, user=user, match_report_id=mr.id)
    assert exc.value.code == "match_not_completed"


def test_create_kit_match_failed_raises(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    mr = _make_match_report(
        db,
        user=user,
        profile_version_id=profile_version.id,
        status="completed",
        error_code="match_execution_interrupted",
    )
    service = _service(generator=_FakeGenerator())
    with pytest.raises(InterviewPrepInputError) as exc:
        service.create_kit(db, user=user, match_report_id=mr.id)
    assert exc.value.code == "match_failed"


def test_create_kit_cross_user_not_found(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    # MatchReport owned by a different user -> invisible to this user.
    other = User(
        id=str(uuid.uuid4()),
        account="other",
        nickname="Other",
        password_hash="x",
    )
    db.add(other)
    db.flush()
    mr = _make_match_report(db, user=other, profile_version_id=profile_version.id)
    service = _service(generator=_FakeGenerator())
    with pytest.raises(InterviewPrepInputError) as exc:
        service.create_kit(db, user=user, match_report_id=mr.id)
    assert exc.value.code == "not_found"


# ---------------------------------------------------------------------------
# create_kit - defensive complete_kit fallback (row vanished mid-run)
# ---------------------------------------------------------------------------


def test_create_kit_success_returns_kit_when_complete_returns_none(
    db: Session, user: User, profile_version: ConfirmedProfileVersion, monkeypatch
):
    mr = _make_match_report(
        db, user=user, profile_version_id=profile_version.id
    )
    service = _service(generator=_FakeGenerator())
    # Force repo.complete_kit to behave as if the row vanished.
    from backend.app.services.interview_prep import service as service_module

    monkeypatch.setattr(service_module.repo, "complete_kit", lambda *a, **k: None)

    kit = service.create_kit(db, user=user, match_report_id=mr.id)

    # Falls back to the in-memory generating kit (no None returned).
    assert kit is not None
    assert kit.status == InterviewPrepKitStatus.generating


def test_create_kit_failed_returns_kit_when_complete_returns_none(
    db: Session, user: User, profile_version: ConfirmedProfileVersion, monkeypatch
):
    mr = _make_match_report(
        db, user=user, profile_version_id=profile_version.id
    )
    service = _service(generator=None)  # triggers _finalize_failed
    from backend.app.services.interview_prep import service as service_module

    monkeypatch.setattr(service_module.repo, "complete_kit", lambda *a, **k: None)

    kit = service.create_kit(db, user=user, match_report_id=mr.id)

    assert kit is not None
    assert kit.status == InterviewPrepKitStatus.generating


# ---------------------------------------------------------------------------
# get_kit / list_kits
# ---------------------------------------------------------------------------


def test_get_kit_returns_owner_kit(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    mr = _make_match_report(
        db, user=user, profile_version_id=profile_version.id
    )
    service = _service(generator=_FakeGenerator())
    created = service.create_kit(db, user=user, match_report_id=mr.id)

    fetched = service.get_kit(db, str(created.id), user=user)
    assert str(fetched.id) == str(created.id)


def test_get_kit_not_found_raises(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    service = _service(generator=_FakeGenerator())
    with pytest.raises(InterviewPrepNotFoundError):
        service.get_kit(db, "missing-id", user=user)


def test_get_kit_cross_user_not_found(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    mr = _make_match_report(
        db, user=user, profile_version_id=profile_version.id
    )
    service = _service(generator=_FakeGenerator())
    created = service.create_kit(db, user=user, match_report_id=mr.id)

    other = User(
        id=str(uuid.uuid4()),
        account="other2",
        nickname="Other2",
        password_hash="x",
    )
    db.add(other)
    db.flush()
    with pytest.raises(InterviewPrepNotFoundError):
        service.get_kit(db, str(created.id), user=other)


def test_list_kits_returns_newest_first(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    service = _service(generator=_FakeGenerator())
    created_ids = []
    for _ in range(3):
        mr = _make_match_report(
            db, user=user, profile_version_id=profile_version.id
        )
        kit = service.create_kit(db, user=user, match_report_id=mr.id)
        created_ids.append(str(kit.id))

    items = service.list_kits(db, user=user)
    assert [str(k.id) for k in items] == list(reversed(created_ids))


def test_list_kits_respects_pagination(
    db: Session, user: User, profile_version: ConfirmedProfileVersion
):
    service = _service(generator=_FakeGenerator())
    for _ in range(5):
        mr = _make_match_report(
            db, user=user, profile_version_id=profile_version.id
        )
        service.create_kit(db, user=user, match_report_id=mr.id)

    page = service.list_kits(db, user=user, limit=2, offset=1)
    assert len(page) == 2


def test_list_kits_empty(db: Session, user: User):
    service = _service(generator=_FakeGenerator())
    assert service.list_kits(db, user=user) == []


# ---------------------------------------------------------------------------
# _truncate helper
# ---------------------------------------------------------------------------


def test_truncate_short_text_returned_unchanged():
    assert _truncate("hi", 10) == "hi"


def test_truncate_long_text_clamped():
    assert _truncate("x" * 100, 10) == "xxxxxxx" + "..."


def test_truncate_exact_limit_returned_unchanged():
    assert _truncate("x" * 10, 10) == "x" * 10
