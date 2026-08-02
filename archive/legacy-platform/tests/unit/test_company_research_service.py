"""Unit tests for the CompanyResearchService business layer.

The runtime is stubbed so these tests stay deterministic and browser-free; the
runtime + browse script are covered by their own dedicated test modules.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from backend.app.db.models import User, UserRole
from backend.app.domain.company_research import (
    COMPANY_NAME_MAX_LENGTH,
    CompanyResearchBlockReason,
    CompanyResearchStatus,
)
from backend.app.repositories import company_research as repository
from backend.app.services.company_research.runtime import CompanyResearchResult
from backend.app.services.company_research.service import (
    CompanyResearchNotFoundError,
    CompanyResearchService,
    InvalidCompanyResearchInput,
    InvalidCompanyResearchTransition,
)
from tests.conftest import settings_override


class _FakeRuntime:
    """Stand-in runtime that returns a preset result on each ``run``."""

    def __init__(self, result: CompanyResearchResult) -> None:
        self.result = result
        self.calls: list[dict[str, str]] = []

    def run(self, *, report_id: str, company_name: str, source_url: str) -> CompanyResearchResult:
        self.calls.append(
            {"report_id": report_id, "company_name": company_name, "source_url": source_url}
        )
        return self.result


def _settings(**overrides: Any) -> Any:
    return settings_override(
        company_research_enabled=True,
        company_research_agent_version="1.0.0",
        **overrides,
    )


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


def _service(result: CompanyResearchResult, *, settings: Any | None = None) -> CompanyResearchService:
    return CompanyResearchService(
        settings or _settings(),
        runtime=_FakeRuntime(result),
    )


# ---------------------------------------------------------------------------
# create_report
# ---------------------------------------------------------------------------


def test_create_report_validates_and_hashes_url(db_session: Session) -> None:
    service = _service(_succeeded_result())
    user = _make_user(db_session)
    report = service.create_report(
        db_session,
        user=user,
        company_name="  Acme  ",
        source_url="https://careers.acme.example",
    )
    assert report.status == CompanyResearchStatus.queued
    assert report.company_name == "Acme"  # stripped
    assert report.source_url == "https://careers.acme.example"
    assert len(report.source_url_hash) == 64
    assert report.agent_version == "1.0.0"
    assert report.user_id == user.id


@pytest.mark.parametrize(
    "company_name, source_url",
    [
        ("", "https://x.example"),
        ("   ", "https://x.example"),
        ("Acme", "ftp://x.example"),
        ("Acme", "not-a-url"),
        ("Acme", ""),
    ],
)
def test_create_report_rejects_invalid_input(
    db_session: Session, company_name: str, source_url: str
) -> None:
    service = _service(_succeeded_result())
    user = _make_user(db_session)
    with pytest.raises(InvalidCompanyResearchInput):
        service.create_report(
            db_session, user=user, company_name=company_name, source_url=source_url
        )


def test_create_report_rejects_oversized_name(db_session: Session) -> None:
    service = _service(_succeeded_result())
    user = _make_user(db_session)
    with pytest.raises(InvalidCompanyResearchInput):
        service.create_report(
            db_session,
            user=user,
            company_name="x" * (COMPANY_NAME_MAX_LENGTH + 1),
            source_url="https://x.example",
        )


# ---------------------------------------------------------------------------
# run_report
# ---------------------------------------------------------------------------


def test_run_report_success_writes_payload(db_session: Session) -> None:
    result = _succeeded_result()
    service = _service(result)
    user = _make_user(db_session)
    report = service.create_report(
        db_session, user=user, company_name="Acme", source_url="https://careers.acme.example"
    )
    done = service.run_report(db_session, str(report.id), user=user)
    assert done.status == CompanyResearchStatus.succeeded
    assert done.profile_json == result.profile
    assert done.openings_json == result.openings
    assert done.evidence_refs_json == result.evidence_refs
    assert done.summary == result.summary
    assert done.block_reason is None
    assert done.last_error is None
    assert done.finished_at is not None
    assert service.runtime.calls[0]["company_name"] == "Acme"  # type: ignore[attr-defined]


def test_run_report_blocked_maps_block_reason(db_session: Session) -> None:
    result = CompanyResearchResult(
        status="needs_manual_review",
        block_reason="anti_bot",
        summary="verification wall detected; needs manual review",
        profile={"description": "partial"},
        openings=[{"title": "Engineer"}],
        evidence_refs=[{"evidence_type": "page_text"}],
    )
    service = _service(result)
    user = _make_user(db_session)
    report = service.create_report(
        db_session, user=user, company_name="Acme", source_url="https://careers.acme.example"
    )
    done = service.run_report(db_session, str(report.id), user=user)
    assert done.status == CompanyResearchStatus.needs_manual_review
    assert done.block_reason == CompanyResearchBlockReason.anti_bot
    # Payload is preserved so a reviewer can still see partial evidence.
    assert done.profile_json == {"description": "partial"}
    assert done.openings_json == [{"title": "Engineer"}]
    assert done.evidence_refs_json == [{"evidence_type": "page_text"}]
    assert done.last_error is None


def test_run_report_blocked_with_unknown_reason_falls_back(db_session: Session) -> None:
    result = CompanyResearchResult(
        status="needs_manual_review",
        block_reason="not-a-real-reason",
        summary="unknown block",
    )
    service = _service(result)
    user = _make_user(db_session)
    report = service.create_report(
        db_session, user=user, company_name="Acme", source_url="https://careers.acme.example"
    )
    done = service.run_report(db_session, str(report.id), user=user)
    assert done.block_reason == CompanyResearchBlockReason.no_evidence


def test_run_report_blocked_without_block_reason_keeps_none(db_session: Session) -> None:
    result = CompanyResearchResult(
        status="needs_manual_review",
        block_reason=None,
        summary="blocked",
    )
    service = _service(result)
    user = _make_user(db_session)
    report = service.create_report(
        db_session, user=user, company_name="Acme", source_url="https://careers.acme.example"
    )
    done = service.run_report(db_session, str(report.id), user=user)
    assert done.block_reason is None


def test_run_report_failed_sets_last_error(db_session: Session) -> None:
    result = CompanyResearchResult(
        status="failed",
        summary="company-research page fetch failed",
        last_error="boom",
    )
    service = _service(result)
    user = _make_user(db_session)
    report = service.create_report(
        db_session, user=user, company_name="Acme", source_url="https://careers.acme.example"
    )
    done = service.run_report(db_session, str(report.id), user=user)
    assert done.status == CompanyResearchStatus.failed
    assert done.last_error == "boom"
    assert done.profile_json is None


def test_run_report_unexpected_status_coerced_to_failed(db_session: Session) -> None:
    # A runtime that emits a non-terminal / unknown status must never leave
    # the row silently in ``running``.
    result = CompanyResearchResult(status="running", summary="huh")
    service = _service(result)
    user = _make_user(db_session)
    report = service.create_report(
        db_session, user=user, company_name="Acme", source_url="https://careers.acme.example"
    )
    done = service.run_report(db_session, str(report.id), user=user)
    assert done.status == CompanyResearchStatus.failed


def test_run_report_missing_raises_not_found(db_session: Session) -> None:
    service = _service(_succeeded_result())
    user = _make_user(db_session)
    with pytest.raises(CompanyResearchNotFoundError):
        service.run_report(db_session, "missing", user=user)


def test_run_report_not_owned_raises_not_found(db_session: Session) -> None:
    service = _service(_succeeded_result())
    owner = _make_user(db_session, "alice")
    other = _make_user(db_session, "bob")
    report = service.create_report(
        db_session, user=owner, company_name="Acme", source_url="https://careers.acme.example"
    )
    with pytest.raises(CompanyResearchNotFoundError):
        service.run_report(db_session, str(report.id), user=other)


def test_run_report_already_running_raises_transition(db_session: Session) -> None:
    service = _service(_succeeded_result())
    user = _make_user(db_session)
    report = service.create_report(
        db_session, user=user, company_name="Acme", source_url="https://careers.acme.example"
    )
    # Pre-claim so claim_for_run returns None on the real run.
    repository.claim_for_run(db_session, report.id)
    with pytest.raises(InvalidCompanyResearchTransition):
        service.run_report(db_session, str(report.id), user=user)


def test_run_report_complete_returns_none_falls_back_to_report(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate the row vanishing mid-run: complete_report returns None, so the
    # service must still return *something* (the claimed in-memory report).
    service = _service(_succeeded_result())
    user = _make_user(db_session)
    report = service.create_report(
        db_session, user=user, company_name="Acme", source_url="https://careers.acme.example"
    )
    monkeypatch.setattr(
        "backend.app.services.company_research.service.repo.complete_report",
        lambda *a, **k: None,
    )
    done = service.run_report(db_session, str(report.id), user=user)
    assert done.id == report.id
    assert done.status == CompanyResearchStatus.running  # claim happened, complete was stubbed


# ---------------------------------------------------------------------------
# get_report / list_reports
# ---------------------------------------------------------------------------


def test_get_report_returns_owner_report(db_session: Session) -> None:
    service = _service(_succeeded_result())
    user = _make_user(db_session)
    created = service.create_report(
        db_session, user=user, company_name="Acme", source_url="https://careers.acme.example"
    )
    fetched = service.get_report(db_session, str(created.id), user=user)
    assert fetched.id == created.id


def test_get_report_missing_raises_not_found(db_session: Session) -> None:
    service = _service(_succeeded_result())
    user = _make_user(db_session)
    with pytest.raises(CompanyResearchNotFoundError):
        service.get_report(db_session, "missing", user=user)


def test_list_reports_returns_user_reports(db_session: Session) -> None:
    service = _service(_succeeded_result())
    user = _make_user(db_session)
    first = service.create_report(
        db_session, user=user, company_name="A", source_url="https://a.example"
    )
    second = service.create_report(
        db_session, user=user, company_name="B", source_url="https://b.example"
    )
    rows = service.list_reports(db_session, user=user)
    assert [r.id for r in rows] == [second.id, first.id]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _succeeded_result() -> CompanyResearchResult:
    return CompanyResearchResult(
        status="succeeded",
        summary="2 openings found",
        profile={"company_name": "Acme", "description": "maker of things", "opening_count": 2},
        openings=[{"title": "Engineer", "locations": ["Beijing"]}],
        evidence_refs=[{"evidence_type": "page_text", "content_hash": "abc"}],
    )
