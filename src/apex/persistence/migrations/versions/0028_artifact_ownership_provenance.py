"""Quarantine artifact and run rows with ambiguous application ownership.

Revision ID: 0028
Revises: 0027
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_provenance_column(table_name: str) -> None:
    # Start false so every row written by an older application image is
    # quarantined while the DDL runs. Only exact app-owned rows can be proven
    # from the relational data already present.
    op.add_column(
        table_name,
        sa.Column(
            "ownership_known",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        schema="apex",
    )
    op.execute(
        sa.text(
            f"""
            UPDATE apex.{table_name}
            SET ownership_known = true
            WHERE project_id IS NOT NULL
              AND app_id IS NOT NULL
            """
        )
    )
    # Keep the database default false. During a rolling upgrade, older pods do
    # not know this column and may continue writing after the pre-upgrade
    # migration. Only the new application writes true explicitly and atomically.


def upgrade() -> None:
    # The buggy projection window stamped app-less rows as known. There is no
    # trustworthy way to distinguish an intentional project-level run from a
    # selected application whose id was dropped, so quarantine all of them.
    op.execute(
        sa.text(
            """
            UPDATE apex.engine_runs
            SET ownership_known = false
            WHERE app_id IS NULL
            """
        )
    )
    op.add_column(
        "engine_runs",
        sa.Column(
            "scope_ownership_known",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        schema="apex",
    )
    op.execute(
        sa.text(
            """
            UPDATE apex.engine_runs
            SET scope_ownership_known = true
            WHERE ownership_known = true
              AND project_id IS NOT NULL
              AND app_id IS NOT NULL
            """
        )
    )
    _add_provenance_column("artifact_references")
    # 0021 intentionally creates the byte-bearing outbox only on PostgreSQL.
    if op.get_bind().dialect.name == "postgresql":
        _add_provenance_column("artifact_upload_intents")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_column("artifact_upload_intents", "ownership_known", schema="apex")
    op.drop_column("artifact_references", "ownership_known", schema="apex")
    op.drop_column("engine_runs", "scope_ownership_known", schema="apex")
    # Engine-run quarantine is intentionally irreversible. Reclassifying an
    # ambiguous row as known during rollback would recreate the cross-app leak.
