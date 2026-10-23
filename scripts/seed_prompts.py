"""Seed the built-in phase prompts (7 phases x system/user) into the catalog.

Idempotent by (namespace, key): missing prompts are created as v1. Optional
--upgrade-defaults creates new versions only for exact legacy builtins; customized
and archived entries are preserved. --application ID seeds an application template
only when absent. Run: uv run python scripts/seed_prompts.py
"""

import argparse
import asyncio
import sys

from sqlalchemy.exc import SQLAlchemyError

from apex.domain.diagnostics import safe_type_name
from apex.domain.prompt_catalog import APPLICATION_TEMPLATE, CATALOG_REVISION, SHARED_SYSTEM_PROMPT
from apex.persistence.db import get_sessionmaker
from apex.persistence.repositories.prompts import PromptRepository
from apex.services.prompts import DEFAULT_PHASE_PROMPTS, PHASE_NAMESPACE, PromptCatalogService


async def main(*, upgrade_defaults: bool = False, application: str | None = None) -> int:
    try:
        async with get_sessionmaker()() as session:
            repo = PromptRepository(session)
            catalog = PromptCatalogService(repo)
            system = await repo.get_by_key("system", "default")
            if system is None:
                system, _ = await catalog.create_prompt(
                    namespace="system",
                    key="default",
                    content=SHARED_SYSTEM_PROMPT,
                    applicability={"selection_enabled": True},
                )
                if await repo.get_default("system") is None:
                    await catalog.set_default(system.id)
            for key in sorted(DEFAULT_PHASE_PROMPTS):
                existing = await repo.get_by_key(PHASE_NAMESPACE, key)
                if existing is not None:
                    if upgrade_defaults:
                        # Take the same aggregate lock as catalog saves before
                        # comparing content, so a concurrent operator edit cannot
                        # be overwritten based on a stale legacy-default read.
                        await session.refresh(existing, with_for_update=True)
                    active = await repo.get_active_version(PHASE_NAMESPACE, key)
                    phase, _, part = key.partition("/")
                    legacy = (
                        f"You are the APEX {phase.replace('_', ' ')} agent. "
                        f"Produce the {phase} deliverable for this performance-testing run."
                        if part == "system"
                        else "Title: {title}\nRequest: {request}"
                    )
                    if (
                        upgrade_defaults
                        and active is not None
                        and active.content == legacy
                        and active.content != DEFAULT_PHASE_PROMPTS[key]
                    ):
                        _, version = await catalog.save_version(
                            existing.id,
                            content=DEFAULT_PHASE_PROMPTS[key],
                            note=f"APEX canonical catalog {CATALOG_REVISION}",
                            created_by="seed_prompts",
                        )
                        print(f"{PHASE_NAMESPACE}/{key}: upgraded to v{version.version}")
                        continue
                    print(f"{PHASE_NAMESPACE}/{key}: already exists (id={existing.id}); unchanged")
                    if upgrade_defaults:
                        await session.rollback()  # release the skipped row lock
                    continue
                phase, _, part = key.partition("/")
                prompt, version = await catalog.create_prompt(
                    namespace=PHASE_NAMESPACE,
                    key=key,
                    content=DEFAULT_PHASE_PROMPTS[key],
                    description=(
                        f"Phase context for the {phase.replace('_', ' ')} phase"
                        if part == "system"
                        else f"Legacy request template for the {phase.replace('_', ' ')} phase"
                    ),
                    note=f"APEX canonical catalog {CATALOG_REVISION}",
                    created_by="seed_prompts",
                    applicability={
                        "phase": phase if part == "system" else None,
                        "selection_enabled": part == "system",
                    },
                )
                if part == "system":
                    if await repo.get_default("phase", phase=phase) is None:
                        await catalog.set_default(prompt.id)
                print(f"{PHASE_NAMESPACE}/{key}: created v{version.version} (id={prompt.id})")
            if application:
                existing = await repo.get_by_key("application", application)
                if existing is None:
                    await catalog.create_prompt(
                        namespace="application",
                        key=application,
                        content=APPLICATION_TEMPLATE,
                        description="Application requirements template; tailor UNKNOWN fields",
                        note=f"APEX canonical catalog {CATALOG_REVISION}",
                        created_by="seed_prompts",
                        applicability={"application_id": application},
                    )
                    print(f"application/{application}: created v1")
                else:
                    print(f"application/{application}: already exists; unchanged")
    except (SQLAlchemyError, OSError) as exc:
        print(f"Database unreachable ({safe_type_name(exc)}).")
        print("Run `make infra-up` + `make migrate` first, then re-run this script.")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upgrade-defaults",
        action="store_true",
        help="Version unchanged legacy builtins; preserve all customized and archived prompts",
    )
    parser.add_argument("--application", help="Seed requirements for this application ID if absent")
    args = parser.parse_args()
    sys.exit(
        asyncio.run(main(upgrade_defaults=args.upgrade_defaults, application=args.application))
    )
