"""Separate adapter generation from connection metadata timestamps.

Revision ID: 0026
Revises: 0025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connections",
        sa.Column(
            "runtime_version",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        schema="apex",
    )
    # Preserve every checkpointed pre-upgrade generation exactly. A fresh
    # server timestamp would make all in-flight engine/work-item reservations
    # look stale immediately after deployment.
    op.execute("UPDATE apex.connections SET runtime_version = updated_at")
    op.alter_column(
        "connections",
        "runtime_version",
        nullable=False,
        existing_type=sa.DateTime(timezone=True),
        schema="apex",
    )


def downgrade() -> None:
    # A downgrade restores the historical dual-purpose timestamp. Preserve the
    # semantic generation so existing durable reservations remain usable.
    op.execute(
        "UPDATE apex.connections SET updated_at = runtime_version WHERE runtime_version IS NOT NULL"
    )
    op.drop_column("connections", "runtime_version", schema="apex")
