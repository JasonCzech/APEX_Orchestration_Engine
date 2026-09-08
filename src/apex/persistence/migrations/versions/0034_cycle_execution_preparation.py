"""Preserve execution preparation and references to existing engine attempts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cycle_execution_preparations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.String(32),
            sa.ForeignKey("apex.test_cycles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(32), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("cycle_id", "idempotency_key"),
        schema="apex",
    )
    op.create_index(
        "ix_apex_cycle_execution_preparations_cycle_id",
        "cycle_execution_preparations",
        ["cycle_id"],
        schema="apex",
    )
    op.create_table(
        "cycle_run_links",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.String(32),
            sa.ForeignKey("apex.test_cycles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "engine_run_id",
            sa.String(32),
            sa.ForeignKey("apex.engine_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("cycle_id", "engine_run_id"),
        schema="apex",
    )
    op.create_index(
        "ix_apex_cycle_run_links_cycle_id", "cycle_run_links", ["cycle_id"], schema="apex"
    )


def downgrade() -> None:
    op.drop_table("cycle_run_links", schema="apex")
    op.drop_table("cycle_execution_preparations", schema="apex")
