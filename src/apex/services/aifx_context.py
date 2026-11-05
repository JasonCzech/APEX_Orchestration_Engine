"""Read AIFx Context Services repositories at a pinned commit.

Shared by the admin preview (a dry run that reports every problem with its
repository, path and line) and by launch, which freezes the result into the
run as an ``AifxSnapshotV1``. Which repositories a test reads:

* global rules from the primary system's organization (or, if the primary
  system is not onboarded, the first onboarded participant's organization);
* team documents of the primary system's APM;
* the ACB (application context) of every onboarded participating application.

Nothing here prints file content in an error. Credential-shaped text is
rejected (the same scanner every model input goes through), and the issue
names the file and line so the repository owner can fix it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from apex.adapters.registry import PortKind
from apex.domain.aifx import (
    MANIFEST_PATH,
    MAX_SNAPSHOT_ENTRIES,
    AifxEntry,
    AifxManifestError,
    AifxRenderError,
    AifxRole,
    AifxSnapshotStatus,
    AifxSnapshotV1,
    AifxSource,
    RenderedAifx,
    parse_manifest,
    render_aifx,
    text_sha256,
)
from apex.domain.diagnostics import contains_credential_material
from apex.domain.integrations import RepoRef
from apex.persistence.models import AifxApplicationSource, AifxOrganization
from apex.settings import AifxSettings

_MAX_LINES_REPORTED = 5
_MAX_ISSUES = 32


@dataclass(frozen=True)
class SourcePlan:
    """One repository to read: its role, owner/name, ref and connection."""

    role: AifxRole
    owner: str
    repo: str
    ref: str
    connection_id: str
    application_id: str | None = None
    application_name: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class AifxIssue:
    repo: str
    message: str
    path: str | None = None
    lines: tuple[int, ...] = ()

    def text(self) -> str:
        where = self.repo
        if self.path:
            where += f":{self.path}"
        if self.lines:
            where += ":" + ",".join(str(line) for line in self.lines)
        return f"{where} — {self.message}"[:500]


@dataclass
class SourceRead:
    plan: SourcePlan
    source: AifxSource | None = None
    entries: list[AifxEntry] = field(default_factory=list)
    issues: list[AifxIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.source is not None and not self.issues


class AifxUnavailableError(Exception):
    """A required AIFx source could not be pinned; ``issues`` say where and why."""

    def __init__(self, issues: Sequence[AifxIssue]) -> None:
        self.issues = list(issues)[:_MAX_ISSUES]
        first = "; ".join(issue.text() for issue in self.issues[:3])
        more = f" (+{len(self.issues) - 3} more)" if len(self.issues) > 3 else ""
        super().__init__(f"AIFx context could not be pinned: {first}{more}")


# --- planning ---------------------------------------------------------------------


def plan_sources(
    *,
    application_ids: Sequence[str],
    primary_application_id: str | None,
    mappings: Sequence[AifxApplicationSource],
    organizations: Mapping[str, AifxOrganization],
    application_names: Mapping[str, str],
    settings: AifxSettings,
) -> list[SourcePlan]:
    """Choose the repositories a test with these participants reads, in prompt order."""

    usable: dict[str, tuple[AifxApplicationSource, AifxOrganization]] = {}
    for mapping in mappings:
        org = organizations.get(mapping.organization_id)
        if mapping.enabled and org is not None and org.enabled:
            usable[mapping.application_id] = (mapping, org)
    ordered = [app_id for app_id in dict.fromkeys(application_ids) if app_id in usable]
    if not ordered:
        return []
    lead = primary_application_id if primary_application_id in usable else ordered[0]
    lead_mapping, lead_org = usable[lead]
    plans: list[SourcePlan] = []
    if lead_org.global_rules_repo:
        plans.append(
            SourcePlan(
                role=AifxRole.GLOBAL_RULES,
                owner=lead_org.ghe_owner,
                repo=lead_org.global_rules_repo,
                ref=lead_org.default_ref,
                connection_id=lead_org.connection_id,
            )
        )
    if lead == primary_application_id and lead_mapping.team_documents_repo:
        plans.append(
            SourcePlan(
                role=AifxRole.TEAM_DOCUMENTS,
                owner=lead_org.ghe_owner,
                repo=lead_mapping.team_documents_repo,
                ref=lead_mapping.ref or lead_org.default_ref,
                connection_id=lead_org.connection_id,
                application_id=lead,
                application_name=application_names.get(lead),
            )
        )
    seen_acb: set[tuple[str, str]] = set()
    # Primary first: a shared ACB is read at the primary system's ref.
    for app_id in [lead, *(app_id for app_id in ordered if app_id != lead)]:
        mapping, org = usable[app_id]
        if mapping.acb_repo:
            ref = mapping.ref or org.default_ref
            key = (org.ghe_owner.lower(), mapping.acb_repo.lower())
            if key in seen_acb:
                # Two participants mapped to one ACB: read and render it once, at
                # the first (primary-first) participant's ref, so entry ids stay
                # unique as "owner/repo:id" in the audit and at the gate.
                continue
            seen_acb.add(key)
            plans.append(
                SourcePlan(
                    role=AifxRole.APPLICATION_CONTEXT,
                    owner=org.ghe_owner,
                    repo=mapping.acb_repo,
                    ref=ref,
                    connection_id=org.connection_id,
                    application_id=app_id,
                    application_name=application_names.get(app_id),
                )
            )
    return plans


def check_source_count(plans: Sequence[SourcePlan], settings: AifxSettings) -> None:
    """Refuse rather than silently drop sources past the configured limit."""

    if len(plans) > settings.max_sources:
        raise AifxUnavailableError(
            [
                AifxIssue(
                    repo="AIFx context",
                    message=(
                        f"this test maps {len(plans)} repositories; the limit is "
                        f"{settings.max_sources} (APEX_AIFX__MAX_SOURCES)"
                    ),
                )
            ]
        )


# --- reading ----------------------------------------------------------------------


def credential_lines(text: str) -> tuple[int, ...]:
    """1-based lines the credential scanner objects to (empty when the text is clean)."""

    if not contains_credential_material(text):
        return ()
    lines = text.splitlines()
    hits = [number for number, line in enumerate(lines, 1) if contains_credential_material(line)]
    if not hits:
        # Multi-line shapes (a PEM block) only match as a whole.
        hits = [number for number, line in enumerate(lines, 1) if "-----BEGIN" in line] or [1]
    return tuple(hits[:_MAX_LINES_REPORTED])


def web_base_for(provider: str, options: Mapping[str, Any]) -> str | None:
    """Browser URL root for linking a pinned source, e.g. https://ghe.example.com."""

    if provider != "github_enterprise":
        return None
    base = options.get("base_url")
    if not isinstance(base, str):
        return None
    parsed = urlsplit(base)
    if parsed.hostname == "api.github.com":
        return "https://github.com"
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str):
        return exc.args[0][:300]
    if isinstance(exc, (RuntimeError, ValueError)) and str(exc):
        return str(exc)[:300]
    return "the repository could not be read"


async def read_source(
    adapter: Any,
    plan: SourcePlan,
    *,
    settings: AifxSettings,
    web_base: str | None = None,
) -> SourceRead:
    """Pin one repository and read its manifest and entries; collect, don't raise."""

    result = SourceRead(plan=plan)
    repo = RepoRef(owner=plan.owner, name=plan.repo)
    full = plan.full_name
    try:
        commit = await adapter.resolve_commit(repo, plan.ref)
        manifest_file = await adapter.get_file(repo, MANIFEST_PATH, commit)
    except Exception as exc:  # noqa: BLE001 - adapters raise safe, bounded messages
        message = _error_message(exc)
        if isinstance(exc, KeyError) and "aifx.yaml" not in message:
            message = f"{message} (check the repository name, ref and that aifx.yaml exists)"
        result.issues.append(AifxIssue(repo=full, message=message))
        return result
    manifest_text = manifest_file.text
    if len(manifest_text.encode("utf-8")) > settings.max_manifest_bytes:
        result.issues.append(
            AifxIssue(
                repo=full,
                path=MANIFEST_PATH,
                message=f"manifest exceeds {settings.max_manifest_bytes:,} bytes",
            )
        )
        return result
    lines = credential_lines(manifest_text)
    if lines:
        result.issues.append(
            AifxIssue(
                repo=full,
                path=MANIFEST_PATH,
                lines=lines,
                message="text looks like a credential; remove or rephrase it",
            )
        )
        return result
    try:
        manifest = parse_manifest(manifest_text, expected_role=plan.role)
    except AifxManifestError as exc:
        result.issues.append(AifxIssue(repo=full, path=MANIFEST_PATH, message=str(exc)))
        return result
    if len(manifest.entries) > settings.max_entries_per_source:
        result.issues.append(
            AifxIssue(
                repo=full,
                path=MANIFEST_PATH,
                message=(
                    f"declares {len(manifest.entries)} entries; "
                    f"the limit is {settings.max_entries_per_source}"
                ),
            )
        )
        return result

    semaphore = asyncio.Semaphore(settings.fetch_concurrency)

    async def read_entry(entry_path: str) -> str | AifxIssue:
        async with semaphore:
            try:
                file = await adapter.get_file(repo, entry_path, commit)
            except Exception as exc:  # noqa: BLE001
                return AifxIssue(repo=full, path=entry_path, message=_error_message(exc))
        if len(file.text) > settings.max_file_chars:
            return AifxIssue(
                repo=full,
                path=entry_path,
                message=f"file exceeds {settings.max_file_chars:,} characters",
            )
        found = credential_lines(file.text)
        if found:
            return AifxIssue(
                repo=full,
                path=entry_path,
                lines=found,
                message="text looks like a credential; remove or rephrase it",
            )
        return file.text

    contents = await asyncio.gather(*(read_entry(entry.path) for entry in manifest.entries))
    for entry, content in zip(manifest.entries, contents, strict=True):
        if isinstance(content, AifxIssue):
            result.issues.append(content)
            continue
        result.entries.append(
            AifxEntry(
                id=entry.id,
                role=plan.role,
                application_id=plan.application_id,
                repo=full,
                type=entry.type,
                phases=entry.phases,
                title=entry.title,
                description=entry.description,
                path=entry.path,
                content_sha256=text_sha256(content),
                content=content,
            )
        )
    result.source = AifxSource(
        role=plan.role,
        application_id=plan.application_id,
        application_name=plan.application_name,
        repo=full,
        ref=plan.ref,
        commit_sha=commit,
        manifest_sha256=text_sha256(manifest_text),
        web_url=f"{web_base}/{full}/tree/{commit}" if web_base else None,
    )
    return result


async def read_sources(
    plans: Sequence[SourcePlan],
    adapters: Mapping[str, Any],
    *,
    settings: AifxSettings,
    web_bases: Mapping[str, str | None] | None = None,
    connection_failures: Mapping[str, str] | None = None,
) -> list[SourceRead]:
    """Read every planned source concurrently under one overall deadline."""

    web_bases = web_bases or {}
    connection_failures = connection_failures or {}

    async def one(plan: SourcePlan) -> SourceRead:
        adapter = adapters.get(plan.connection_id)
        if adapter is None:
            reason = connection_failures.get(plan.connection_id, "its connection is unavailable")
            return SourceRead(plan=plan, issues=[AifxIssue(repo=plan.full_name, message=reason)])
        return await read_source(
            adapter, plan, settings=settings, web_base=web_bases.get(plan.connection_id)
        )

    tasks = [asyncio.ensure_future(one(plan)) for plan in plans]
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks, timeout=settings.fetch_timeout_s)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    reads: list[SourceRead] = []
    for plan, task in zip(plans, tasks, strict=True):
        if task in done and not task.cancelled() and task.exception() is None:
            reads.append(task.result())
            continue
        message = (
            f"no response within {settings.fetch_timeout_s:g}s"
            if task in pending
            else "the repository could not be read"
        )
        reads.append(
            SourceRead(plan=plan, issues=[AifxIssue(repo=plan.full_name, message=message)])
        )
    return reads


def seal_snapshot(
    reads: Sequence[SourceRead], *, settings: AifxSettings, require_all: bool
) -> AifxSnapshotV1 | None:
    """Freeze what was read. Locked environments refuse partial context."""

    issues = [issue for read in reads for issue in read.issues]
    if issues and require_all:
        raise AifxUnavailableError(issues)
    total = sum(len(entry.content) for read in reads for entry in read.entries)
    if total > settings.max_snapshot_chars:
        raise AifxUnavailableError(
            [
                AifxIssue(
                    repo="AIFx context",
                    message=(
                        f"{total:,} characters across all sources exceeds the "
                        f"{settings.max_snapshot_chars:,}-character snapshot limit"
                    ),
                )
            ]
        )
    sources = [read.source for read in reads if read.source is not None]
    if not sources and not issues:
        return None
    pinned_keys = {source.key() for source in sources}
    entries = [
        entry for read in reads for entry in read.entries if entry.source_key() in pinned_keys
    ]
    if len(entries) > MAX_SNAPSHOT_ENTRIES:
        raise AifxUnavailableError(
            [
                AifxIssue(
                    repo="AIFx context",
                    message=(
                        f"{len(entries)} entries across all sources exceeds the "
                        f"{MAX_SNAPSHOT_ENTRIES}-entry snapshot limit"
                    ),
                )
            ]
        )
    try:
        return AifxSnapshotV1.seal(
            status=AifxSnapshotStatus.DEGRADED if issues else AifxSnapshotStatus.PINNED,
            sources=sources,
            entries=entries,
            issues=[issue.text() for issue in issues[:_MAX_ISSUES]],
        )
    except ValueError:
        raise AifxUnavailableError(
            [AifxIssue(repo="AIFx context", message="the pinned sources could not be sealed")]
        ) from None


# --- connections ------------------------------------------------------------------


async def open_adapters(
    resolver: Any, connection_ids: Sequence[str]
) -> tuple[dict[str, Any], dict[str, str | None], dict[str, str]]:
    """Build one source-control adapter per connection; callers close them.

    Returns the adapters, their browser URL roots, and why any connection
    could not be opened (keyed by connection id).
    """

    adapters: dict[str, Any] = {}
    web_bases: dict[str, str | None] = {}
    failures: dict[str, str] = {}
    for connection_id in dict.fromkeys(connection_ids):
        try:
            metadata = await resolver.resolve_metadata(
                PortKind.SOURCE_CONTROL, connection_id=connection_id, project_id=None
            )
            if metadata.internal_project_id is not None:
                raise ValueError("AIFx needs a global (not project-scoped) connection")
            resolved = await resolver.build_from_metadata(metadata)
        except Exception:  # noqa: BLE001 - resolver errors can name secrets and scopes
            failures[connection_id] = (
                "its connection could not be opened; test it on the Connections page"
            )
            continue
        adapters[connection_id] = resolved
        web_bases[connection_id] = web_base_for(metadata.config.provider, metadata.config.options)
    return adapters, web_bases, failures


async def close_adapters(adapters: Mapping[str, Any]) -> None:
    from apex.services.connections import close_adapter

    for adapter in adapters.values():
        try:
            await close_adapter(adapter)
        except Exception:  # noqa: BLE001, S110 - best-effort release
            pass


# --- launch -----------------------------------------------------------------------

#: The credential scanner treats a configuration over ~1M characters as hostile,
#: so give an oversized launch a clear error first.
MAX_CONFIGURABLE_CHARS = 900_000


async def load_onboarding(
    application_ids: Sequence[str],
) -> tuple[list[AifxApplicationSource], dict[str, AifxOrganization], dict[str, str]]:
    """Mappings, their organizations and application names for these applications."""

    from sqlalchemy import select

    from apex.persistence.db import get_sessionmaker
    from apex.persistence.models import Application
    from apex.persistence.repositories.aifx import AifxRepository

    async with get_sessionmaker()() as session:
        repo = AifxRepository(session)
        mappings = list(await repo.list_sources(application_ids))
        if not mappings:
            return [], {}, {}
        organization_ids = {mapping.organization_id for mapping in mappings}
        organizations = {
            org.id: org for org in await repo.list_organizations() if org.id in organization_ids
        }
        rows = await session.execute(
            select(Application.id, Application.name).where(Application.id.in_(application_ids))
        )
        return mappings, organizations, dict(rows.tuples())


async def prepare_aifx_snapshot(configurable: dict[str, Any]) -> None:
    """Pin the test's AIFx sources and write ``configurable["aifx_snapshot"]``.

    A no-op when AIFx is disabled or no participating application is onboarded.
    Locked environments refuse to launch on partial context; dev environments
    record a ``degraded`` snapshot that lists what could not be read.
    """

    from apex.graphs.pipeline.configurable import PipelineConfigurable
    from apex.services.connections import get_connection_resolver
    from apex.settings import get_settings

    settings = get_settings()
    if not settings.aifx_enabled:
        return
    cfg = PipelineConfigurable.model_validate(
        {
            key: value
            for key, value in configurable.items()
            if key in PipelineConfigurable.model_fields
        }
    )
    if cfg.test_scope is not None:
        application_ids = [
            participant.application_id for participant in cfg.test_scope.participants
        ]
        primary = cfg.test_scope.primary.application_id
    else:
        application_ids = [cfg.app_id] if cfg.app_id else []
        primary = cfg.app_id
    if not application_ids:
        return
    mappings, organizations, names = await load_onboarding(application_ids)
    plans = plan_sources(
        application_ids=application_ids,
        primary_application_id=primary,
        mappings=mappings,
        organizations=organizations,
        application_names=names,
        settings=settings.aifx,
    )
    if not plans:
        return
    check_source_count(plans, settings.aifx)
    adapters, web_bases, failures = await open_adapters(
        get_connection_resolver(), [plan.connection_id for plan in plans]
    )
    try:
        reads = await read_sources(
            plans,
            adapters,
            settings=settings.aifx,
            web_bases=web_bases,
            connection_failures=failures,
        )
    finally:
        await close_adapters(adapters)
    snapshot = seal_snapshot(
        reads,
        settings=settings.aifx,
        require_all=settings.is_locked_down and settings.aifx.required_in_locked,
    )
    if snapshot is not None:
        check_phase_budgets(snapshot)
        configurable["aifx_snapshot"] = snapshot.model_dump(mode="json")


def check_configurable_size(configurable: dict[str, Any]) -> None:
    """Fail with a clear message before the oversize scan misreports credentials."""

    import json

    size = len(json.dumps(configurable, ensure_ascii=False, default=str))
    if size > MAX_CONFIGURABLE_CHARS:
        raise AifxUnavailableError(
            [
                AifxIssue(
                    repo="launch configuration",
                    message=(
                        f"{size:,} characters exceeds the {MAX_CONFIGURABLE_CHARS:,}-character "
                        "limit; trim AIFx sources or selected prompts"
                    ),
                )
            ]
        )


# --- per-phase rendering and gate metadata ---------------------------------------

_GATE_MAX_ENTRIES = 64
_GATE_MAX_ISSUES = 8


def render_phase_aifx(snapshot: AifxSnapshotV1 | None, phase: Any) -> RenderedAifx | None:
    """This phase's AIFx prompt parts under the configured per-phase budgets."""

    if snapshot is None:
        return None
    from apex.domain.prompt_catalog import AIFX_PREAMBLE
    from apex.settings import get_settings

    settings = get_settings().aifx
    return render_aifx(
        snapshot,
        phase,
        preamble=AIFX_PREAMBLE,
        max_system_chars=settings.max_phase_system_chars,
        max_memory_chars=settings.max_phase_memory_chars,
    )


#: A phase result records its applicable entry ids; the checkpoint reducer caps
#: any list in a phase entry at 256 items.
MAX_PHASE_ENTRIES = 256


def check_phase_budgets(snapshot: AifxSnapshotV1, phases: Sequence[Any] | None = None) -> None:
    """Fail the launch, not a later gate, when a phase's AIFx content is too large.

    Checks every pipeline phase by default: a rerun inherits the snapshot and
    may run phases the original launch did not plan.
    """

    from apex.domain.pipeline import PHASE_ORDER, Phase

    issues: list[AifxIssue] = []
    for phase in PHASE_ORDER if phases is None else phases:
        applicable = sum(1 for entry in snapshot.entries if entry.applies_to(Phase(phase)))
        if applicable > MAX_PHASE_ENTRIES:
            issues.append(
                AifxIssue(
                    repo="AIFx context",
                    message=(
                        f"{applicable} entries apply to {Phase(phase).value}; "
                        f"the per-phase limit is {MAX_PHASE_ENTRIES}"
                    ),
                )
            )
            continue
        try:
            render_phase_aifx(snapshot, phase)
        except AifxRenderError as exc:
            issues.append(AifxIssue(repo="AIFx context", message=str(exc)))
    if issues:
        raise AifxUnavailableError(issues)


def gate_metadata(snapshot: AifxSnapshotV1 | None, phase: Any) -> dict[str, Any] | None:
    """What the prompt-review gate shows about AIFx: sources and titles, never content."""

    if snapshot is None:
        return None
    from apex.domain.pipeline import Phase

    applicable = [entry for entry in snapshot.entries if entry.applies_to(Phase(phase))]
    return {
        "status": snapshot.status.value,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "sources": [
            {
                "role": source.role.value,
                "repo": source.repo,
                "ref": source.ref,
                "commit_sha": source.commit_sha,
                "web_url": source.web_url,
                "application_name": source.application_name,
            }
            for source in snapshot.sources
        ],
        "entries": [
            {
                "id": entry.id,
                "type": entry.type.value,
                "title": entry.title,
                "repo": entry.repo,
                "path": entry.path,
            }
            for entry in applicable[:_GATE_MAX_ENTRIES]
        ],
        "issues": list(snapshot.issues[:_GATE_MAX_ISSUES]),
        # Totals, so a truncated list is never presented as complete.
        "entry_count": len(applicable),
        "issue_count": len(snapshot.issues),
    }
