"""Prompt catalog service + phase prompt resolution.

Catalog semantics (plan "Prompt management design"): a prompt is (namespace, key)
plus an active-version pointer. Save inserts an immutable PromptVersion
(version = max + 1, parent_version_id = previous active) and moves the pointer;
rollback only moves the pointer to an existing version; archive sets/clears a
flag. Versions are never mutated or deleted while the prompt exists, so the
audit trail is preserved by construction.

Phase prompt resolution order (resolve_phase_prompt):
1. run override from the configurable (cfg.prompt_overrides["phase/<phase>"]),
2. catalog active versions for phase/<phase>/system + phase/<phase>/user,
3. built-in DEFAULT_PHASE_PROMPTS.

When cfg.app_id is set, an application prompt is also resolved from
cfg.prompt_overrides["application/<app_id>"] or catalog key
application/<app_id>. It stays separate from the phase system/user pair so
operators can review and edit app-specific requirements independently.
Catalog errors and missing rows fall through to the builtins silently (debug
log) so `langgraph dev` keeps working without Postgres.
"""

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, TypedDict
from uuid import uuid4

import structlog

from apex.domain.pipeline import PHASE_ORDER, Phase
from apex.graphs.pipeline.configurable import PipelineConfigurable, PromptOverride
from apex.persistence.models import Prompt, PromptVersion

logger = structlog.get_logger(__name__)

PHASE_NAMESPACE = "phase"
APPLICATION_NAMESPACE = "application"

# Upper bound on one catalog lookup so a wedged database can never stall a
# pipeline prepare node; on timeout we fall through to the builtin templates.
CATALOG_TIMEOUT_S = 5.0

ADDITIONAL_CONTEXT_DELIMITER = "\n\n===ADDITIONAL CONTEXT (operator)===\n"


# ── Errors (routers map these onto problem-details responses) ───────────────


class PromptError(Exception):
    """Base class for prompt catalog domain errors."""


class PromptNotFoundError(PromptError):
    """No prompt with the given id (HTTP 404)."""


class PromptVersionNotFoundError(PromptError):
    """No such version on this prompt (HTTP 404)."""


class DuplicatePromptError(PromptError):
    """(namespace, key) already exists (HTTP 409)."""


class PromptVersionMismatchError(PromptError):
    """The version exists but belongs to another prompt (HTTP 409)."""


# ── Store protocols ──────────────────────────────────────────────────────────


class ActiveVersionReader(Protocol):
    """The minimal read surface the prompt resolver needs."""

    async def get_active_version(self, namespace: str, key: str) -> PromptVersion | None: ...


class PromptStore(Protocol):
    """Persistence contract for the catalog. Invariants live in the service;
    add_prompt/add_version must persist the rows AND move the active pointer."""

    async def get(self, prompt_id: str) -> Prompt | None: ...

    async def get_by_key(self, namespace: str, key: str) -> Prompt | None: ...

    async def search(
        self,
        *,
        namespace: str | None = None,
        include_archived: bool = False,
        q: str | None = None,
    ) -> list[Prompt]: ...

    async def get_version(self, version_id: str) -> PromptVersion | None: ...

    async def get_versions_by_ids(self, version_ids: list[str]) -> list[PromptVersion]: ...

    async def list_versions(self, prompt_id: str) -> list[PromptVersion]: ...

    async def max_version(self, prompt_id: str) -> int: ...

    async def get_active_version(self, namespace: str, key: str) -> PromptVersion | None: ...

    async def add_prompt(self, prompt: Prompt, first_version: PromptVersion) -> None: ...

    async def add_version(self, prompt: Prompt, version: PromptVersion) -> None: ...

    async def save(self, prompt: Prompt) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ── Catalog service ──────────────────────────────────────────────────────────


class PromptCatalogService:
    """Catalog invariants (immutable versions, monotonic numbering, pointer
    moves) on top of any PromptStore. Ids/timestamps are set here so in-memory
    fakes behave exactly like the SQLAlchemy repository."""

    def __init__(self, store: PromptStore) -> None:
        self._store = store

    async def list_prompts(
        self,
        *,
        namespace: str | None = None,
        include_archived: bool = False,
        q: str | None = None,
    ) -> list[tuple[Prompt, PromptVersion | None]]:
        prompts = await self._store.search(
            namespace=namespace, include_archived=include_archived, q=q
        )
        active_ids = [p.active_version_id for p in prompts if p.active_version_id]
        versions = {v.id: v for v in await self._store.get_versions_by_ids(active_ids)}
        return [
            (p, versions.get(p.active_version_id) if p.active_version_id else None) for p in prompts
        ]

    async def get_prompt(self, prompt_id: str) -> tuple[Prompt, PromptVersion | None]:
        prompt = await self._require(prompt_id)
        active = (
            await self._store.get_version(prompt.active_version_id)
            if prompt.active_version_id
            else None
        )
        return prompt, active

    async def create_prompt(
        self,
        *,
        namespace: str,
        key: str,
        content: str,
        description: str | None = None,
        note: str | None = None,
        created_by: str | None = None,
    ) -> tuple[Prompt, PromptVersion]:
        if await self._store.get_by_key(namespace, key) is not None:
            raise DuplicatePromptError(f"prompt {namespace}/{key} already exists")
        now = _utcnow()
        prompt = Prompt(
            id=uuid4().hex,
            namespace=namespace,
            key=key,
            description=description,
            created_at=now,
            updated_at=now,
        )
        version = PromptVersion(
            id=uuid4().hex,
            prompt_id=prompt.id,
            version=1,
            content=content,
            note=note,
            parent_version_id=None,
            created_by=created_by,
            created_at=now,
        )
        await self._store.add_prompt(prompt, version)
        return prompt, version

    async def save_version(
        self,
        prompt_id: str,
        *,
        content: str,
        note: str | None = None,
        created_by: str | None = None,
    ) -> tuple[Prompt, PromptVersion]:
        prompt = await self._require(prompt_id)
        version = PromptVersion(
            id=uuid4().hex,
            prompt_id=prompt.id,
            version=await self._store.max_version(prompt.id) + 1,
            content=content,
            note=note,
            parent_version_id=prompt.active_version_id,
            created_by=created_by,
            created_at=_utcnow(),
        )
        prompt.updated_at = _utcnow()
        await self._store.add_version(prompt, version)
        return prompt, version

    async def list_versions(self, prompt_id: str) -> list[PromptVersion]:
        await self._require(prompt_id)
        return await self._store.list_versions(prompt_id)

    async def get_version(self, prompt_id: str, version_id: str) -> PromptVersion:
        version = await self._store.get_version(version_id)
        if version is None or version.prompt_id != prompt_id:
            raise PromptVersionNotFoundError(
                f"version {version_id} not found on prompt {prompt_id}"
            )
        return version

    async def rollback(self, prompt_id: str, version_id: str) -> tuple[Prompt, PromptVersion]:
        prompt = await self._require(prompt_id)
        version = await self._store.get_version(version_id)
        if version is None:
            raise PromptVersionNotFoundError(f"version {version_id} not found")
        if version.prompt_id != prompt.id:
            raise PromptVersionMismatchError(
                f"version {version_id} belongs to prompt {version.prompt_id}, not {prompt.id}"
            )
        prompt.active_version_id = version.id
        prompt.updated_at = _utcnow()
        await self._store.save(prompt)
        return prompt, version

    async def set_archived(self, prompt_id: str, archived: bool) -> Prompt:
        prompt = await self._require(prompt_id)
        prompt.archived_at = _utcnow() if archived else None
        prompt.updated_at = _utcnow()
        await self._store.save(prompt)
        return prompt

    async def _require(self, prompt_id: str) -> Prompt:
        prompt = await self._store.get(prompt_id)
        if prompt is None:
            raise PromptNotFoundError(f"prompt {prompt_id} not found")
        return prompt


# ── Built-in phase prompt templates (moved out of phase_subgraph in M2) ─────


def _default_system(phase: Phase) -> str:
    label = phase.value.replace("_", " ")
    return (
        f"You are the APEX {label} agent. "
        f"Produce the {phase.value} deliverable for this performance-testing run."
    )


DEFAULT_USER_TEMPLATE = "Title: {title}\nRequest: {request}"

# Keyed exactly like the catalog: "<phase>/system" and "<phase>/user".
DEFAULT_PHASE_PROMPTS: dict[str, str] = {
    key: value
    for phase in PHASE_ORDER
    for key, value in (
        (f"{phase.value}/system", _default_system(phase)),
        (f"{phase.value}/user", DEFAULT_USER_TEMPLATE),
    )
}


class _SafeVariables(dict[str, Any]):
    """format_map mapping that leaves unknown {placeholders} literal."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_template(template: str, variables: Mapping[str, Any]) -> str:
    """Best-effort {placeholder} substitution; malformed templates pass through
    unchanged (catalog content may contain literal braces, e.g. JSON examples)."""
    try:
        return template.format_map(_SafeVariables(variables))
    except (ValueError, KeyError, IndexError):
        return template


# ── Phase prompt resolver ────────────────────────────────────────────────────


class ResolvedPhasePrompt(TypedDict):
    system: str
    user: str
    application: str | None
    source: dict[str, Any]  # ResolvedPromptSource-shaped: origin / ref / editor


class PromptReviewDraft(TypedDict):
    system: str
    phase_prompt: str
    application: str | None
    additional_context: str
    source: dict[str, Any]
    updated_at: str
    updated_by: str


def _app_override(cfg: PipelineConfigurable) -> PromptOverride | None:
    if not cfg.app_id:
        return None
    return cfg.prompt_overrides.get(f"{APPLICATION_NAMESPACE}/{cfg.app_id}")


def _source(origin: str, refs: list[str]) -> dict[str, Any]:
    return {"origin": origin, "ref": ",".join(refs), "editor": None}


def _compose_resolved(
    phase: Phase,
    cfg: PipelineConfigurable,
    variables: Mapping[str, Any],
    *,
    system_v: PromptVersion | None,
    user_v: PromptVersion | None,
    application_v: PromptVersion | None,
) -> ResolvedPhasePrompt:
    refs: list[str] = []
    origin = "catalog"

    system_override = cfg.prompt_overrides.get(f"{PHASE_NAMESPACE}/{phase.value}")
    if system_override is not None and system_override.content is not None:
        system = system_override.content
        refs.append(system_override.version_id or f"phase/{phase.value}@override")
        origin = "run_override"
    else:
        system = (
            system_v.content
            if system_v is not None
            else DEFAULT_PHASE_PROMPTS[f"{phase.value}/system"]
        )
        if system_v is not None:
            refs.append(f"phase/{phase.value}/system@v{system_v.version}")

    user_override = cfg.prompt_overrides.get(f"{PHASE_NAMESPACE}/{phase.value}/user")
    if user_override is not None and user_override.content is not None:
        user = user_override.content
        refs.append(user_override.version_id or f"phase/{phase.value}/user@override")
        origin = "run_override"
    elif user_v is not None:
        user = user_v.content
        refs.append(f"phase/{phase.value}/user@v{user_v.version}")
    else:
        user = DEFAULT_PHASE_PROMPTS[f"{phase.value}/user"]

    application: str | None = None
    if cfg.app_id:
        app_override = _app_override(cfg)
        if app_override is not None and app_override.content is not None:
            application = app_override.content
            refs.append(app_override.version_id or f"{APPLICATION_NAMESPACE}/{cfg.app_id}@override")
            origin = "run_override"
        elif application_v is not None:
            application = render_template(application_v.content, variables)
            refs.append(f"{APPLICATION_NAMESPACE}/{cfg.app_id}@v{application_v.version}")
        else:
            # App selected, but no catalog row yet. Keep the prompt part
            # editable in prompt-review gates without inventing builtin text.
            application = ""

    if not refs:
        refs.append(f"phase/{phase.value}@builtin")

    return ResolvedPhasePrompt(
        system=render_template(system, variables),
        user=render_template(user, variables),
        application=application,
        source=_source(origin, refs),
    )


def resolve_phase_prompt_no_catalog(
    phase: Phase,
    cfg: PipelineConfigurable,
    *,
    variables: Mapping[str, Any] | None = None,
) -> ResolvedPhasePrompt:
    """Resolve with only config overrides + builtins.

    Used as the safe fallback when catalog IO is unavailable or a sync graph
    node is already running inside an event loop.
    """
    return _compose_resolved(
        phase,
        cfg,
        dict(variables or {}),
        system_v=None,
        user_v=None,
        application_v=None,
    )


def user_prompt_with_context(phase_prompt: str, additional_context: str | None) -> str:
    context = (additional_context or "").strip()
    if not context:
        return phase_prompt
    return f"{phase_prompt}{ADDITIONAL_CONTEXT_DELIMITER}{context}"


def prompt_review_from_resolved(
    resolved: ResolvedPhasePrompt,
    *,
    additional_context: str = "",
    updated_by: str = "system",
    updated_at: str | None = None,
) -> PromptReviewDraft:
    return PromptReviewDraft(
        system=resolved["system"],
        phase_prompt=resolved["user"],
        application=resolved["application"],
        additional_context=additional_context,
        source=dict(resolved["source"]),
        updated_at=updated_at or _utcnow().isoformat(),
        updated_by=updated_by,
    )


def resolved_from_prompt_review(draft: Mapping[str, Any]) -> ResolvedPhasePrompt:
    source = draft.get("source")
    return ResolvedPhasePrompt(
        system=str(draft.get("system") or ""),
        user=user_prompt_with_context(
            str(draft.get("phase_prompt") or ""),
            str(draft.get("additional_context") or ""),
        ),
        application=draft.get("application") if draft.get("application") is not None else None,
        source=dict(source) if isinstance(source, dict) else _source("run_override", ["run"]),
    )


class PromptResolver:
    """Resolves the (system, user, application) prompt parts for a phase.

    With no injected store it opens a throwaway engine per lookup: the sync
    graph node bridges in via asyncio.run on worker threads, so pooled
    connections must never outlive their event loop.
    """

    def __init__(self, store: ActiveVersionReader | None = None) -> None:
        self._store = store

    async def resolve_phase_prompt(
        self,
        phase: Phase,
        cfg: PipelineConfigurable,
        *,
        variables: Mapping[str, Any] | None = None,
    ) -> ResolvedPhasePrompt:
        variables = dict(variables or {})
        system_v, user_v, application_v = await self._load_catalog_prompts(phase, cfg.app_id)
        return _compose_resolved(
            phase,
            cfg,
            variables,
            system_v=system_v,
            user_v=user_v,
            application_v=application_v,
        )

    async def _load_catalog_prompts(
        self, phase: Phase, app_id: str | None
    ) -> tuple[PromptVersion | None, PromptVersion | None, PromptVersion | None]:
        try:
            async with asyncio.timeout(CATALOG_TIMEOUT_S):
                if self._store is not None:
                    return (
                        await self._store.get_active_version(
                            PHASE_NAMESPACE, f"{phase.value}/system"
                        ),
                        await self._store.get_active_version(
                            PHASE_NAMESPACE, f"{phase.value}/user"
                        ),
                        await self._store.get_active_version(APPLICATION_NAMESPACE, app_id)
                        if app_id
                        else None,
                    )
                return await _load_prompts_with_fresh_engine(phase, app_id)
        except Exception:
            # Expected when running without Postgres (langgraph dev, unit tests).
            logger.debug("apex.prompts.catalog_unavailable", phase=phase.value, exc_info=True)
            return None, None, None


async def resolve_phase_prompts(
    phases: Sequence[Phase],
    cfg: PipelineConfigurable,
    *,
    variables: Mapping[str, Any] | None = None,
    store: ActiveVersionReader | None = None,
) -> dict[Phase, ResolvedPhasePrompt]:
    """Resolve multiple phase prompts using one catalog session when possible."""
    variables = dict(variables or {})
    if store is not None:
        resolver = PromptResolver(store=store)
        return {
            phase: await resolver.resolve_phase_prompt(phase, cfg, variables=variables)
            for phase in phases
        }
    try:
        async with asyncio.timeout(CATALOG_TIMEOUT_S):
            return await _resolve_phase_prompts_with_fresh_engine(phases, cfg, variables)
    except Exception:
        logger.debug("apex.prompts.catalog_unavailable_batch", exc_info=True)
        return {
            phase: resolve_phase_prompt_no_catalog(phase, cfg, variables=variables)
            for phase in phases
        }


async def _load_prompts_with_fresh_engine(
    phase: Phase, app_id: str | None
) -> tuple[PromptVersion | None, PromptVersion | None, PromptVersion | None]:
    # Imported lazily so importing the graph module never drags engine machinery in.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from apex.persistence.repositories.prompts import PromptRepository
    from apex.settings import database_ssl_connect_args, get_settings

    database = get_settings().database
    engine = create_async_engine(
        database.uri,
        connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
    )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            repo = PromptRepository(session)
            return (
                await repo.get_active_version(PHASE_NAMESPACE, f"{phase.value}/system"),
                await repo.get_active_version(PHASE_NAMESPACE, f"{phase.value}/user"),
                await repo.get_active_version(APPLICATION_NAMESPACE, app_id) if app_id else None,
            )
    finally:
        await engine.dispose()


async def _resolve_phase_prompts_with_fresh_engine(
    phases: Sequence[Phase],
    cfg: PipelineConfigurable,
    variables: Mapping[str, Any],
) -> dict[Phase, ResolvedPhasePrompt]:
    # Imported lazily so importing the graph module never drags engine machinery in.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from apex.persistence.repositories.prompts import PromptRepository
    from apex.settings import database_ssl_connect_args, get_settings

    database = get_settings().database
    engine = create_async_engine(
        database.uri,
        connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
    )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            resolver = PromptResolver(store=PromptRepository(session))
            return {
                phase: await resolver.resolve_phase_prompt(phase, cfg, variables=variables)
                for phase in phases
            }
    finally:
        await engine.dispose()


async def resolve_phase_prompt(
    phase: Phase,
    cfg: PipelineConfigurable,
    *,
    variables: Mapping[str, Any] | None = None,
    store: ActiveVersionReader | None = None,
) -> ResolvedPhasePrompt:
    """Module-level convenience wrapper around PromptResolver."""
    return await PromptResolver(store=store).resolve_phase_prompt(phase, cfg, variables=variables)


def resolve_phase_prompt_sync(
    phase: Phase,
    cfg: PipelineConfigurable,
    *,
    variables: Mapping[str, Any] | None = None,
) -> ResolvedPhasePrompt:
    """Bridge for sync graph nodes (async nodes cannot run under sync invoke).

    Sync nodes execute on worker threads with no running event loop, where
    asyncio.run is safe. If a loop *is* running in this thread (defensive),
    skip catalog IO rather than block the loop: override -> builtin only.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(resolve_phase_prompt(phase, cfg, variables=variables))
    return resolve_phase_prompt_no_catalog(phase, cfg, variables=variables)


def resolve_phase_prompts_sync(
    phases: Sequence[Phase],
    cfg: PipelineConfigurable,
    *,
    variables: Mapping[str, Any] | None = None,
) -> dict[Phase, ResolvedPhasePrompt]:
    """Sync bridge for batch plan-resolver seeding."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(resolve_phase_prompts(phases, cfg, variables=variables))
    return {
        phase: resolve_phase_prompt_no_catalog(phase, cfg, variables=variables) for phase in phases
    }


__all__ = [
    "CATALOG_TIMEOUT_S",
    "DEFAULT_PHASE_PROMPTS",
    "DEFAULT_USER_TEMPLATE",
    "APPLICATION_NAMESPACE",
    "ADDITIONAL_CONTEXT_DELIMITER",
    "PHASE_NAMESPACE",
    "ActiveVersionReader",
    "DuplicatePromptError",
    "PromptCatalogService",
    "PromptError",
    "PromptNotFoundError",
    "PromptReviewDraft",
    "PromptResolver",
    "PromptStore",
    "PromptVersionMismatchError",
    "PromptVersionNotFoundError",
    "ResolvedPhasePrompt",
    "prompt_review_from_resolved",
    "render_template",
    "resolved_from_prompt_review",
    "resolve_phase_prompt",
    "resolve_phase_prompt_no_catalog",
    "resolve_phase_prompt_sync",
    "resolve_phase_prompts",
    "resolve_phase_prompts_sync",
    "user_prompt_with_context",
]
