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
    CheckConstraint,
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
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
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
    principal_scopes: Mapped[dict[str, Any]] = mapped_column(
        JsonColumn, default=dict, server_default=text("'{}'")
    )
    request_method: Mapped[str | None] = mapped_column(String(16))
    request_path: Mapped[str | None] = mapped_column(String(2048))
    request_id: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(255))
    user_agent: Mapped[str | None] = mapped_column(String(1024))
    status_code: Mapped[int | None] = mapped_column(Integer)
    resource_type: Mapped[str | None] = mapped_column(String(128))
    resource_id: Mapped[str | None] = mapped_column(String(255))
    extra: Mapped[dict[str, Any]] = mapped_column(
        JsonColumn, default=dict, server_default=text("'{}'")
    )
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
    scopes: Mapped[dict[str, Any]] = mapped_column(
        JsonColumn, default=dict, server_default=text("'{}'")
    )


# ── Prompt catalog (M2) ─────────────────────────────────────────────────────
# A prompt is (namespace, key) with an active-version pointer. Versions are
# immutable; save = new version + pointer move; rollback = pointer move only.


class Prompt(Base):
    __tablename__ = "prompts"
    __table_args__ = (
        UniqueConstraint("namespace", "key"),
        Index("ix_prompts_active_version_id", "active_version_id"),
        CheckConstraint(
            "NOT is_default OR (selection_enabled AND archived_at IS NULL "
            "AND active_version_id IS NOT NULL AND namespace IN ('system', 'phase'))",
            name="prompt_default_eligible",
        ),
        Index(
            "uq_prompts_default_system",
            "namespace",
            unique=True,
            postgresql_where=text("is_default AND namespace = 'system'"),
            sqlite_where=text("is_default AND namespace = 'system'"),
        ),
        Index(
            "uq_prompts_default_phase",
            "phase",
            unique=True,
            postgresql_where=text("is_default AND namespace = 'phase'"),
            sqlite_where=text("is_default AND namespace = 'phase'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    namespace: Mapped[str] = mapped_column(String(255))  # e.g. "phase", "observability"
    key: Mapped[str] = mapped_column(String(255))  # e.g. "story_analysis/system"
    phase: Mapped[str | None] = mapped_column(String(64))
    application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id"))
    environment_id: Mapped[str | None] = mapped_column(ForeignKey("environments.id"))
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    selection_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )

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


class ProjectLabel(Base):
    """Administrator-maintained display metadata; never grants project access."""

    __tablename__ = "project_labels"
    project_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Application(Base):
    """A system under test. One application serves any number of projects
    (``project_links``); its environments belong to it, not to a project."""

    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
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
    project_links: Mapped[list["ProjectApplication"]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ProjectApplication.project_id",
    )

    @property
    def project_ids(self) -> list[str]:
        """The projects this application is linked to, sorted."""
        return [link.project_id for link in self.project_links]

    @project_ids.setter
    def project_ids(self, values: list[str]) -> None:
        # Diff rather than replace: re-adding an existing (project, application)
        # row in the same flush would collide with its own primary key.
        wanted = list(dict.fromkeys(values))
        kept = [link for link in self.project_links if link.project_id in wanted]
        have = {link.project_id for link in kept}
        self.project_links = sorted(
            kept + [ProjectApplication(project_id=pid) for pid in wanted if pid not in have],
            key=lambda link: link.project_id,
        )


class ProjectApplication(Base):
    """Links an application to a project that tests it (many-to-many)."""

    __tablename__ = "project_applications"
    __table_args__ = (Index("ix_project_applications_application_id", "application_id"),)

    project_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    application: Mapped[Application] = relationship(back_populates="project_links")


class CatalogMerge(Base):
    """Audit trail of duplicate catalog rows merged by migration 0047 (old id → kept id)."""

    __tablename__ = "catalog_merges"

    old_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # "application" | "environment"
    new_id: Mapped[str] = mapped_column(String(32))
    old_project_id: Mapped[str | None] = mapped_column(String(255))
    merged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
        Index("ix_saved_queries_connection_id", "connection_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(255))
    project_id: Mapped[str | None] = mapped_column(String(255))  # null = global
    provider: Mapped[str] = mapped_column(String(64))  # jira | jira_align | ado | stub
    query: Mapped[str] = mapped_column(Text)  # JQL / OData $filter / WIQL / stub expression
    # Nullable only for global templates and quarantined legacy rows. Deliberately
    # not a foreign key: development/static adapters have stable ids without a
    # connections-table row, and deleting a persisted connection must leave a
    # dead explicit id that fails closed rather than silently rebinding.
    connection_id: Mapped[str | None] = mapped_column(String(32))
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
    execution_connection_version: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    artifact_connection_version: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_kind: Mapped[str | None] = mapped_column(String(32))
    handle: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    status: Mapped[str] = mapped_column(String(32))  # EngineRunPhase value
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn)


# ── APEX Load native control-plane mirror ────────────────────────────────────


class ApexLoadNativeReconcileLease(Base):
    """Durable ownership for one connection's provider snapshot poll."""

    __tablename__ = "apex_load_native_reconcile_leases"
    __table_args__ = (
        Index("ix_apex_load_native_reconcile_leases_ready", "next_poll_at", "claimed_at"),
    )

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), primary_key=True
    )
    connection_version: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    project_id: Mapped[str] = mapped_column(String(255))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ApexLoadNativeScenario(Base):
    """A provider-owned scenario mirrored for audit and reconciliation only."""

    __tablename__ = "apex_load_native_scenarios"
    __table_args__ = (
        UniqueConstraint("connection_id", "connection_version", "remote_project_id", "remote_id"),
        UniqueConstraint(
            "connection_id",
            "connection_version",
            "remote_project_id",
            "idempotency_key",
            name="uq_apex_load_native_scenario_idempotency",
        ),
        Index("ix_apex_load_native_scenarios_project_seen", "project_id", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id", ondelete="RESTRICT"))
    connection_version: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    project_id: Mapped[str] = mapped_column(String(255))
    remote_project_id: Mapped[str] = mapped_column(String(255))
    remote_id: Mapped[str] = mapped_column(String(255))
    revision: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    idempotency_fingerprint: Mapped[str | None] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default=text("'{}'")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ApexLoadNativeSchedule(Base):
    """A provider-owned schedule mirrored without becoming a second scheduler."""

    __tablename__ = "apex_load_native_schedules"
    __table_args__ = (
        UniqueConstraint("connection_id", "connection_version", "remote_project_id", "remote_id"),
        UniqueConstraint(
            "connection_id",
            "connection_version",
            "remote_project_id",
            "scenario_remote_id",
            "idempotency_key",
            name="uq_apex_load_native_schedule_idempotency",
        ),
        Index("ix_apex_load_native_schedules_project_seen", "project_id", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id", ondelete="RESTRICT"))
    connection_version: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    project_id: Mapped[str] = mapped_column(String(255))
    remote_project_id: Mapped[str] = mapped_column(String(255))
    remote_id: Mapped[str] = mapped_column(String(255))
    scenario_remote_id: Mapped[str] = mapped_column(String(255))
    revision: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    idempotency_fingerprint: Mapped[str | None] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default=text("'{}'")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ApexLoadNativeRun(Base):
    """Durable native-run import and exactly-once reporting-launch ledger."""

    __tablename__ = "apex_load_native_runs"
    __table_args__ = (
        UniqueConstraint("connection_id", "connection_version", "remote_project_id", "remote_id"),
        Index("ix_apex_load_native_runs_project_started", "project_id", "started_at"),
        Index("ix_apex_load_native_runs_launch", "terminal", "pipeline_status", "claimed_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id", ondelete="RESTRICT"))
    connection_version: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    project_id: Mapped[str] = mapped_column(String(255))
    remote_project_id: Mapped[str] = mapped_column(String(255))
    remote_id: Mapped[str] = mapped_column(String(255))
    scenario_remote_id: Mapped[str | None] = mapped_column(String(255))
    schedule_remote_id: Mapped[str | None] = mapped_column(String(255))
    revision: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(64))
    terminal: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default=text("'{}'")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255))
    pipeline_status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending"
    )
    pipeline_thread_id: Mapped[str | None] = mapped_column(String(255))
    pipeline_run_id: Mapped[str | None] = mapped_column(String(255))
    pipeline_launch_error: Mapped[str | None] = mapped_column(String(255))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ── Usage analytics (M6) ─────────────────────────────────────────────────────


class ModelCatalogEntry(Base):
    """Retained model identity; registry membership never grants execution access."""

    __tablename__ = "model_catalog"
    __table_args__ = (UniqueConstraint("provider", "model_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    provider: Mapped[str] = mapped_column(String(64))
    model_key: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by: Mapped[str | None] = mapped_column(String(255))
    archive_reason: Mapped[str | None] = mapped_column(String(1000))
    version: Mapped[int] = mapped_column(Integer, default=1)
    active_pricing_generation_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ModelPricingGeneration(Base):
    __tablename__ = "model_pricing_generations"
    __table_args__ = (UniqueConstraint("model_id", "number"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    model_id: Mapped[str] = mapped_column(ForeignKey("model_catalog.id", ondelete="RESTRICT"))
    number: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str | None] = mapped_column(String(255))
    creator_name: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String(1000))
    source: Mapped[str] = mapped_column(String(32))
    job_id: Mapped[str | None] = mapped_column(String(32))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelPriceInterval(Base):
    __tablename__ = "model_price_intervals"
    __table_args__ = (
        CheckConstraint(
            "input >= 0 AND output >= 0 AND cache_read >= 0 AND cache_creation >= 0",
            name="nonnegative_rates",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from", name="positive_interval"
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("model_pricing_generations.id", ondelete="RESTRICT"), index=True
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input: Mapped[Decimal] = mapped_column(Numeric(18, 9))
    output: Mapped[Decimal] = mapped_column(Numeric(18, 9))
    cache_read: Mapped[Decimal] = mapped_column(Numeric(18, 9))
    cache_creation: Mapped[Decimal] = mapped_column(Numeric(18, 9))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    unit: Mapped[str] = mapped_column(String(32), default="million_tokens")


class PricingCorrectionJob(Base):
    __tablename__ = "pricing_correction_jobs"
    __table_args__ = (UniqueConstraint("model_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    model_id: Mapped[str] = mapped_column(ForeignKey("model_catalog.id", ondelete="RESTRICT"))
    generation_id: Mapped[str] = mapped_column(String(32))
    base_generation_id: Mapped[str | None] = mapped_column(String(32))
    expected_version: Mapped[int] = mapped_column(Integer)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor_id: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String(1000))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(32))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    progress_cursor: Mapped[str | None] = mapped_column(String(32))
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    preview_totals: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    final_totals: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UsageCostAdjustment(Base):
    __tablename__ = "usage_cost_adjustments"
    __table_args__ = (UniqueConstraint("event_id", "generation_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("agent_events.id", ondelete="RESTRICT"), index=True
    )
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("model_pricing_generations.id", ondelete="RESTRICT")
    )
    interval_id: Mapped[str] = mapped_column(
        ForeignKey("model_price_intervals.id", ondelete="RESTRICT")
    )
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    job_id: Mapped[str] = mapped_column(
        ForeignKey("pricing_correction_jobs.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelAdminAction(Base):
    """Transactional, retained administrative history and idempotency receipt."""

    __tablename__ = "model_admin_actions"
    __table_args__ = (UniqueConstraint("model_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    model_id: Mapped[str] = mapped_column(ForeignKey("model_catalog.id", ondelete="RESTRICT"))
    actor_id: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(1000))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsageEvent(Base):
    """One usage-analytics event: a /v1 request or a graph phase terminal status.

    Written best-effort by apex.services.usage (analytics must never fail a request
    or a run) and aggregated by GET /v1/analytics/usage. Plain table in v1 — the
    plan's monthly partitioning on `at` (plus a retention job) is a deliberate
    scale follow-up once event volume warrants it.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_at", "at"),
        UniqueConstraint("event_key"),
        Index("ix_usageevent_consumer_at", "consumer_id", "at"),
        Index("ix_usageevent_scope_consumer_at", "project_id", "app_id", "consumer_id", "at"),
        Index("ix_usageevent_at_id", "at", "id"),
        Index("ix_usage_events_project_id_at", "project_id", "at"),
        Index("ix_usage_events_project_app_at", "project_id", "app_id", "at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    consumer_id: Mapped[str | None] = mapped_column(String(255), index=True)
    recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    event_key: Mapped[str | None] = mapped_column(String(512))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
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
        Index("ix_agent_events_at", "at"),
        UniqueConstraint("event_key"),
        Index("ix_agentevent_consumer_at", "consumer_id", "at"),
        Index("ix_agentevent_scope_consumer_at", "project_id", "app_id", "consumer_id", "at"),
        Index("ix_agentevent_at_id", "at", "id"),
        Index("ix_agentevent_model_catalog_at", "model_catalog_id", "at"),
        Index("ix_agent_events_project_id_at", "project_id", "at"),
        Index("ix_agent_events_project_app_at", "project_id", "app_id", "at"),
        Index("ix_agent_events_phase_at", "phase", "at"),
        Index("ix_agent_events_model_at", "model", "at"),
        Index("ix_agent_events_thread_id", "thread_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    consumer_id: Mapped[str | None] = mapped_column(String(255), index=True)
    recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    model_catalog_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_catalog.id", ondelete="RESTRICT"), index=True
    )
    consumer_name_snapshot: Mapped[str | None] = mapped_column(String(255))
    pricing_interval_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_price_intervals.id", ondelete="RESTRICT")
    )
    event_key: Mapped[str | None] = mapped_column(String(512))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
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
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
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


class TestCycle(Base):
    """Additive cycle aggregate; existing pipeline checkpoints remain untouched."""

    __tablename__ = "test_cycles"
    __table_args__ = (UniqueConstraint("origin_cycle_id", "origin_request_key"),)
    origin_cycle_id: Mapped[str | None] = mapped_column(
        ForeignKey("test_cycles.id", ondelete="RESTRICT")
    )
    origin_request_key: Mapped[str | None] = mapped_column(String(32))
    origin_fingerprint: Mapped[str | None] = mapped_column(String(64))
    followups: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonColumn, default=list, server_default=text("'[]'")
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    project_id: Mapped[str] = mapped_column(String(255), index=True)
    app_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    environment_id: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(32))
    owner: Mapped[str] = mapped_column(String(255))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    independent_review: Mapped[bool] = mapped_column(Boolean, default=False)
    versions: Mapped[list[dict[str, Any]]] = mapped_column(JsonColumn, default=list)
    progress: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CycleExecutionPreparation(Base):
    """Immutable bindings with an auditable archive marker; never a launch receipt."""

    __tablename__ = "cycle_execution_preparations"
    __table_args__ = (UniqueConstraint("cycle_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("test_cycles.id", ondelete="RESTRICT"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(32))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JsonColumn)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by: Mapped[str | None] = mapped_column(String(255))


class CycleExecutionDispatch(Base):
    """One durable launch intent per preparation; nonterminal intents own the target."""

    __tablename__ = "cycle_execution_dispatches"
    __table_args__ = (
        UniqueConstraint("preparation_id"),
        UniqueConstraint("cycle_id", "request_key"),
        UniqueConstraint("active_environment_id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("test_cycles.id", ondelete="RESTRICT"), index=True
    )
    preparation_id: Mapped[str] = mapped_column(
        ForeignKey("cycle_execution_preparations.id", ondelete="RESTRICT")
    )
    request_key: Mapped[str] = mapped_column(String(32))
    active_environment_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="reserved")
    pipeline_config: Mapped[dict[str, Any]] = mapped_column(JsonColumn)
    thread_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    run_id: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(255))
    canceled_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CycleRunLink(Base):
    """A scoped reference to an existing attempt, without rewriting its provenance."""

    __tablename__ = "cycle_run_links"
    __table_args__ = (UniqueConstraint("cycle_id", "engine_run_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("test_cycles.id", ondelete="RESTRICT"), index=True
    )
    engine_run_id: Mapped[str] = mapped_column(ForeignKey("engine_runs.id", ondelete="RESTRICT"))
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CycleScriptArtifact(Base):
    """One immutable source object per cycle script version, with durable affinity."""

    __tablename__ = "cycle_script_artifacts"
    __table_args__ = (UniqueConstraint("cycle_id", "version_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("test_cycles.id", ondelete="RESTRICT"), index=True
    )
    version_id: Mapped[str] = mapped_column(String(32))
    version_checksum: Mapped[str] = mapped_column(String(64))
    source_checksum: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    artifact_key: Mapped[str] = mapped_column(String(255))
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id", ondelete="RESTRICT"))
    connection_version: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CycleScriptValidation(Base):
    __tablename__ = "cycle_script_validations"
    __table_args__ = (UniqueConstraint("cycle_id", "request_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("test_cycles.id", ondelete="RESTRICT"), index=True
    )
    preparation_id: Mapped[str] = mapped_column(
        ForeignKey("cycle_execution_preparations.id", ondelete="RESTRICT")
    )
    request_key: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    issues: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    warnings: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CycleProviderScriptImport(Base):
    __tablename__ = "cycle_provider_script_imports"
    __table_args__ = (UniqueConstraint("cycle_id", "request_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("test_cycles.id", ondelete="RESTRICT"), index=True
    )
    version_id: Mapped[str] = mapped_column(String(32))
    request_key: Mapped[str] = mapped_column(String(32))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id", ondelete="RESTRICT"))
    connection_version: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    remote_script_id: Mapped[str] = mapped_column(String(255))
    remote_name: Mapped[str] = mapped_column(String(255))
    remote_project_id: Mapped[str] = mapped_column(String(255))
    remote_version: Mapped[str] = mapped_column(String(255))
    source_checksum: Mapped[str] = mapped_column(String(64))
    imported_by: Mapped[str] = mapped_column(String(255))
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelRunAdmission(Base):
    """Server-owned resolved model choices for continuing an admitted thread."""

    __tablename__ = "model_run_admissions"
    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    models_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    models: Mapped[dict[str, Any]] = mapped_column(JsonColumn)
    actor_id: Mapped[str] = mapped_column(String(255))
    admitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class NativeAnalysisSubmission(Base):
    """Write-ahead receipt for a native analysis POST with uncertain deduplication."""

    __tablename__ = "native_analysis_submissions"
    idempotency_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_digest: Mapped[str] = mapped_column(String(64))
    thread_id: Mapped[str] = mapped_column(String(255))
    connection_id: Mapped[str] = mapped_column(String(256))
    native_run_id: Mapped[str] = mapped_column(String(256))
    job_id: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunScopeBinding(Base):
    """Immutable canonical audience and completeness marker for a combined run."""

    __tablename__ = "run_scope_bindings"
    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[dict[str, Any]] = mapped_column(JsonColumn)
    participant_count: Mapped[int] = mapped_column(Integer)
    scope_digest: Mapped[str] = mapped_column(String(64))


class RunParticipant(Base):
    """Immutable scope projection; no catalog FK so historical evidence survives deletion."""

    __tablename__ = "run_participants"
    __table_args__ = (
        Index("ix_run_participants_audience", "project_id", "application_id", "thread_id"),
    )
    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    participant_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(255))
    application_id: Mapped[str] = mapped_column(String(255))
    environment_id: Mapped[str] = mapped_column(String(256))
    layer: Mapped[str] = mapped_column(String(16))
    is_primary: Mapped[bool] = mapped_column(Boolean)
    is_traffic_entry: Mapped[bool] = mapped_column(Boolean)


class PromptUsage(Base):
    """Which prompt versions a launched test pinned, for the catalog's "Used by".

    Written best-effort when a launch creates its run; one row per pinned slot.
    """

    __tablename__ = "prompt_usages"
    __table_args__ = (Index("ix_prompt_usages_prompt_recorded", "prompt_id", "recorded_at"),)
    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    prompt_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    slot: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    project_id: Mapped[str | None] = mapped_column(String(255))
    app_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(500))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
