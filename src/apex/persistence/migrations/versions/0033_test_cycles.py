"""Add the opt-in test cycle aggregate without changing pipeline history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "test_cycles",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("app_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("environment_id", sa.String(255), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("independent_review", sa.Boolean(), nullable=False),
        sa.Column("versions", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="apex",
    )
    op.create_index("ix_apex_test_cycles_project_id", "test_cycles", ["project_id"], schema="apex")


def downgrade() -> None:
    op.drop_table("test_cycles", schema="apex")
