"""Seed the built-in phase prompts (7 phases x system/user) into the catalog.

Idempotent by (namespace, key): existing prompts are left untouched (pointer,
versions, and edits preserved); missing ones are created as v1 from
DEFAULT_PHASE_PROMPTS. Run: uv run python scripts/seed_prompts.py
"""

import asyncio
import sys

from sqlalchemy.exc import SQLAlchemyError

from apex.domain.diagnostics import safe_type_name
from apex.persistence.db import get_sessionmaker
from apex.persistence.repositories.prompts import PromptRepository
from apex.services.prompts import DEFAULT_PHASE_PROMPTS, PHASE_NAMESPACE, PromptCatalogService


async def main() -> int:
    try:
        async with get_sessionmaker()() as session:
            repo = PromptRepository(session)
            catalog = PromptCatalogService(repo)
            for key in sorted(DEFAULT_PHASE_PROMPTS):
                existing = await repo.get_by_key(PHASE_NAMESPACE, key)
                if existing is not None:
                    print(f"{PHASE_NAMESPACE}/{key}: already exists (id={existing.id}); unchanged")
                    continue
                phase, _, part = key.partition("/")
                prompt, version = await catalog.create_prompt(
                    namespace=PHASE_NAMESPACE,
                    key=key,
                    content=DEFAULT_PHASE_PROMPTS[key],
                    description=f"Built-in {part} prompt for the {phase} phase",
                    note="seeded built-in default",
                    created_by="seed_prompts",
                )
                print(f"{PHASE_NAMESPACE}/{key}: created v{version.version} (id={prompt.id})")
    except (SQLAlchemyError, OSError) as exc:
        print(f"Database unreachable ({safe_type_name(exc)}).")
        print("Run `make infra-up` + `make migrate` first, then re-run this script.")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
