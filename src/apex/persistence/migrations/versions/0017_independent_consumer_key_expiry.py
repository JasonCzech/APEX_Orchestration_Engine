"""Separate consumer lifetime from per-credential lifetime.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Remove expiries that older writers inherited from the consumer row.

    Consumer expiry remains enforced by authentication. For historical rows we
    can prove inheritance only when the consumer has never rotated; rotated rows
    may carry an operator-supplied deadline equal to the consumer deadline and
    are preserved conservatively. New writers no longer copy consumer expiry.
    """

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        UPDATE apex.consumer_keys AS key
        SET expires_at = NULL
        FROM apex.api_consumers AS consumer
        WHERE key.consumer_id = consumer.id
          AND consumer.expires_at IS NOT NULL
          AND key.expires_at = consumer.expires_at
          -- A rotated credential could have been given this exact deadline
          -- explicitly. Historical rows did not record expiry provenance, so
          -- only consumers that have never rotated are provably inherited.
          AND COALESCE(consumer.rotation_count, 0) = 0
        """
    )


def downgrade() -> None:
    """Restore the pre-0017 inherited-expiry representation."""

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        UPDATE apex.consumer_keys AS key
        SET expires_at = consumer.expires_at
        FROM apex.api_consumers AS consumer
        WHERE key.consumer_id = consumer.id
          AND consumer.expires_at IS NOT NULL
          AND key.expires_at IS NULL
        """
    )
