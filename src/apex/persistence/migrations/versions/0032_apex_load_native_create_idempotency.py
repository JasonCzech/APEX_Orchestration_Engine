"""Persist APEX Load native resource create idempotency mappings.

Revision ID: 0032
Revises: 0031
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table_name in ("apex_load_native_scenarios", "apex_load_native_schedules"):
        op.add_column(
            table_name,
            sa.Column("idempotency_fingerprint", sa.String(length=64), nullable=True),
            schema="apex",
        )
    op.create_unique_constraint(
        "uq_apex_load_native_scenario_idempotency",
        "apex_load_native_scenarios",
        ["connection_id", "connection_version", "remote_project_id", "idempotency_key"],
        schema="apex",
    )
    op.create_unique_constraint(
        "uq_apex_load_native_schedule_idempotency",
        "apex_load_native_schedules",
        [
            "connection_id",
            "connection_version",
            "remote_project_id",
            "scenario_remote_id",
            "idempotency_key",
        ],
        schema="apex",
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_apex_load_native_schedule_idempotency",
        "apex_load_native_schedules",
        schema="apex",
        type_="unique",
    )
    op.drop_constraint(
        "uq_apex_load_native_scenario_idempotency",
        "apex_load_native_scenarios",
        schema="apex",
        type_="unique",
    )
    for table_name in ("apex_load_native_schedules", "apex_load_native_scenarios"):
        op.drop_column(table_name, "idempotency_fingerprint", schema="apex")
