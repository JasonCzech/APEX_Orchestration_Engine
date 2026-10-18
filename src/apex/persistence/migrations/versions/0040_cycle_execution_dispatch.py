"""Reserve exact, replayable cycle launches and serialize their environment use."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cycle_execution_dispatches",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.String(32),
            sa.ForeignKey("apex.test_cycles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "preparation_id",
            sa.String(32),
            sa.ForeignKey("apex.cycle_execution_preparations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_key", sa.String(32), nullable=False),
        sa.Column("active_environment_id", sa.String(255)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("pipeline_config", postgresql.JSONB(), nullable=False),
        sa.Column("thread_id", sa.String(64)),
        sa.Column("run_id", sa.String(64)),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("canceled_by", sa.String(255)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("preparation_id"),
        sa.UniqueConstraint("cycle_id", "request_key"),
        sa.UniqueConstraint("active_environment_id"),
        sa.UniqueConstraint("thread_id"),
        schema="apex",
    )
    op.create_index(
        "ix_apex_cycle_execution_dispatches_cycle_id",
        "cycle_execution_dispatches",
        ["cycle_id"],
        schema="apex",
    )


def downgrade() -> None:
    op.drop_table("cycle_execution_dispatches", schema="apex")
