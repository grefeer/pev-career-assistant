from __future__ import annotations

from collections.abc import Callable, Mapping, Set
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
import ipaddress
import re
from urllib.parse import SplitResult, urlsplit

from sqlalchemy.orm import Session

from backend.app.db.base import utc_now
from backend.app.db.models import JobPosting, JobPostingStatus, JobVerification
from backend.app.domain.job_review import EXPIRE_REASON_CODES, REJECT_REASON_CODES
from backend.app.repositories import jobs


class JobNotFoundError(LookupError):
    pass


class StaleJobReviewError(RuntimeError):
    pass


class InvalidJobReviewTransition(ValueError):
    pass


class IncompleteJobError(ValueError):
    pass


@dataclass(frozen=True)
class JobCompletionInput:
    company_name: str
    title: str
    description_text: str
    locations: list[str]
    recruitment_types: list[str]
    industries: list[str]
    apply_url: str
    referral_code: str | None
    deadline_text: str | None


_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_EMAIL_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+")
_MANUAL_OPAQUE_SCHEMES = frozenset({"qr", "weixin", "wechat"})
_SNAPSHOT_FIELDS = (
    "company_name",
    "title",
    "description_text",
    "locations",
    "recruitment_types",
    "industries",
    "apply_url",
    "referral_code",
    "deadline_text",
    "gui_eligible",
)


def _normalized_text(value: str) -> str:
    return value.strip()


def _normalized_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalized_list(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if item and item not in seen:
            seen.add(item)
            normalized.append(item)
    return normalized


def _normalized_completion(values: JobCompletionInput) -> dict[str, object]:
    normalized = asdict(values)
    for key in ("company_name", "title", "description_text", "apply_url"):
        normalized[key] = _normalized_text(normalized[key])
    for key in ("referral_code", "deadline_text"):
        normalized[key] = _normalized_optional_text(normalized[key])
    for key in ("locations", "recruitment_types", "industries"):
        normalized[key] = _normalized_list(normalized[key])
    return normalized


def _split_application_channel(value: str) -> SplitResult | None:
    if not value or any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return None
    return parsed


def _valid_hostname(value: str | None) -> bool:
    if value is None:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    try:
        ascii_value = value.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if not ascii_value or len(ascii_value) > 253:
        return False
    return all(_HOST_LABEL.fullmatch(label) for label in ascii_value.split("."))


def _valid_email_address(value: str) -> bool:
    if len(value) > 254 or value.count("@") != 1:
        return False
    local, domain = value.rsplit("@", 1)
    if (
        not local
        or len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or _EMAIL_LOCAL.fullmatch(local) is None
    ):
        return False
    return _valid_hostname(domain)


def _valid_manual_opaque_channel(parsed: SplitResult) -> bool:
    return (
        parsed.scheme in _MANUAL_OPAQUE_SCHEMES
        and not parsed.netloc
        and bool(parsed.path)
        and not parsed.query
        and not parsed.fragment
    )


def _valid_application_channel(value: str, *, gui_eligible: bool) -> bool:
    parsed = _split_application_channel(value)
    if parsed is None:
        return False
    if parsed.scheme in {"http", "https"}:
        return bool(parsed.netloc) and _valid_hostname(parsed.hostname)
    if gui_eligible:
        return False
    if parsed.scheme == "mailto":
        return (
            not parsed.netloc
            and not parsed.query
            and not parsed.fragment
            and _valid_email_address(parsed.path)
        )
    return _valid_manual_opaque_channel(parsed)


Validator = Callable[[JobPosting], None]


class JobReviewService:
    def __init__(self, *, now: Callable[[], datetime] = utc_now) -> None:
        self._now = now

    def _locked(self, db: Session, *, job_id: str, expected_version: int) -> JobPosting:
        row = jobs.get_posting_for_review(db, job_id, lock=True)
        if row is None:
            raise JobNotFoundError(job_id)
        posting, _source = row
        if posting.review_version != expected_version:
            raise StaleJobReviewError(job_id)
        return posting

    def _transition(
        self,
        db: Session,
        *,
        job_id: str,
        actor_user_id: str,
        expected_version: int,
        allowed_from: Set[JobPostingStatus],
        to_status: JobPostingStatus,
        action: str,
        reason_code: str | None,
        updates: Mapping[str, object],
        gui_eligible: bool,
        validate: Validator,
    ) -> JobPosting:
        posting = self._locked(db, job_id=job_id, expected_version=expected_version)
        if posting.status not in allowed_from:
            raise InvalidJobReviewTransition(posting.status.value)
        validate(posting)
        transitioned_at = self._now()
        from_status = posting.status
        for key, value in updates.items():
            setattr(posting, key, value)
        posting.status = to_status
        posting.gui_eligible = (
            gui_eligible if to_status is JobPostingStatus.VERIFIED else False
        )
        self._set_terminal_timestamps(posting, to_status, transitioned_at)
        posting.review_version += 1
        db.add(
            JobVerification(
                job_id=posting.id,
                actor_user_id=actor_user_id,
                action=action,
                from_status=from_status.value,
                to_status=to_status.value,
                review_version=posting.review_version,
                field_snapshot=deepcopy(
                    {name: getattr(posting, name) for name in _SNAPSHOT_FIELDS}
                ),
                reason_code=reason_code,
                created_at=transitioned_at,
            )
        )
        db.flush()
        return posting

    @staticmethod
    def _set_terminal_timestamps(
        posting: JobPosting,
        to_status: JobPostingStatus,
        transitioned_at: datetime,
    ) -> None:
        if to_status is JobPostingStatus.PENDING_REVIEW:
            posting.verified_at = None
            posting.rejected_at = None
            posting.expired_at = None
        elif to_status is JobPostingStatus.VERIFIED:
            posting.verified_at = transitioned_at
            posting.rejected_at = None
            posting.expired_at = None
        elif to_status is JobPostingStatus.REJECTED:
            posting.verified_at = None
            posting.rejected_at = transitioned_at
            posting.expired_at = None
        elif to_status is JobPostingStatus.EXPIRED:
            posting.verified_at = posting.verified_at or transitioned_at
            posting.rejected_at = None
            posting.expired_at = transitioned_at

    def save_completion(
        self,
        db: Session,
        *,
        job_id: str,
        actor_user_id: str,
        expected_version: int,
        values: JobCompletionInput,
    ) -> JobPosting:
        normalized = _normalized_completion(values)

        def validate(_posting: JobPosting) -> None:
            for key in ("company_name", "title", "description_text", "apply_url"):
                if not normalized[key]:
                    raise IncompleteJobError(key)
            if not _valid_application_channel(
                normalized["apply_url"], gui_eligible=False
            ):
                raise IncompleteJobError("apply_url")

        return self._transition(
            db,
            job_id=job_id,
            actor_user_id=actor_user_id,
            expected_version=expected_version,
            allowed_from={
                JobPostingStatus.PENDING_COMPLETION,
                JobPostingStatus.PENDING_REVIEW,
                JobPostingStatus.REJECTED,
            },
            to_status=JobPostingStatus.PENDING_REVIEW,
            action="completion_saved",
            reason_code=None,
            updates={**normalized, "source_changed_since_review": False},
            gui_eligible=False,
            validate=validate,
        )

    def verify(
        self,
        db: Session,
        *,
        job_id: str,
        actor_user_id: str,
        expected_version: int,
        gui_eligible: bool,
    ) -> JobPosting:
        def validate(posting: JobPosting) -> None:
            if not all(
                (
                    (posting.company_name or "").strip(),
                    (posting.title or "").strip(),
                    (posting.description_text or "").strip(),
                )
            ):
                raise IncompleteJobError("required_fields")
            if not _valid_application_channel(
                posting.apply_url, gui_eligible=gui_eligible
            ):
                raise IncompleteJobError("apply_url")

        return self._transition(
            db,
            job_id=job_id,
            actor_user_id=actor_user_id,
            expected_version=expected_version,
            allowed_from={JobPostingStatus.PENDING_REVIEW},
            to_status=JobPostingStatus.VERIFIED,
            action="verified",
            reason_code=None,
            updates={},
            gui_eligible=gui_eligible,
            validate=validate,
        )

    def reject(
        self,
        db: Session,
        *,
        job_id: str,
        actor_user_id: str,
        expected_version: int,
        reason_code: str,
    ) -> JobPosting:
        normalized_reason = reason_code.strip()

        def validate(_posting: JobPosting) -> None:
            if normalized_reason not in REJECT_REASON_CODES:
                raise IncompleteJobError("reason_code")

        return self._transition(
            db,
            job_id=job_id,
            actor_user_id=actor_user_id,
            expected_version=expected_version,
            allowed_from={
                JobPostingStatus.PENDING_COMPLETION,
                JobPostingStatus.PENDING_REVIEW,
            },
            to_status=JobPostingStatus.REJECTED,
            action="rejected",
            reason_code=normalized_reason,
            updates={},
            gui_eligible=False,
            validate=validate,
        )

    def expire(
        self,
        db: Session,
        *,
        job_id: str,
        actor_user_id: str,
        expected_version: int,
        reason_code: str,
    ) -> JobPosting:
        normalized_reason = reason_code.strip()

        def validate(_posting: JobPosting) -> None:
            if normalized_reason not in EXPIRE_REASON_CODES:
                raise IncompleteJobError("reason_code")

        return self._transition(
            db,
            job_id=job_id,
            actor_user_id=actor_user_id,
            expected_version=expected_version,
            allowed_from={JobPostingStatus.VERIFIED},
            to_status=JobPostingStatus.EXPIRED,
            action="expired",
            reason_code=normalized_reason,
            updates={},
            gui_eligible=False,
            validate=validate,
        )
