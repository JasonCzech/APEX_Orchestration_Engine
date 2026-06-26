"""auth audit log and api consumer lifecycle

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-25
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _json_type() -> sa.TypeEngine[Any]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _create_index(name: str, table_name: str, columns: list[str]) -> None:
    kwargs: dict[str, Any] = {"schema": "apex"}
    if _is_postgres():
        kwargs["postgresql_concurrently"] = True
        with op.get_context().autocommit_block():
            op.create_index(name, table_name, columns, **kwargs)
        return
    op.create_index(name, table_name, columns, **kwargs)


def _drop_index(name: str, table_name: str) -> None:
    if _is_postgres():
        with op.get_context().autocommit_block():
            op.get_bind().exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS apex.{name}")
        return
    op.drop_index(name, table_name=table_name, schema="apex")


def upgrade() -> None:
    op.add_column(
        "api_consumers",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
    )
    op.add_column(
        "api_consumers",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
    )
    op.add_column(
        "api_consumers",
        sa.Column("created_by", sa.String(length=255), nullable=True),
        schema="apex",
    )
    op.add_column(
        "api_consumers",
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        schema="apex",
    )
    op.add_column(
        "api_consumers",
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
    )
    op.add_column(
        "api_consumers",
        sa.Column("rotation_count", sa.Integer(), server_default="0", nullable=False),
        schema="apex",
    )
    op.add_column(
        "api_consumers",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("principal_id", sa.String(length=255), nullable=True),
        sa.Column("principal_type", sa.String(length=64), nullable=True),
        sa.Column("principal_role", sa.String(length=32), nullable=True),
        sa.Column("principal_scopes", _json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("request_method", sa.String(length=16), nullable=True),
        sa.Column("request_path", sa.String(length=2048), nullable=True),
        sa.Column("request_id", sa.String(length=255), nullable=True),
        sa.Column("ip_address", sa.String(length=255), nullable=True),
        sa.Column("user_agent", sa.String(length=1024), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("resource_type", sa.String(length=128), nullable=True),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("extra", _json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("previous_hash", sa.String(length=64), nullable=True),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sa.UniqueConstraint("event_hash", name=op.f("uq_audit_log_event_hash")),
        schema="apex",
    )
    _create_index("ix_audit_log_at", "audit_log", ["at"])
    _create_index("ix_audit_log_principal_at", "audit_log", ["principal_id", "at"])
    _create_index("ix_audit_log_decision_at", "audit_log", ["decision", "at"])


def downgrade() -> None:
    _drop_index("ix_audit_log_decision_at", "audit_log")
    _drop_index("ix_audit_log_principal_at", "audit_log")
    _drop_index("ix_audit_log_at", "audit_log")
    op.drop_table("audit_log", schema="apex")
    for column in (
        "deleted_at",
        "rotation_count",
        "rotated_at",
        "updated_by",
        "created_by",
        "revoked_at",
        "expires_at",
    ):
        op.drop_column("api_consumers", column, schema="apex")
