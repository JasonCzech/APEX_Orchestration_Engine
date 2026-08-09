"""Add durable APEX Load connection reconciliation leases.

Revision ID: 0031
Revises: 0030
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "apex_load_native_reconcile_leases",
        sa.Column("connection_id", sa.String(length=32), nullable=False),
        sa.Column("connection_version", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["connection_id"], ["apex.connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("connection_id"),
        schema="apex",
    )
    op.create_index(
        "ix_apex_load_native_reconcile_leases_ready",
        "apex_load_native_reconcile_leases",
        ["next_poll_at", "claimed_at"],
        schema="apex",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_apex_load_native_reconcile_leases_ready",
        table_name="apex_load_native_reconcile_leases",
        schema="apex",
    )
    op.drop_table("apex_load_native_reconcile_leases", schema="apex")
