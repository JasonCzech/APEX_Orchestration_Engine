"""SQLAlchemy declarative base for all APEX tables (dedicated `apex` schema).

LangGraph Server owns its own tables in the default schema with its own migrations;
APEX never writes those. Domain tables (prompts, connections, catalog, consumers, ...)
land here from M2 onward.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(referred_table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(schema="apex", naming_convention=NAMING_CONVENTION)
