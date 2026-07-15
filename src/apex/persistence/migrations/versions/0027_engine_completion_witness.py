"""Persist exact post-effect engine completion witnesses.

Revision ID: 0027
Revises: 0026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "engine_runs",
        sa.Column("execution_connection_version", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
    )
    op.add_column(
        "engine_runs",
        sa.Column("artifact_connection_version", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
    )
    op.add_column(
        "engine_runs",
        sa.Column("completion_kind", sa.String(length=32), nullable=True),
        schema="apex",
    )


def downgrade() -> None:
    op.drop_column("engine_runs", "completion_kind", schema="apex")
    op.drop_column("engine_runs", "artifact_connection_version", schema="apex")
    op.drop_column("engine_runs", "execution_connection_version", schema="apex")
