"""application tracking records

Revision ID: 20260729_0016
Revises: 20260729_0015
Create Date: 2026-07-29 14:00:00.000000

Adds the ``application_records`` and ``application_record_events`` tables backing
the application-tracking skill.  ``application_records`` is the user-scoped,
hand-maintained tracker entry: the job saved/applied to, where it was found, and
how far it has progressed through the state machine
(saved -> applied -> screening -> interview -> offer / rejected / withdrawn).
``application_record_events`` is the append-only audit row written for each
explicit human status transition.

This is a non-agent skill - no crawl, no LLM, and crucially no auto-submit
(security gate #1): the platform never files an application on the user's
behalf.  ``target_job_id`` optionally links to a verified ``JobPosting`` and is
``SET NULL`` on job deletion (the company/title text is retained so off-platform
applications and historical records never orphan).  ``state_version`` is the
optimistic-lock guard for transitions (concurrent edits get a 409).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260729_0016"
down_revision: Union[str, Sequence[str], None] = "20260729_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create application_records and application_record_events."""
    op.create_table(
        "application_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_job_id",
            sa.String(length=36),
            sa.ForeignKey("job_postings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("apply_url", sa.String(length=1024), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "saved",
                "applied",
                "screening",
                "interview",
                "offer",
                "rejected",
                "withdrawn",
                name="application_record_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_application_records_user_status_created",
        "application_records",
        ["user_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_application_records_user_id",
        "application_records",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_application_records_target_job_id",
        "application_records",
        ["target_job_id"],
        unique=False,
    )
    op.create_index(
        "ix_application_records_status",
        "application_records",
        ["status"],
        unique=False,
    )

    op.create_table(
        "application_record_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "application_id",
            sa.String(length=36),
            sa.ForeignKey("application_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(length=40), nullable=False),
        sa.Column("to_status", sa.String(length=40), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_application_record_events_app_created",
        "application_record_events",
        ["application_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop application_records and application_record_events."""
    op.drop_index(
        "ix_application_record_events_app_created",
        table_name="application_record_events",
    )
    op.drop_table("application_record_events")
    op.drop_index(
        "ix_application_records_status", table_name="application_records"
    )
    op.drop_index(
        "ix_application_records_target_job_id", table_name="application_records"
    )
    op.drop_index(
        "ix_application_records_user_id", table_name="application_records"
    )
    op.drop_index(
        "ix_application_records_user_status_created",
        table_name="application_records",
    )
    op.drop_table("application_records")
