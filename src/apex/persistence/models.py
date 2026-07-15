"""SQLAlchemy declarative base for all APEX tables (dedicated `apex` schema).

LangGraph Server owns its own tables in the default schema with its own migrations;
APEX never writes those. Domain tables (prompts, connections, catalog, consumers, ...)
land here from M2 onward.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JsonColumn = JSON().with_variant(JSONB(), "postgresql")

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(referred_table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(schema="apex", naming_convention=NAMING_CONVENTION)


def _new_id() -> str:
    return uuid4().hex


class ApiConsumer(Base):
    """An API consumer (ADR-0003): hashed key, type, ordered role, project/app scopes."""

    __tablename__ = "api_consumers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex
    consumer_type: Mapped[str] = mapped_column(String(32))  # dashboard | headless | internal
    role: Mapped[str] = mapped_column(String(32))  # viewer | operator | admin
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(255))
    updated_by: Mapped[str | None] = mapped_column(String(255))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotation_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    scopes: Mapped[list["ConsumerScope"]] = relationship(
        back_populates="consumer", cascade="all, delete-orphan", lazy="selectin"
    )
    keys: Mapped[list["ConsumerKey"]] = relationship(
        back_populates="consumer", cascade="all, delete-orphan", lazy="selectin"
    )


class ConsumerScope(Base):
    """A project (optionally narrowed to one app) an API consumer may act on."""

    __tablename__ = "consumer_scopes"
    __table_args__ = (
        UniqueConstraint("consumer_id", "project_id", "app_id"),
        Index("ix_consumer_scopes_consumer_id", "consumer_id"),
        Index("ix_consumer_scopes_project_app", "project_id", "app_id"),
        Index(
            "uq_consumer_scopes_consumer_project_no_app",
            "consumer_id",
            "project_id",
            unique=True,
            postgresql_where=text("app_id IS NULL"),
            sqlite_where=text("app_id IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    consumer_id: Mapped[str] = mapped_column(ForeignKey("api_consumers.id", ondelete="CASCADE"))
    project_id: Mapped[str] = mapped_column(String(255))
    app_id: Mapped[str | None] = mapped_column(String(255))

    consumer: Mapped[ApiConsumer] = relationship(back_populates="scopes")


class ConsumerKey(Base):
    """One hashed API key belonging to an API consumer."""

    __tablename__ = "consumer_keys"
    __table_args__ = (
        Index("ix_consumer_keys_consumer_id", "consumer_id"),
        Index("ix_consumer_keys_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    consumer_id: Mapped[str] = mapped_column(ForeignKey("api_consumers.id", ondelete="CASCADE"))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiry_source: Mapped[str] = mapped_column(
        String(32),
        default="independent",
        server_default="legacy_ambiguous",
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_from_id: Mapped[str | None] = mapped_column(String(32))
    created_by: Mapped[str | None] = mapped_column(String(255))

    consumer: Mapped[ApiConsumer] = relationship(back_populates="keys")


class AuditLog(Base):
    """Append-only security/audit event with a hash-chain for tamper evidence."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_at", "at"),
        Index("ix_audit_log_principal_at", "principal_id", "at"),
        Index("ix_audit_log_decision_at", "decision", "at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    chain_seq: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    category: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(128))
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    principal_id: Mapped[str | None] = mapped_column(String(255))
    principal_type: Mapped[str | None] = mapped_column(String(64))
    principal_role: Mapped[str | None] = mapped_column(String(32))
    principal_scopes: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    request_method: Mapped[str | None] = mapped_column(String(16))
    request_path: Mapped[str | None] = mapped_column(String(2048))
    request_id: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(255))
    user_agent: Mapped[str | None] = mapped_column(String(1024))
    status_code: Mapped[int | None] = mapped_column(Integer)
    resource_type: Mapped[str | None] = mapped_column(String(128))
    resource_id: Mapped[str | None] = mapped_column(String(255))
    extra: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    event_nonce: Mapped[str | None] = mapped_column(String(32))
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)


class ConsumerDeletionRecord(Base):
    """Immutable tombstone for API consumer deletion events."""

    __tablename__ = "consumer_deletion_records"
    __table_args__ = (
        Index("ix_consumer_deletion_records_consumer_id", "consumer_id"),
        Index("ix_consumer_deletion_records_deleted_at", "deleted_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    consumer_id: Mapped[str] = mapped_column(String(32))
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_by: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    consumer_type: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(32))
    scopes: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)


# ── Prompt catalog (M2) ─────────────────────────────────────────────────────
# A prompt is (namespace, key) with an active-version pointer. Versions are
# immutable; save = new version + pointer move; rollback = pointer move only.


class Prompt(Base):
    __tablename__ = "prompts"
    __table_args__ = (
        UniqueConstraint("namespace", "key"),
        Index("ix_prompts_active_version_id", "active_version_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    namespace: Mapped[str] = mapped_column(String(255))  # e.g. "phase", "observability"
    key: Mapped[str] = mapped_column(String(255))  # e.g. "story_analysis/system"
    description: Mapped[str | None] = mapped_column(Text)
    active_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("prompt_versions.id", use_alter=True)
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    versions: Mapped[list["PromptVersion"]] = relationship(
        back_populates="prompt",
        cascade="all, delete-orphan",
        foreign_keys="PromptVersion.prompt_id",
        order_by="PromptVersion.version.desc()",
    )


class PromptVersion(Base):
    """Immutable prompt content. Never updated or deleted while the prompt exists."""

    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("prompt_id", "version"),
        Index("ix_prompt_versions_prompt_id", "prompt_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    prompt_id: Mapped[str] = mapped_column(
        ForeignKey("prompts.id", ondelete="CASCADE", use_alter=True)
    )
    version: Mapped[int] = mapped_column(Integer)  # monotonic per prompt, 1-based
    content: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    parent_version_id: Mapped[str | None] = mapped_column(String(32))
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    prompt: Mapped[Prompt] = relationship(back_populates="versions", foreign_keys=[prompt_id])


# ── Application / environment catalog (M2) ─────────────────────────────────


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("project_id", "name"),
        Index("ix_applications_project_id", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    project_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    environments: Mapped[list["Environment"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class Environment(Base):
    """An environment reference (legacy 'environment configurations')."""

    __tablename__ = "environments"
    __table_args__ = (
        UniqueConstraint("application_id", "name"),
        Index("ix_environments_application_id", "application_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))  # e.g. "staging-2"
    kind: Mapped[str | None] = mapped_column(String(64))  # e.g. "k8s", "vm"
    base_url: Mapped[str | None] = mapped_column(String(1024))
    options: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    target_approved: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    target_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    application: Mapped[Application] = relationship(back_populates="environments")
    hosts: Mapped[list["EnvironmentHost"]] = relationship(
        back_populates="environment", cascade="all, delete-orphan", lazy="selectin"
    )


class EnvironmentHost(Base):
    __tablename__ = "environment_hosts"
    __table_args__ = (Index("ix_environment_hosts_environment_id", "environment_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"))
    hostname: Mapped[str] = mapped_column(String(1024))
    role: Mapped[str | None] = mapped_column(String(255))  # e.g. "app", "db", "lb"

    environment: Mapped[Environment] = relationship(back_populates="hosts")


class EnvironmentSnapshot(Base):
    """Cluster-inventory scan results (k8s rescan fills these from M4)."""

    __tablename__ = "environment_snapshots"
    __table_args__ = (
        Index("ix_environment_snapshots_environment_scanned", "environment_id", "scanned_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"))
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    data: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)


# ── Connections (M2) — admin CRUD that doubles as runtime adapter config ───


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint(
            "name",
        ),
        Index("ix_connections_project_id", "project_id"),
        Index("ix_connections_kind_project_enabled", "kind", "project_id", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    kind: Mapped[str] = mapped_column(String(64))  # PortKind value
    provider: Mapped[str] = mapped_column(String(64))  # registered adapter provider
    name: Mapped[str] = mapped_column(String(255))
    project_id: Mapped[str | None] = mapped_column(String(255))  # null = global
    base_url: Mapped[str | None] = mapped_column(String(1024))
    options: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    secret_ref: Mapped[str | None] = mapped_column(String(1024))  # supported "env:NAME" reference
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # Adapter affinity uses a semantic runtime generation, not the public row
    # modification timestamp. Metadata-only edits (for example a display-name
    # rename) must update ``updated_at`` without invalidating an in-flight engine
    # or artifact-store reservation.
    runtime_version: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    host_mappings: Mapped[list["HostMapping"]] = relationship(
        back_populates="connection", cascade="all, delete-orphan", lazy="selectin"
    )


class HostMapping(Base):
    __tablename__ = "host_mappings"
    __table_args__ = (Index("ix_host_mappings_connection_id", "connection_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"))
    pattern: Mapped[str] = mapped_column(String(1024))
    target: Mapped[str] = mapped_column(String(1024))
    enabled: Mapped[bool] = mapped_column(default=True)

    connection: Mapped[Connection] = relationship(back_populates="host_mappings")


# ── Documents + wizard drafts (M2) ──────────────────────────────────────────

# Length of the inline text snippet returned in API responses; full extracted text stays
# server-side (used only to build phase context), so list/detail payloads stay small.
DOCUMENT_TEXT_PREVIEW_CHARS = 2000


class Document(Base):
    """Uploaded context document metadata; bytes live in the artifact store."""

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_artifact_key", "artifact_key"),
        Index("ix_documents_project_created", "project_id", "created_at"),
        Index("ix_documents_created_at", "created_at"),
        Index("ix_documents_deletion_pending", "deletion_pending_at"),
        Index("ix_documents_upload_pending", "upload_pending_at"),
        Index("ix_documents_cleanup_retry", "cleanup_retry_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(1024))
    media_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    artifact_key: Mapped[str] = mapped_column(String(1024))  # key in the artifact store
    artifact_connection_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("connections.id", ondelete="RESTRICT")
    )
    project_id: Mapped[str | None] = mapped_column(String(255))
    app_id: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deletion_pending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upload_pending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cleanup_last_error: Mapped[str | None] = mapped_column(Text)
    # Extracted plain text + parse outcome, populated on upload by the text extractor.
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extracted_chars: Mapped[int | None] = mapped_column(Integer)
    parse_status: Mapped[str | None] = mapped_column(String(32))
    parse_error: Mapped[str | None] = mapped_column(Text)

    @property
    def text_preview(self) -> str | None:
        """Short snippet of the extracted text for API responses (full text stays server-side)."""
        if not self.extracted_text:
            return None
        if len(self.extracted_text) <= DOCUMENT_TEXT_PREVIEW_CHARS:
            return self.extracted_text
        return self.extracted_text[:DOCUMENT_TEXT_PREVIEW_CHARS] + "…"


class ArtifactReference(Base):
    """Durable ownership/lifecycle index for checkpoint-addressed artifacts."""

    __tablename__ = "artifact_references"
    __table_args__ = (
        UniqueConstraint("artifact_key"),
        Index("ix_artifact_references_connection_id", "connection_id"),
        Index("ix_artifact_references_thread_id", "thread_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    artifact_key: Mapped[str] = mapped_column(String(1024))
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64))
    thread_id: Mapped[str] = mapped_column(String(255))
    project_id: Mapped[str | None] = mapped_column(String(255))
    app_id: Mapped[str | None] = mapped_column(String(255))
    ownership_known: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # Uploads finalized through the durable outbox bind the logical key to exact
    # bytes. Legacy/external references remain nullable because their payload is
    # not available to the migration for a trustworthy backfill.
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    content_type: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ArtifactUploadIntent(Base):
    """Durable outbox row for an artifact that has not been indexed yet."""

    __tablename__ = "artifact_upload_intents"
    __table_args__ = (
        UniqueConstraint("artifact_key"),
        Index("ix_artifact_upload_intents_connection_id", "connection_id"),
        Index("ix_artifact_upload_intents_updated_at", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    artifact_key: Mapped[str] = mapped_column(String(1024))
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64))
    thread_id: Mapped[str] = mapped_column(String(255))
    project_id: Mapped[str | None] = mapped_column(String(255))
    app_id: Mapped[str | None] = mapped_column(String(255))
    ownership_known: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    content_type: Mapped[str] = mapped_column(String(255))
    claim_token: Mapped[str] = mapped_column(String(32))
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WorkItemMutation(Base):
    """Durable idempotency record for provider-side work-item mutations."""

    __tablename__ = "work_item_mutations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_scope",
            "consumer_id",
            "connection_id",
            "operation",
            "idempotency_key",
            name="uq_work_item_mutation_scope_key",
        ),
        Index("ix_work_item_mutations_connection_id", "connection_id"),
        Index("ix_work_item_mutations_reconcile", "status", "next_attempt_at"),
        Index("ix_work_item_mutations_terminal_retirement", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    tenant_scope: Mapped[str] = mapped_column(String(64))
    consumer_id: Mapped[str] = mapped_column(String(255))
    project_id: Mapped[str | None] = mapped_column(String(255))
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="RESTRICT"), nullable=False
    )
    connection_version: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    operation: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonColumn)
    target_key: Mapped[str | None] = mapped_column(String(255))
    provider_marker: Mapped[str] = mapped_column(String(64), unique=True)
    provider_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending")
    fields_status: Mapped[str] = mapped_column(
        String(32), default="skipped", server_default="skipped"
    )
    comment_status: Mapped[str] = mapped_column(
        String(32), default="skipped", server_default="skipped"
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn)
    claim_token: Mapped[str | None] = mapped_column(String(32))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    terminal_error: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WorkItemMutationTombstone(Base):
    """Compact permanent key claim after the live replay window expires.

    The fixed-size scope digest prevents a retired idempotency key from ever
    issuing another provider mutation without retaining payloads, results, or
    a foreign-key lease on a connection forever.
    """

    __tablename__ = "work_item_mutation_tombstones"

    scope_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    retired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SavedQuery(Base):
    """A saved work-tracking query (project-scoped, provider-tagged)."""

    __tablename__ = "saved_queries"
    __table_args__ = (
        UniqueConstraint("project_id", "name"),
        Index(
            "uq_saved_queries_global_name",
            "name",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index("ix_saved_queries_project_id", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(255))
    project_id: Mapped[str | None] = mapped_column(String(255))  # null = global
    provider: Mapped[str] = mapped_column(String(64))  # jira | ado | stub
    query: Mapped[str] = mapped_column(Text)  # JQL / WIQL / stub expression
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EngineRun(Base):
    """Queryable engine-run history projection, independent of graph checkpoints.

    Upserted by the execution phase's engine nodes (best-effort — checkpointed graph
    state stays the source of truth; this exists for dashboard history queries).
    """

    __tablename__ = "engine_runs"
    __table_args__ = (
        UniqueConstraint("thread_id", "attempt"),
        UniqueConstraint("artifact_namespace"),
        Index("ix_engine_runs_project_started", "project_id", "started_at"),
        Index("ix_engine_runs_project_app_started", "project_id", "app_id", "started_at"),
        Index("ix_engine_runs_status_started", "status", "started_at"),
        Index("ix_engine_runs_engine_started", "engine", "started_at"),
        Index("ix_engine_runs_external_run_id", "external_run_id"),
        Index("ix_engine_runs_connection_id", "connection_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    thread_id: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[str | None] = mapped_column(String(255))
    app_id: Mapped[str | None] = mapped_column(String(255))
    ownership_known: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    # Introduced after the app-projection bug. Unlike ``ownership_known``, old
    # rolling pods cannot explicitly set this new bit, so its false DB default
    # quarantines writes made during the migration-to-rollout overlap.
    scope_ownership_known: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    attempt: Mapped[int] = mapped_column(Integer)
    engine: Mapped[str] = mapped_column(String(64))
    external_run_id: Mapped[str | None] = mapped_column(String(255))
    artifact_namespace: Mapped[str | None] = mapped_column(String(512))
    artifact_connection_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("connections.id", ondelete="RESTRICT")
    )
    connection_id: Mapped[str | None] = mapped_column(
        ForeignKey("connections.id", ondelete="RESTRICT")
    )
    # Durable post-effect witness fields survive terminal lease release. They
    # let a graph recover a committed terminal projection without resolving a
    # connection that may have been disabled/deleted after teardown completed.
    execution_connection_version: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    artifact_connection_version: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    completion_kind: Mapped[str | None] = mapped_column(String(32))
    handle: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    status: Mapped[str] = mapped_column(String(32))  # EngineRunPhase value
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn)


# ── Usage analytics (M6) ─────────────────────────────────────────────────────


class UsageEvent(Base):
    """One usage-analytics event: a /v1 request or a graph phase terminal status.

    Written best-effort by apex.services.usage (analytics must never fail a request
    or a run) and aggregated by GET /v1/analytics/usage. Plain table in v1 — the
    plan's monthly partitioning on `at` (plus a retention job) is a deliberate
    scale follow-up once event volume warrants it.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("event_key"),
        Index("ix_usage_events_project_id_at", "project_id", "at"),
        Index("ix_usage_events_project_app_at", "project_id", "app_id", "at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    event_key: Mapped[str | None] = mapped_column(String(512))
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    consumer_name: Mapped[str] = mapped_column(String(255))
    project_id: Mapped[str | None] = mapped_column(String(255))
    app_id: Mapped[str | None] = mapped_column(String(255))
    surface: Mapped[str] = mapped_column(String(32))  # "v1" | "graph"
    action: Mapped[str] = mapped_column(String(255))  # operation_id or graph event name
    thread_id: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # "ok" | "error"
    extra: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)


class AgentEvent(Base):
    """One agent-behavior analytics event emitted per phase/agent invocation.

    Stubs write zero-token rows today; real LLM agents can populate the token
    columns from AIMessage.usage_metadata without changing the analytics API.
    """

    __tablename__ = "agent_events"
    __table_args__ = (
        UniqueConstraint("event_key"),
        Index("ix_agent_events_project_id_at", "project_id", "at"),
        Index("ix_agent_events_project_app_at", "project_id", "app_id", "at"),
        Index("ix_agent_events_phase_at", "phase", "at"),
        Index("ix_agent_events_model_at", "model", "at"),
        Index("ix_agent_events_thread_id", "thread_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    event_key: Mapped[str | None] = mapped_column(String(512))
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    thread_id: Mapped[str | None] = mapped_column(String(64))
    project_id: Mapped[str | None] = mapped_column(String(255))
    app_id: Mapped[str | None] = mapped_column(String(255))
    phase: Mapped[str] = mapped_column(String(64))
    agent_name: Mapped[str] = mapped_column(String(255))
    model: Mapped[str | None] = mapped_column(String(255))
    provider: Mapped[str | None] = mapped_column(String(64))
    attempt: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # "ok" | "error"
    input_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0", default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0", default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0", default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0", default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0", default=0)
    reasoning_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0", default=0)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    extra: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)


class Draft(Base):
    """Server-side new-test wizard draft (roams across browsers/operators)."""

    __tablename__ = "drafts"
    __table_args__ = (
        Index("ix_drafts_project_id", "project_id"),
        Index("ix_drafts_created_by_consumer_id", "created_by_consumer_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    title: Mapped[str] = mapped_column(String(1024))
    project_id: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_consumer_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
