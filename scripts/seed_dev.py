"""Seed dev API consumers (apex-admin / apex-operator / apex-viewer).

Idempotent by name. Each generated key is printed exactly once — existing
consumers keep their keys. Run: uv run python scripts/seed_dev.py
"""

import asyncio
import secrets
import sys

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from apex.auth.service import hash_api_key
from apex.domain.diagnostics import safe_type_name
from apex.persistence.db import get_sessionmaker
from apex.persistence.models import ApiConsumer

SEED_CONSUMERS: tuple[tuple[str, str, str], ...] = (
    ("apex-admin", "internal", "admin"),
    ("apex-operator", "headless", "operator"),
    ("apex-viewer", "dashboard", "viewer"),
)


async def main() -> int:
    try:
        async with get_sessionmaker()() as session:
            for name, consumer_type, role in SEED_CONSUMERS:
                existing = await session.scalar(select(ApiConsumer).where(ApiConsumer.name == name))
                if existing is not None:
                    print(f"{name}: already exists (id={existing.id}); key unchanged")
                    continue
                api_key = secrets.token_urlsafe(32)
                session.add(
                    ApiConsumer(
                        name=name,
                        key_hash=hash_api_key(api_key),
                        consumer_type=consumer_type,
                        role=role,
                    )
                )
                print(f"{name}: {api_key}  <- shown once, store it now")
            await session.commit()
    except (SQLAlchemyError, OSError) as exc:
        print(f"Database unreachable ({safe_type_name(exc)}).")
        print("Run `make infra-up` + `make migrate` first, then re-run this script.")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
