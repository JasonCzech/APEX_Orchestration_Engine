"""backfill consumer key rows

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        INSERT INTO apex.consumer_keys (
            id,
            consumer_id,
            key_hash,
            created_at,
            expires_at,
            revoked_at,
            last_used_at,
            created_by
        )
        SELECT
            md5(c.id || ':' || c.key_hash),
            c.id,
            c.key_hash,
            c.created_at,
            c.expires_at,
            c.revoked_at,
            c.last_used_at,
            c.created_by
        FROM apex.api_consumers AS c
        WHERE c.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM apex.consumer_keys AS k
              WHERE k.consumer_id = c.id
                AND k.key_hash = c.key_hash
          )
        ON CONFLICT (key_hash) DO NOTHING
        """
    )


def downgrade() -> None:
    return
