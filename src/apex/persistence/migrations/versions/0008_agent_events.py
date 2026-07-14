"""agent behavior events

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_index(name: str, columns: list[str]) -> None:
    op.drop_index(
        name,
        table_name="agent_events",
        schema="apex",
        if_exists=True,
    )
    op.create_index(
        name,
        "agent_events",
        columns,
        schema="apex",
    )


def _drop_index(name: str) -> None:
    op.drop_index(
        name,
        table_name="agent_events",
        schema="apex",
        if_exists=True,
    )


def upgrade() -> None:
    op.create_table(
        "agent_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("project_id", sa.String(length=255), nullable=True),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("total_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("cache_creation_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reasoning_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "extra",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_events")),
        schema="apex",
        if_not_exists=True,
    )
    _create_index(op.f("ix_agent_events_at"), ["at"])
    _create_index("ix_agent_events_project_id_at", ["project_id", "at"])
    _create_index("ix_agent_events_phase_at", ["phase", "at"])
    _create_index("ix_agent_events_model_at", ["model", "at"])
    _create_index("ix_agent_events_thread_id", ["thread_id"])


def downgrade() -> None:
    _drop_index("ix_agent_events_thread_id")
    _drop_index("ix_agent_events_model_at")
    _drop_index("ix_agent_events_phase_at")
    _drop_index("ix_agent_events_project_id_at")
    _drop_index(op.f("ix_agent_events_at"))
    op.drop_table("agent_events", schema="apex", if_exists=True)
