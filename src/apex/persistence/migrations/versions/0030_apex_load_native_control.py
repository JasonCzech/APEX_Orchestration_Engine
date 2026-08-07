"""Persist APEX Load native control-plane mirror and launch ledger.

Revision ID: 0030
Revises: 0029
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common_columns() -> list[Any]:
    return [
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("connection_id", sa.String(length=32), nullable=False),
        sa.Column("connection_version", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("remote_project_id", sa.String(length=255), nullable=False),
        sa.Column("remote_id", sa.String(length=255), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["connection_id"], ["apex.connections.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id", "connection_version", "remote_project_id", "remote_id"
        ),
    ]


def upgrade() -> None:
    scenario_columns = _common_columns()
    op.create_table("apex_load_native_scenarios", *scenario_columns, schema="apex")
    op.create_index(
        "ix_apex_load_native_scenarios_project_seen",
        "apex_load_native_scenarios",
        ["project_id", "last_seen_at"],
        schema="apex",
    )

    schedule_columns = _common_columns()
    schedule_columns.insert(
        6, sa.Column("scenario_remote_id", sa.String(length=255), nullable=False)
    )
    op.create_table("apex_load_native_schedules", *schedule_columns, schema="apex")
    op.create_index(
        "ix_apex_load_native_schedules_project_seen",
        "apex_load_native_schedules",
        ["project_id", "last_seen_at"],
        schema="apex",
    )

    op.create_table(
        "apex_load_native_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("connection_id", sa.String(length=32), nullable=False),
        sa.Column("connection_version", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("remote_project_id", sa.String(length=255), nullable=False),
        sa.Column("remote_id", sa.String(length=255), nullable=False),
        sa.Column("scenario_remote_id", sa.String(length=255), nullable=True),
        sa.Column("schedule_remote_id", sa.String(length=255), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("terminal", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "pipeline_status", sa.String(length=32), nullable=False, server_default="pending"
        ),
        sa.Column("pipeline_thread_id", sa.String(length=255), nullable=True),
        sa.Column("pipeline_run_id", sa.String(length=255), nullable=True),
        sa.Column("pipeline_launch_error", sa.String(length=255), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["connection_id"], ["apex.connections.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id", "connection_version", "remote_project_id", "remote_id"
        ),
        schema="apex",
    )
    op.create_index(
        "ix_apex_load_native_runs_project_started",
        "apex_load_native_runs",
        ["project_id", "started_at"],
        schema="apex",
    )
    op.create_index(
        "ix_apex_load_native_runs_launch",
        "apex_load_native_runs",
        ["terminal", "pipeline_status", "claimed_at"],
        schema="apex",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_apex_load_native_runs_launch",
        table_name="apex_load_native_runs",
        schema="apex",
    )
    op.drop_index(
        "ix_apex_load_native_runs_project_started",
        table_name="apex_load_native_runs",
        schema="apex",
    )
    op.drop_table("apex_load_native_runs", schema="apex")
    op.drop_index(
        "ix_apex_load_native_schedules_project_seen",
        table_name="apex_load_native_schedules",
        schema="apex",
    )
    op.drop_table("apex_load_native_schedules", schema="apex")
    op.drop_index(
        "ix_apex_load_native_scenarios_project_seen",
        table_name="apex_load_native_scenarios",
        schema="apex",
    )
    op.drop_table("apex_load_native_scenarios", schema="apex")
