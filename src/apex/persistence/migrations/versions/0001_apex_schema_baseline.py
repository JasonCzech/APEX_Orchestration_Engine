"""apex schema baseline

Revision ID: 0001
Revises:
Create Date: 2026-06-11

"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Schema creation happens in migrations/env.py (CREATE SCHEMA IF NOT EXISTS apex)
    # before the version table is written; this baseline pins the revision chain.
    pass


def downgrade() -> None:
    pass
