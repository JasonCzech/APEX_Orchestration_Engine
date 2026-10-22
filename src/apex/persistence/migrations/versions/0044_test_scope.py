"""Durable cross-project run participants."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_scope_bindings",
        sa.Column("thread_id", sa.String(64), primary_key=True),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("participant_count", sa.Integer(), nullable=False),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        schema="apex",
    )
    op.create_table(
        "run_participants",
        sa.Column("thread_id", sa.String(64), primary_key=True),
        sa.Column("participant_id", sa.String(256), primary_key=True),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("application_id", sa.String(255), nullable=False),
        sa.Column("environment_id", sa.String(256), nullable=False),
        sa.Column("layer", sa.String(16), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("is_traffic_entry", sa.Boolean(), nullable=False),
        schema="apex",
    )
    op.create_index(
        "ix_run_participants_audience",
        "run_participants",
        ["project_id", "application_id", "thread_id"],
        schema="apex",
    )


def downgrade():
    op.drop_table("run_participants", schema="apex")
    op.drop_table("run_scope_bindings", schema="apex")
