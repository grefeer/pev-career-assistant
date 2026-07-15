"""expire and rotate device credentials

Revision ID: 20260715_0002
Revises: 20260714_0001
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0002"
down_revision: Union[str, Sequence[str], None] = "20260714_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "devices",
        sa.Column("credential_rotated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE devices SET expires_at = DATE_ADD(COALESCE(paired_at, UTC_TIMESTAMP()), INTERVAL 90 DAY) WHERE expires_at IS NULL"
    )
    op.alter_column(
        "devices",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.create_index(
        op.f("ix_devices_expires_at"), "devices", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_devices_expires_at"), table_name="devices")
    op.drop_column("devices", "credential_rotated_at")
    op.drop_column("devices", "expires_at")
