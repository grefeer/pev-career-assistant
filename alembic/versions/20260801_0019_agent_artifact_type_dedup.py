"""deduplicate artifacts by type as well as source content

Revision ID: 20260801_0019
Revises: 20260801_0018
Create Date: 2026-08-01 23:20:00.000000

One executor step may preserve both a public page snapshot and the structured
JD derived from it.  They intentionally share a content hash but are separate,
immutable artifact products.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260801_0019"
down_revision: Union[str, Sequence[str], None] = "20260801_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Allow one artifact of each type for a source snapshot in a step."""
    # MySQL may use the old unique index as the supporting index for the
    # ``step_id`` foreign key. Create an explicit non-unique support index
    # before replacing that uniqueness invariant.
    op.create_index(
        "ix_agent_artifacts_step_id",
        "agent_artifacts",
        ["step_id"],
        unique=False,
    )
    op.drop_constraint(
        "uq_agent_artifacts_step_content_hash",
        "agent_artifacts",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_agent_artifacts_step_type_content_hash",
        "agent_artifacts",
        ["step_id", "artifact_type", "content_hash"],
    )


def downgrade() -> None:
    """Restore the original single-artifact-per-source invariant."""
    op.drop_constraint(
        "uq_agent_artifacts_step_type_content_hash",
        "agent_artifacts",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_agent_artifacts_step_content_hash",
        "agent_artifacts",
        ["step_id", "content_hash"],
    )
    op.drop_index("ix_agent_artifacts_step_id", table_name="agent_artifacts")
