"""fix_match_report_status_check — add pending/running to status CHECK constraint

The original ck_match_reports_status only allowed 'completed' and 'failed',
but the MatchReport model defaults to 'pending' and transitions through
'running' during execution. This migration drops the old constraint and
re-creates it with all four valid states.

Revision ID: 7b757ef17d3f
Revises: 20260718_0008
Create Date: 2026-07-17 16:30:17.963683
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7b757ef17d3f"
down_revision: Union[str, Sequence[str], None] = "20260718_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _check_constraint_name():
    """Return the CHECK constraint name (same in both PG and SQLite)."""
    return "ck_match_reports_status"


def upgrade() -> None:
    """Upgrade schema."""
    # Drop the old constraint and re-create with all 4 values.
    # Use batch_alter_table so SQLite recreates the table under the hood.
    with op.batch_alter_table("match_reports") as batch_op:
        batch_op.drop_constraint(
            _check_constraint_name(), type_="check"
        )
        batch_op.create_check_constraint(
            _check_constraint_name(),
            "status IN ('pending','running','completed','failed')",
        )


def downgrade() -> None:
    """Downgrade schema — restore the original 2-value constraint."""
    with op.batch_alter_table("match_reports") as batch_op:
        batch_op.drop_constraint(
            _check_constraint_name(), type_="check"
        )
        batch_op.create_check_constraint(
            _check_constraint_name(),
            "status IN ('completed','failed')",
        )
