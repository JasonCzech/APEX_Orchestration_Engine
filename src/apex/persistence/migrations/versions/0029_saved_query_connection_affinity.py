"""Pin project-scoped saved queries to one work-tracking connection.

Revision ID: 0029
Revises: 0028
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Historical default selection cannot be reconstructed safely. Existing
    # scoped rows remain NULL and are quarantined by the API until an operator
    # explicitly rebinds them; global rows are intentionally reusable templates.
    op.add_column(
        "saved_queries",
        sa.Column("connection_id", sa.String(length=32), nullable=True),
        schema="apex",
    )
    op.create_index(
        "ix_saved_queries_connection_id",
        "saved_queries",
        ["connection_id"],
        unique=False,
        schema="apex",
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM apex.saved_queries
                    WHERE connection_id IS NOT NULL
                ) THEN
                    RAISE EXCEPTION
                        'cannot downgrade with bound saved queries present; '
                        'delete or explicitly archive them first';
                END IF;
            END $$;
            """
        )
    op.drop_index(
        "ix_saved_queries_connection_id",
        table_name="saved_queries",
        schema="apex",
    )
    op.drop_column("saved_queries", "connection_id", schema="apex")
