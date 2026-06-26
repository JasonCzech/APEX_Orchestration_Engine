"""add audit event nonce

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_log",
        sa.Column("event_nonce", sa.String(length=32), nullable=True),
        schema="apex",
    )


def downgrade() -> None:
    op.drop_column("audit_log", "event_nonce", schema="apex")
