"""Unit tests for the company-research repository (data access only)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.db.models import User, UserRole
from backend.app.domain.company_research import (
    CompanyResearchBlockReason,
    CompanyResearchStatus,
)
from backend.app.repositories import company_research as repository


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


def _url_hash(seed: str) -> str:
    return (seed * 8)[:64]


def _create(db: Session, user_id: str, *, name: str = "Acme", seed: str = "a") -> object:
    return repository.create_report(
        db,
        user_id=user_id,
        company_name=name,
        source_url=f"https://careers.{name.lower()}.example",
        source_url_hash=_url_hash(seed),
        agent_version="1.0.0",
    )


def test_create_report_defaults_to_queued(db_session: Session) -> None:
    user = _make_user(db_session)
    report = _create(db_session, user.id)
    assert report.status == CompanyResearchStatus.queued
    assert report.company_name == "Acme"
    assert report.agent_version == "1.0.0"
    assert report.block_reason is None
    assert report.profile_json is None
    assert report.openings_json is None
    assert report.started_at is None
    assert report.finished_at is None
    assert report.last_error is None


def test_get_report_and_owner_scoping(db_session: Session) -> None:
    user = _make_user(db_session)
    other = _make_user(db_session, "bob")
    report = _create(db_session, user.id)
    assert repository.get_report(db_session, report.id).id == report.id
    assert repository.get_report_for_owner(db_session, report.id, user.id).id == report.id
    assert repository.get_report_for_owner(db_session, report.id, other.id) is None
    assert repository.get_report(db_session, "missing") is None


def test_list_reports_pages_newest_first(db_session: Session) -> None:
    user = _make_user(db_session)
    first = _create(db_session, user.id, name="A", seed="a")
    second = _create(db_session, user.id, name="B", seed="b")
    rows = repository.list_reports(db_session, user.id, limit=10)
    assert [r.id for r in rows] == [second.id, first.id]
    paged = repository.list_reports(db_session, user.id, limit=10, offset=1)
    assert [r.id for r in paged] == [first.id]
    other = _make_user(db_session, "carol")
    assert repository.list_reports(db_session, other.id) == []


def test_claim_for_run_transitions_queued_to_running(db_session: Session) -> None:
    user = _make_user(db_session)
    report = _create(db_session, user.id)
    claimed = repository.claim_for_run(db_session, report.id)
    assert claimed.status == CompanyResearchStatus.running
    assert claimed.started_at is not None
    # Re-claiming a running report returns None without mutating.
    assert repository.claim_for_run(db_session, report.id) is None
    # Missing report returns None.
    assert repository.claim_for_run(db_session, "nope") is None


def test_complete_report_success_persists_payload(db_session: Session) -> None:
    user = _make_user(db_session)
    report = _create(db_session, user.id)
    repository.claim_for_run(db_session, report.id)
    done = repository.complete_report(
        db_session,
        report.id,
        status=CompanyResearchStatus.succeeded,
        profile_json={"description": "maker of things"},
        openings_json=[{"title": "Engineer", "locations": ["Beijing"]}],
        evidence_refs_json=[{"evidence_type": "page", "content_hash": _url_hash("p")}],
        summary="2 openings found",
    )
    assert done.status == CompanyResearchStatus.succeeded
    assert done.profile_json == {"description": "maker of things"}
    assert done.openings_json == [
        {"title": "Engineer", "locations": ["Beijing"]}
    ]
    assert done.evidence_refs_json[0]["evidence_type"] == "page"
    assert done.summary == "2 openings found"
    assert done.finished_at is not None
    assert done.block_reason is None


def test_complete_report_blocked_sets_block_reason(db_session: Session) -> None:
    user = _make_user(db_session)
    report = _create(db_session, user.id)
    repository.claim_for_run(db_session, report.id)
    done = repository.complete_report(
        db_session,
        report.id,
        status=CompanyResearchStatus.needs_manual_review,
        block_reason=CompanyResearchBlockReason.anti_bot,
        summary="verification wall",
    )
    assert done.status == CompanyResearchStatus.needs_manual_review
    assert done.block_reason == CompanyResearchBlockReason.anti_bot
    assert done.profile_json is None


def test_complete_report_failed_sets_last_error(db_session: Session) -> None:
    user = _make_user(db_session)
    report = _create(db_session, user.id)
    repository.claim_for_run(db_session, report.id)
    done = repository.complete_report(
        db_session,
        report.id,
        status=CompanyResearchStatus.failed,
        last_error="browse subprocess crashed",
        summary="runtime error",
    )
    assert done.status == CompanyResearchStatus.failed
    assert done.last_error == "browse subprocess crashed"
    assert done.summary == "runtime error"


def test_complete_report_missing_returns_none(db_session: Session) -> None:
    assert (
        repository.complete_report(
            db_session, "nope", status=CompanyResearchStatus.failed
        )
        is None
    )


def test_list_reports_by_status(db_session: Session) -> None:
    user = _make_user(db_session)
    first = _create(db_session, user.id, name="A", seed="a")
    second = _create(db_session, user.id, name="B", seed="b")
    repository.claim_for_run(db_session, first.id)
    queued = repository.list_reports_by_status(
        db_session, CompanyResearchStatus.queued
    )
    assert [r.id for r in queued] == [second.id]
    running = repository.list_reports_by_status(
        db_session, CompanyResearchStatus.running
    )
    assert [r.id for r in running] == [first.id]
