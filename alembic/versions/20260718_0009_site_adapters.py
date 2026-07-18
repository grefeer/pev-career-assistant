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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
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

    op.execute(
        """
        INSERT INTO site_adapters (
            id, adapter_id, version, supported_domains, status, created_at, updated_at
        ) VALUES
            (
                '00000000-0000-4000-8000-000000000901',
                'moka.dji',
                '1.0.0',
                JSON_ARRAY('moka.com', 'mokahr.com', 'zhaopin.dji.com'),
                'active',
                UTC_TIMESTAMP(),
                UTC_TIMESTAMP()
            ),
            (
                '00000000-0000-4000-8000-000000000902',
                'xpeng.feishu',
                '1.0.0',
                JSON_ARRAY('feishu.cn'),
                'active',
                UTC_TIMESTAMP(),
                UTC_TIMESTAMP()
            ),
            (
                '00000000-0000-4000-8000-000000000903',
                'iflytek.zhiye',
                '1.0.0',
                JSON_ARRAY('zhiye.com'),
                'active',
                UTC_TIMESTAMP(),
                UTC_TIMESTAMP()
            )
        """
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
