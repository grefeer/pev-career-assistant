"""add active_version_id to profiles

Revision ID: 20260806_0021
Revises: 20260805_0020
Create Date: 2026-08-06 00:00:00.000000

Add a nullable active_version_id column to profiles that points at the
confirmed profile version the PEV runtime should consume. SET NULL on
delete so removing a version leaves the profile intact (runtime falls back
to the latest version). Defaults to the latest version at creation time
and is switched by POST /profile-versions/{id}/activate.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260806_0021"
down_revision: Union[str, Sequence[str], None] = "20260805_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable active_version_id FK column to profiles."""
    op.add_column(
        "profiles",
        sa.Column("active_version_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_profiles_active_version",
        "profiles",
        "confirmed_profile_versions",
        ["active_version_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Drop active_version_id FK column from profiles."""
    op.drop_constraint("fk_profiles_active_version", "profiles", type_="foreignkey")
    op.drop_column("profiles", "active_version_id")
