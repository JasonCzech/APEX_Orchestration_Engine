"""usage events

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("consumer_name", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=True),
        sa.Column("surface", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=255), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "extra",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_events")),
        schema="apex",
    )
    op.create_index(op.f("ix_usage_events_at"), "usage_events", ["at"], schema="apex")
    op.create_index(
        "ix_usage_events_project_id_at", "usage_events", ["project_id", "at"], schema="apex"
    )


def downgrade() -> None:
    op.drop_index("ix_usage_events_project_id_at", table_name="usage_events", schema="apex")
    op.drop_index(op.f("ix_usage_events_at"), table_name="usage_events", schema="apex")
    op.drop_table("usage_events", schema="apex")
