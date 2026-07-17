"""add talent profile and resume lifecycle

Revision ID: 20260717_0005
Revises: 20260716_0004
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260717_0005"
down_revision: Union[str, Sequence[str], None] = "20260716_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("local_sensitive_references", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_profiles_user_id"),
    )

    op.create_table(
        "resume_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(120), nullable=False),
        sa.Column("plaintext_size", sa.BigInteger(), nullable=False),
        sa.Column("plaintext_sha256", sa.String(64), nullable=False),
        sa.Column("encryption_version", sa.String(40), nullable=False),
        sa.Column(
            "status",
            sa.String(14),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("object_key", name="uq_resume_assets_object_key"),
        sa.Index("ix_resume_assets_profile_created", "profile_id", "created_at"),
        sa.CheckConstraint(
            "status IN ('pending_upload','ready','upload_failed')",
            name="resume_asset_status",
        ),
    )

    op.create_table(
        "resume_imports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_id", sa.String(36), sa.ForeignKey("resume_assets.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("parser_version", sa.String(40), nullable=False),
        sa.Column(
            "status",
            sa.String(21),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Index("ix_resume_imports_profile_created", "profile_id", "created_at"),
        sa.CheckConstraint(
            "status IN ('pending','parsing','awaiting_confirmation','needs_manual_entry','failed')",
            name="resume_import_status",
        ),
    )

    op.create_table(
        "profile_field_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resume_import_id", sa.String(36), sa.ForeignKey("resume_imports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field_path", sa.String(255), nullable=False),
        sa.Column("candidate_value", sa.JSON(), nullable=False),
        sa.Column("evidence_excerpt", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Index("ix_evidence_profile_field_created", "profile_id", "field_path", "created_at"),
        sa.UniqueConstraint("resume_import_id", "sequence", name="uq_evidence_import_sequence"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_profile_field_evidence_confidence",
        ),
    )

    op.create_table(
        "profile_field_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence_id", sa.String(36), sa.ForeignKey("profile_field_evidence.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_user_id", sa.String(36), nullable=False),
        sa.Column(
            "action",
            sa.String(7),
            nullable=False,
        ),
        sa.Column("resolved_value", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Index("ix_decisions_evidence_created", "evidence_id", "created_at"),
        sa.CheckConstraint(
            "action IN ('confirm','correct','ignore')",
            name="profile_evidence_decision_action",
        ),
    )

    op.create_table(
        "confirmed_profile_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("facts_snapshot", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("local_sensitive_references", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("profile_id", "version_number", name="uq_confirmed_version_number"),
        sa.Index("ix_confirmed_versions_profile_created", "profile_id", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("confirmed_profile_versions")
    op.drop_table("profile_field_decisions")
    op.drop_table("profile_field_evidence")
    op.drop_table("resume_imports")
    op.drop_table("resume_assets")
    op.drop_table("profiles")
