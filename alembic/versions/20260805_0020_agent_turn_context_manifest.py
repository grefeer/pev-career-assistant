"""add context_manifest column to agent_turns

Revision ID: 20260805_0020
Revises: 20260801_0019
Create Date: 2026-08-05 00:00:00.000000

Add a nullable JSON context_manifest column to agent_turns for
per-decision observability (character counts only, no user data).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260805_0020"
down_revision: Union[str, Sequence[str], None] = "20260801_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable JSON context_manifest column to agent_turns."""
    op.add_column(
        "agent_turns",
        sa.Column("context_manifest", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """Drop context_manifest column from agent_turns."""
    op.drop_column("agent_turns", "context_manifest")
