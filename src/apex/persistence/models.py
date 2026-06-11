"""SQLAlchemy declarative base for all APEX tables (dedicated `apex` schema).

LangGraph Server owns its own tables in the default schema with its own migrations;
APEX never writes those. Domain tables (prompts, connections, catalog, consumers, ...)
land here from M2 onward.
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, MetaData, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

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

    scopes: Mapped[list["ConsumerScope"]] = relationship(
        back_populates="consumer", cascade="all, delete-orphan", lazy="selectin"
    )


class ConsumerScope(Base):
    """A project (optionally narrowed to one app) an API consumer may act on."""

    __tablename__ = "consumer_scopes"
    __table_args__ = (UniqueConstraint("consumer_id", "project_id", "app_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    consumer_id: Mapped[str] = mapped_column(ForeignKey("api_consumers.id", ondelete="CASCADE"))
    project_id: Mapped[str] = mapped_column(String(255))
    app_id: Mapped[str | None] = mapped_column(String(255))

    consumer: Mapped[ApiConsumer] = relationship(back_populates="scopes")
