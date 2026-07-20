"""add strategy and trajectory tables

Revision ID: ffc4f5917966
Revises: 7e8f22313271
Create Date: 2026-07-20 13:00:04.921909
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = 'ffc4f5917966'
down_revision: Union[str, Sequence[str], None] = '7e8f22313271'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('job_discovery_strategies',
    sa.Column('url_pattern', sa.String(length=500), nullable=False),
    sa.Column('site_type', sa.String(length=50), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('adapter', sa.String(length=500), nullable=True),
    sa.Column('plan_yaml', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('total_runs', sa.Integer(), nullable=False),
    sa.Column('success_runs', sa.Integer(), nullable=False),
    sa.Column('fallback_runs', sa.Integer(), nullable=False),
    sa.Column('error_count', sa.Integer(), nullable=False),
    sa.Column('consecutive_ok', sa.Integer(), nullable=False),
    sa.Column('last_error_tool', sa.String(length=100), nullable=True),
    sa.Column('last_error_reason', sa.String(length=50), nullable=True),
    sa.Column('last_error_message', sa.Text(), nullable=True),
    sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('success_count', sa.Integer(), nullable=False),
    sa.Column('avg_duration_s', sa.Float(), nullable=True),
    sa.Column('degradation_threshold', sa.Integer(), nullable=False),
    sa.Column('recovery_threshold', sa.Integer(), nullable=False),
    sa.Column('last_health_check_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_job_discovery_strategies_pattern', 'job_discovery_strategies', ['url_pattern'], unique=False)
    op.create_index(op.f('ix_job_discovery_strategies_site_type'), 'job_discovery_strategies', ['site_type'], unique=False)
    op.create_index('ix_job_discovery_strategies_status', 'job_discovery_strategies', ['status'], unique=False)
    op.create_table('job_discovery_trajectories',
    sa.Column('task_id', sa.String(length=36), nullable=True),
    sa.Column('strategy_id', sa.String(length=36), nullable=True),
    sa.Column('executor_type', sa.String(length=20), nullable=False),
    sa.Column('overall_status', sa.String(length=30), nullable=False),
    sa.Column('failed_at_step', sa.Integer(), nullable=True),
    sa.Column('failed_tool', sa.String(length=100), nullable=True),
    sa.Column('failed_params', sa.JSON(), nullable=True),
    sa.Column('failed_error_type', sa.String(length=100), nullable=True),
    sa.Column('failed_error_message', sa.Text(), nullable=True),
    sa.Column('failed_error_reason', sa.String(length=50), nullable=True),
    sa.Column('completed_steps', sa.JSON(), nullable=True),
    sa.Column('fallback_trace', sa.JSON(), nullable=True),
    sa.Column('clean_path', sa.JSON(), nullable=True),
    sa.Column('annotations', sa.JSON(), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('url_pattern', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['strategy_id'], ['job_discovery_strategies.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['job_discovery_tasks.id'], name='fk_job_discovery_trajectories_task_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_job_discovery_trajectories_created', 'job_discovery_trajectories', ['created_at'], unique=False)
    op.create_index('ix_job_discovery_trajectories_url_pattern', 'job_discovery_trajectories', ['url_pattern'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_job_discovery_trajectories_url_pattern', table_name='job_discovery_trajectories')
    op.drop_index('ix_job_discovery_trajectories_created', table_name='job_discovery_trajectories')
    op.drop_table('job_discovery_trajectories')
    op.drop_index('ix_job_discovery_strategies_status', table_name='job_discovery_strategies')
    op.drop_index(op.f('ix_job_discovery_strategies_site_type'), table_name='job_discovery_strategies')
    op.drop_index('ix_job_discovery_strategies_pattern', table_name='job_discovery_strategies')
    op.drop_table('job_discovery_strategies')
