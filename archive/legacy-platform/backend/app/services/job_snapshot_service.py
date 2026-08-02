"""Builds VerifiedJobSnapshot from authoritative MySQL state."""
from datetime import datetime
from sqlalchemy.orm import Session
from backend.app.db.models import JobPosting, JobSourceLink, JobVerification



class VerifiedJobSnapshot:
    def __init__(
        self, *, job_id, company_name, title, description_text, locations,
        recruitment_types, industries, apply_url, gui_eligible,
        verified_at, review_version, source_links, job_verification_id,
    ):
        self.job_id = job_id
        self.company_name = company_name
        self.title = title
        self.description_text = description_text
        self.locations = locations
        self.recruitment_types = recruitment_types
        self.industries = industries
        self.apply_url = apply_url
        self.gui_eligible = gui_eligible
        self.verified_at = verified_at
        self.review_version = review_version
        self.source_links = source_links
        self.job_verification_id = job_verification_id


def build_verified_job_snapshot(db: Session, job_id: str) -> VerifiedJobSnapshot:
    job = db.query(JobPosting).filter(JobPosting.id == job_id).first()
    if job is None:
        raise ValueError("not_found")
    if job.status != "verified":
        raise ValueError("match_not_verified_job")

    source_links = [
        {"source_type": sl.source_type, "source_record_ref": sl.source_record_ref}
        for sl in db.query(JobSourceLink).filter(JobSourceLink.job_id == job_id).all()
    ]

    # Fetch the latest verification for this job
    latest_verification = (
        db.query(JobVerification)
        .filter(JobVerification.job_id == job_id)
        .order_by(JobVerification.created_at.desc())
        .first()
    )
    if latest_verification is None:
        raise ValueError("match_no_job_verification")

    return VerifiedJobSnapshot(
        job_id=job.id,
        company_name=job.company_name or "",
        title=job.title or "",
        description_text=job.description_text or "",
        locations=job.locations or [],
        recruitment_types=job.recruitment_types or [],
        industries=job.industries or [],
        apply_url=job.apply_url,
        gui_eligible=bool(job.gui_eligible),
        verified_at=job.verified_at or datetime.min,
        review_version=job.review_version or 0,
        source_links=source_links,
        job_verification_id=latest_verification.id,
    )
