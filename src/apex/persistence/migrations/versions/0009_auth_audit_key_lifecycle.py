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


def _json_type() -> sa.types.TypeEngine[Any]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _create_index(name: str, table_name: str, columns: list[str]) -> None:
    op.drop_index(name, table_name=table_name, schema="apex", if_exists=True)
    op.create_index(
        name,
        table_name,
        columns,
        schema="apex",
    )


def _drop_index(name: str, table_name: str) -> None:
    op.drop_index(
        name,
        table_name=table_name,
        schema="apex",
        if_exists=True,
    )


def upgrade() -> None:
    op.add_column(
        "api_consumers",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
        if_not_exists=True,
    )
    op.add_column(
        "api_consumers",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
        if_not_exists=True,
    )
    op.add_column(
        "api_consumers",
        sa.Column("created_by", sa.String(length=255), nullable=True),
        schema="apex",
        if_not_exists=True,
    )
    op.add_column(
        "api_consumers",
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        schema="apex",
        if_not_exists=True,
    )
    op.add_column(
        "api_consumers",
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
        if_not_exists=True,
    )
    op.add_column(
        "api_consumers",
        sa.Column("rotation_count", sa.Integer(), server_default="0", nullable=False),
        schema="apex",
        if_not_exists=True,
    )
    op.add_column(
        "api_consumers",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        schema="apex",
        if_not_exists=True,
    )

    op.create_table(
        "consumer_keys",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("consumer_id", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_from_id", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["consumer_id"],
            ["apex.api_consumers.id"],
            name=op.f("fk_consumer_keys_api_consumers_consumer_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consumer_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_consumer_keys_key_hash")),
        schema="apex",
        if_not_exists=True,
    )
    _create_index("ix_consumer_keys_consumer_id", "consumer_keys", ["consumer_id"])
    _create_index("ix_consumer_keys_expires_at", "consumer_keys", ["expires_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
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
        if_not_exists=True,
    )
    _create_index("ix_audit_log_at", "audit_log", ["at"])
    _create_index("ix_audit_log_principal_at", "audit_log", ["principal_id", "at"])
    _create_index("ix_audit_log_decision_at", "audit_log", ["decision", "at"])

    op.create_table(
        "consumer_deletion_records",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("consumer_id", sa.String(length=32), nullable=False),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_by", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("consumer_type", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("scopes", _json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consumer_deletion_records")),
        schema="apex",
        if_not_exists=True,
    )
    _create_index(
        "ix_consumer_deletion_records_consumer_id",
        "consumer_deletion_records",
        ["consumer_id"],
    )
    _create_index(
        "ix_consumer_deletion_records_deleted_at",
        "consumer_deletion_records",
        ["deleted_at"],
    )


def downgrade() -> None:
    _drop_index("ix_consumer_deletion_records_deleted_at", "consumer_deletion_records")
    _drop_index("ix_consumer_deletion_records_consumer_id", "consumer_deletion_records")
    op.drop_table("consumer_deletion_records", schema="apex")
    _drop_index("ix_audit_log_decision_at", "audit_log")
    _drop_index("ix_audit_log_principal_at", "audit_log")
    _drop_index("ix_audit_log_at", "audit_log")
    op.drop_table("audit_log", schema="apex")
    _drop_index("ix_consumer_keys_expires_at", "consumer_keys")
    _drop_index("ix_consumer_keys_consumer_id", "consumer_keys")
    op.drop_table("consumer_keys", schema="apex")
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
