"""extend site_adapters with rollout fields and create observed_sites table

Revision ID: 20260718_0010
Revises: 20260718_0009
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260718_0010"
down_revision: Union[str, Sequence[str], None] = "20260718_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. site_adapters: add rollout tracking columns ---
    op.add_column(
        "site_adapters",
        sa.Column(
            "rollout_stage", sa.String(24), nullable=False, server_default="readonly"
        ),
    )
    op.add_column(
        "site_adapters",
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "site_adapters",
        sa.Column("last_readonly_verified_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- 2. application_tasks: add error tracking columns ---
    op.add_column(
        "application_tasks",
        sa.Column(
            "site_adapter_error_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "application_tasks",
        sa.Column("site_adapter_last_error", sa.String(256), nullable=True),
    )

    # --- 3. observed_sites table ---
    op.create_table(
        "observed_sites",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("site_code", sa.String(32), nullable=False, unique=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("domains", sa.JSON(), nullable=False),
        sa.Column("page_samples_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="observing"
        ),
        sa.Column(
            "adapter_kind", sa.String(16), nullable=False, server_default="observation"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("observed_sites")
    op.drop_column("application_tasks", "site_adapter_last_error")
    op.drop_column("application_tasks", "site_adapter_error_count")
    op.drop_column("site_adapters", "last_readonly_verified_at")
    op.drop_column("site_adapters", "last_success_at")
    op.drop_column("site_adapters", "rollout_stage")
