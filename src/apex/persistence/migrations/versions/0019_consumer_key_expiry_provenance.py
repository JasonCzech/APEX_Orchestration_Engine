"""Record whether a credential expiry is independent or legacy-ambiguous.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # The server default intentionally classifies writes from an old pod during
    # a rolling upgrade as ambiguous. New writers always provide provenance.
    op.add_column(
        "consumer_keys",
        sa.Column(
            "expiry_source",
            sa.String(length=32),
            nullable=True,
            server_default="legacy_ambiguous",
        ),
        schema="apex",
    )
    op.execute(
        """
        UPDATE apex.consumer_keys AS key
        SET expiry_source = CASE
            WHEN key.expires_at IS NULL THEN 'independent'
            WHEN consumer.expires_at IS NULL
              OR key.expires_at <> consumer.expires_at THEN 'explicit'
            WHEN COALESCE(consumer.rotation_count, 0) = 0 THEN 'inherited'
            ELSE 'legacy_ambiguous'
        END
        FROM apex.api_consumers AS consumer
        WHERE key.consumer_id = consumer.id
        """
    )
    op.alter_column(
        "consumer_keys",
        "expiry_source",
        existing_type=sa.String(length=32),
        existing_server_default="legacy_ambiguous",
        nullable=False,
        schema="apex",
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_column("consumer_keys", "expiry_source", schema="apex")
