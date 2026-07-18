"""create site_adapters table and extend application_tasks with adapter columns

Revision ID: 20260718_0009
Revises: 7b757ef17d3f
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260718_0009"
down_revision: Union[str, Sequence[str], None] = "7b757ef17d3f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. site_adapters ---
    op.create_table(
        "site_adapters",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("adapter_id", sa.String(64), nullable=False, unique=True),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("supported_domains", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "circuit_breaker_open",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("UTC_TIMESTAMP()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("UTC_TIMESTAMP() ON UPDATE UTC_TIMESTAMP()"),
        ),
    )

    # --- 2. application_tasks: adapter columns ---
    op.add_column(
        "application_tasks",
        sa.Column("adapter_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "application_tasks",
        sa.Column("adapter_version", sa.String(16), nullable=True),
    )
    op.add_column(
        "application_tasks",
        sa.Column(
            "adapter_status_at_dispatch", sa.String(24), nullable=True
        ),
    )

    site_adapters = sa.table(
        "site_adapters",
        sa.column("id", sa.String),
        sa.column("adapter_id", sa.String),
        sa.column("version", sa.String),
        sa.column("supported_domains", sa.JSON),
        sa.column("status", sa.String),
    )
    op.bulk_insert(
        site_adapters,
        [
            {
                "id": "00000000-0000-4000-8000-000000000901",
                "adapter_id": "moka.dji",
                "version": "1.0.0",
                "supported_domains": ["moka.com", "mokahr.com", "zhaopin.dji.com"],
                "status": "active",
            },
            {
                "id": "00000000-0000-4000-8000-000000000902",
                "adapter_id": "xpeng.feishu",
                "version": "1.0.0",
                "supported_domains": ["feishu.cn"],
                "status": "active",
            },
            {
                "id": "00000000-0000-4000-8000-000000000903",
                "adapter_id": "iflytek.zhiye",
                "version": "1.0.0",
                "supported_domains": ["zhiye.com"],
                "status": "active",
            },
        ],
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM site_adapters WHERE adapter_id IN "
        "('moka.dji', 'xpeng.feishu', 'iflytek.zhiye')"
    )
    op.drop_column("application_tasks", "adapter_status_at_dispatch")
    op.drop_column("application_tasks", "adapter_version")
    op.drop_column("application_tasks", "adapter_id")
    op.drop_table("site_adapters")
