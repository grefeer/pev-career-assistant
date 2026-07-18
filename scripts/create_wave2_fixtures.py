from __future__ import annotations

"""
Create Wave 2 test fixtures: one verified JobPosting and one ConfirmedProfileVersion.

Usage:
    python scripts/create_wave2_fixtures.py

This script is idempotent -- running it multiple times will not duplicate rows.
"""

# ruff: noqa: E402

import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select

from backend.app.db.base import utc_now
from backend.app.db.models import (
    ConfirmedProfileVersion,
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    JobVerification,
    Profile,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.db.session import SessionLocal

# ---------------------------------------------------------------------------
# Fixture constants
# ---------------------------------------------------------------------------

FIXTURE_ACCOUNT = "fixture-user"
FIXTURE_NICKNAME = "Fixture User"
FIXTURE_SOURCE_KEY = "wave2-fixture-source"

FIXTURE_JOB_RECORD_ID = "fixture-ext-001"
FIXTURE_COMPANY = "Fixture Corp"
FIXTURE_TITLE = "Software Engineer Intern (Fixtures)"


def _ensure_user(db) -> User:
    """Return an existing fixture user or create one."""
    user = db.scalar(select(User).where(User.account == FIXTURE_ACCOUNT))
    if user is not None:
        return user
    user = User(
        account=FIXTURE_ACCOUNT,
        nickname=FIXTURE_NICKNAME,
        password_hash="<fixture-only-no-auth>",
        role=UserRole.STUDENT,
        is_active=True,
    )
    db.add(user)
    db.flush()
    print(f"  Created user: {user.id}")
    return user


def _ensure_job_source(db) -> JobSource:
    """Return an existing fixture source or create one."""
    source = db.scalar(
        select(JobSource).where(JobSource.source_key == FIXTURE_SOURCE_KEY)
    )
    if source is not None:
        return source
    source = JobSource(
        source_key=FIXTURE_SOURCE_KEY,
        provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Wave 2 Fixture Source",
        file_id="fixture-file-id",
        sheet_id="fixture-sheet-id",
        mapper_version="fixture-v1",
        enabled=True,
    )
    db.add(source)
    db.flush()
    print(f"  Created JobSource: {source.id}")
    return source


def _ensure_raw_job_record(db, *, source: JobSource) -> RawJobRecord:
    """Return an existing fixture raw record or create one."""
    existing = db.scalar(
        select(RawJobRecord).where(
            RawJobRecord.source_id == source.id,
            RawJobRecord.external_record_id == FIXTURE_JOB_RECORD_ID,
        )
    )
    if existing is not None:
        return existing
    raw_fields = [
        {
            "company_name": FIXTURE_COMPANY,
            "title": FIXTURE_TITLE,
            "locations": ["Shanghai"],
            "recruitment_types": ["internship"],
            "industries": ["technology"],
            "apply_url": "https://example.com/apply",
        }
    ]
    payload_hash = hashlib.sha256(
        json.dumps(raw_fields, sort_keys=True).encode()
    ).hexdigest()
    record = RawJobRecord(
        source_id=source.id,
        external_record_id=FIXTURE_JOB_RECORD_ID,
        payload_hash=payload_hash,
        raw_fields=raw_fields,
        observed_at=utc_now(),
    )
    db.add(record)
    db.flush()
    print(f"  Created RawJobRecord: {record.id}")
    return record


def _ensure_verified_job_posting(
    db, *, source: JobSource, raw_record: RawJobRecord
) -> JobPosting:
    """Return an existing fixture posting (ensuring it is verified) or create one."""
    posting = db.scalar(
        select(JobPosting).where(
            JobPosting.source_id == source.id,
            JobPosting.external_record_id == raw_record.external_record_id,
        )
    )
    if posting is not None:
        if posting.status != JobPostingStatus.VERIFIED:
            posting.status = JobPostingStatus.VERIFIED
            posting.verified_at = utc_now()
            db.flush()
            print(f"  Updated JobPosting status to verified: {posting.id}")
        else:
            print(f"  Verified JobPosting already exists: {posting.id}")
        return posting

    now = utc_now()
    posting = JobPosting(
        source_id=source.id,
        external_record_id=raw_record.external_record_id,
        raw_record_id=raw_record.id,
        status=JobPostingStatus.VERIFIED,
        company_name=FIXTURE_COMPANY,
        title=FIXTURE_TITLE,
        description_text=(
            "This is a fixture job posting for Wave 2 development and testing. "
            "It provides a verified job that downstream matching, drafting, and "
            "snapshot pipelines can depend on."
        ),
        locations=["Shanghai"],
        recruitment_types=["internship"],
        industries=["technology"],
        apply_url="https://example.com/apply",
        referral_code=None,
        deadline_text=None,
        source_updated_at=now,
        mapper_version="fixture-v1",
        source_candidate={},
        source_changed_since_review=False,
        gui_eligible=False,
        review_version=1,
        verified_at=now,
    )
    db.add(posting)
    db.flush()
    print(f"  Created verified JobPosting: {posting.id}")
    return posting


def _ensure_job_verification(db, *, posting: JobPosting) -> JobVerification:
    """Return an existing verification event or create one for the fixture posting."""
    existing = db.scalar(
        select(JobVerification).where(JobVerification.job_id == posting.id)
    )
    if existing is not None:
        print(f"  JobVerification already exists: {existing.id}")
        return existing

    verification = JobVerification(
        job_id=posting.id,
        actor_user_id=None,
        action="verified",
        from_status="pending_review",
        to_status="verified",
        review_version=posting.review_version,
        field_snapshot={
            "company_name": posting.company_name,
            "title": posting.title,
            "description_text": posting.description_text,
            "locations": posting.locations,
            "recruitment_types": posting.recruitment_types,
            "industries": posting.industries,
            "apply_url": posting.apply_url,
            "gui_eligible": posting.gui_eligible,
        },
        reason_code=None,
    )
    db.add(verification)
    db.flush()
    print(f"  Created JobVerification: {verification.id}")
    return verification


def _ensure_profile(db, *, user: User) -> Profile:
    """Return an existing profile for the fixture user or create one."""
    profile = db.scalar(select(Profile).where(Profile.user_id == user.id))
    if profile is not None:
        return profile
    profile = Profile(
        user_id=user.id,
        version=1,
        local_sensitive_references={},
    )
    db.add(profile)
    db.flush()
    print(f"  Created Profile: {profile.id}")
    return profile


def _ensure_confirmed_profile_version(db, *, profile: Profile) -> ConfirmedProfileVersion:
    """Return an existing fixture confirmed version or create one."""
    existing = db.scalar(
        select(ConfirmedProfileVersion).where(
            ConfirmedProfileVersion.profile_id == profile.id,
            ConfirmedProfileVersion.version_number == 1,
        )
    )
    if existing is not None:
        print(f"  ConfirmedProfileVersion already exists: {existing.id}")
        return existing
    cpv = ConfirmedProfileVersion(
        profile_id=profile.id,
        version_number=1,
        aggregate_version=1,
        facts_snapshot={
            "name": "Fixture User",
            "email": "fixture@example.com",
            "school": "Fixture University",
            "major": "Computer Science",
        },
        evidence_refs={},
        local_sensitive_references={},
    )
    db.add(cpv)
    db.flush()
    print(f"  Created ConfirmedProfileVersion: {cpv.id}")
    return cpv


def main() -> int:
    db = SessionLocal()
    try:
        print("Creating Wave 2 test fixtures ...")

        user = _ensure_user(db)
        source = _ensure_job_source(db)
        raw_record = _ensure_raw_job_record(db, source=source)
        posting = _ensure_verified_job_posting(db, source=source, raw_record=raw_record)
        verification = _ensure_job_verification(db, posting=posting)
        profile = _ensure_profile(db, user=user)
        cpv = _ensure_confirmed_profile_version(db, profile=profile)

        db.commit()

        print()
        print("All fixtures created successfully.")
        print(f"  Verified JobPosting:           {posting.id}")
        print(f"  JobVerification:               {verification.id}")
        print(f"  ConfirmedProfileVersion:       {cpv.id}")
        return 0
    except Exception:
        db.rollback()
        print("Fixture creation failed.", file=sys.stderr)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
