"""engine runs projection

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "engine_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("engine", sa.String(length=64), nullable=False),
        sa.Column("external_run_id", sa.String(length=255), nullable=True),
        sa.Column(
            "handle",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "summary",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_engine_runs")),
        sa.UniqueConstraint("thread_id", "attempt", name=op.f("uq_engine_runs_thread_id")),
        schema="apex",
    )


def downgrade() -> None:
    op.drop_table("engine_runs", schema="apex")
