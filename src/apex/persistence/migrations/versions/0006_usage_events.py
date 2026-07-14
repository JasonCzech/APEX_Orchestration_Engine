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


def _create_index(name: str, columns: list[str]) -> None:
    # A failed historical CREATE INDEX CONCURRENTLY may have left an invalid
    # catalog entry. Drop/rebuild inside this revision's transaction on retry.
    op.drop_index(
        name,
        table_name="usage_events",
        schema="apex",
        if_exists=True,
    )
    op.create_index(
        name,
        "usage_events",
        columns,
        schema="apex",
    )


def _drop_index(name: str) -> None:
    op.drop_index(
        name,
        table_name="usage_events",
        schema="apex",
        if_exists=True,
    )


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
        if_not_exists=True,
    )
    _create_index(op.f("ix_usage_events_at"), ["at"])
    _create_index("ix_usage_events_project_id_at", ["project_id", "at"])


def downgrade() -> None:
    _drop_index("ix_usage_events_project_id_at")
    _drop_index(op.f("ix_usage_events_at"))
    op.drop_table("usage_events", schema="apex", if_exists=True)
