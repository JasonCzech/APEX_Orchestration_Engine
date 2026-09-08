"""Durable provider validation attempts for immutable execution preparations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cycle_script_validations",
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
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("issues", postgresql.JSONB(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("cycle_id", "request_key"),
        schema="apex",
    )
    op.create_index(
        "ix_apex_cycle_script_validations_cycle_id",
        "cycle_script_validations",
        ["cycle_id"],
        schema="apex",
    )


def downgrade() -> None:
    op.drop_table("cycle_script_validations", schema="apex")
