"""AIFx Context Services: repository manifests and the per-run pinned snapshot.

AIFx repositories on GitHub Enterprise hold the rules, memories, skills and
application context that shape the phase agents. Each repository declares its
role and readable files in an ``aifx.yaml`` manifest::

    schema: aifx/v1
    role: global-rules | team-documents | application-context
    entries:
      - id: perf-standards              # [a-z0-9-], unique per repository
        type: rule | memory | skill | context
        path: rules/perf-standards.md   # relative .md/.markdown/.txt file
        phases: all                     # or a list of pipeline phases
        title: Performance standards
        description: ...                # required for skills (the skill index)

At launch the orchestrator resolves every mapped repository to a commit SHA,
reads the manifest and its entries at that SHA, and freezes the result into the
run as an ``AifxSnapshotV1``. The snapshot is self-verifying (content and
snapshot SHA-256), so a tampered checkpoint or client-supplied copy is
rejected, and resumes never read GitHub again.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from apex.domain.input_limits import NoNulStr
from apex.domain.pipeline import Phase
from apex.domain.safe_yaml import SafeYamlError, load_yaml_safely

AIFX_SCHEMA = "aifx/v1"
MANIFEST_PATH = "aifx.yaml"
MAX_MANIFEST_ENTRIES = 200
MAX_ENTRY_CHARS = 100_000
MAX_SNAPSHOT_SOURCES = 16
MAX_SNAPSHOT_ENTRIES = 400
MAX_SNAPSHOT_ISSUES = 32
_MANIFEST_MAX_DEPTH = 8
_MANIFEST_MAX_NODES = 4_000

_ENTRY_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_READABLE_SUFFIXES = (".md", ".markdown", ".txt")

#: GitHub owner and repository names (also used for convention suggestions).
REPO_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
#: Branch or tag names a mapping may follow.
REF_RE = re.compile(r"[A-Za-z0-9._/-]{1,255}")

Sha1 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class AifxRole(StrEnum):
    GLOBAL_RULES = "global-rules"
    TEAM_DOCUMENTS = "team-documents"
    APPLICATION_CONTEXT = "application-context"
    # Reserved for the Environment and Performance Phase Context Builders.
    ENVIRONMENT_CONTEXT = "environment-context"
    PHASE_CONTEXT = "phase-context"


SUPPORTED_ROLES = frozenset(
    {AifxRole.GLOBAL_RULES, AifxRole.TEAM_DOCUMENTS, AifxRole.APPLICATION_CONTEXT}
)


class AifxEntryType(StrEnum):
    RULE = "rule"
    MEMORY = "memory"
    SKILL = "skill"
    CONTEXT = "context"


class AifxManifestError(ValueError):
    """The manifest is unusable; the message names the problem, never file content."""


def validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 1_024:
        raise ValueError("Entry paths must be 1-1024 characters")
    if value.startswith("/") or "\\" in value:
        raise ValueError("Entry paths must be relative and use '/' separators")
    if any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise ValueError("Entry paths cannot contain empty, '.' or '..' segments")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("Entry paths cannot contain control characters")
    return value


def validate_repo_name(value: Any, *, label: str = "Repository") -> str:
    if not isinstance(value, str) or REPO_NAME_RE.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError(f"{label} must use letters, digits, '.', '_' or '-' (max 100)")
    return value


def validate_branch_ref(value: Any) -> str:
    if (
        not isinstance(value, str)
        or REF_RE.fullmatch(value) is None
        or ".." in value
        or "//" in value
        or value.startswith(("/", "-", "."))
        or value.endswith(("/", ".", ".lock"))
    ):
        raise ValueError("Use a branch, tag or commit SHA without spaces or '..'")
    return value


class AifxManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str
    type: AifxEntryType
    path: str
    phases: Literal["all"] | list[Phase] = "all"
    title: NoNulStr = Field(min_length=1, max_length=200)
    description: NoNulStr | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if _ENTRY_ID_RE.fullmatch(value) is None:
            raise ValueError("Entry ids use lowercase letters, digits and '-' (max 64)")
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        value = validate_relative_path(value)
        if value == MANIFEST_PATH or not value.lower().endswith(_READABLE_SUFFIXES):
            raise ValueError("Entries must point at a .md, .markdown or .txt file")
        return value

    @field_validator("phases")
    @classmethod
    def _phases(cls, value: Literal["all"] | list[Phase]) -> Literal["all"] | list[Phase]:
        if isinstance(value, list) and (not value or len(set(value)) != len(value)):
            raise ValueError("List each phase once, or use 'all'")
        return value

    @model_validator(mode="after")
    def _skill_description(self) -> AifxManifestEntry:
        if self.type is AifxEntryType.SKILL and self.description is None:
            raise ValueError("Skills need a description for the skill index")
        return self


class AifxManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, populate_by_name=True)

    schema_: Literal["aifx/v1"] = Field(alias="schema")
    role: AifxRole
    entries: list[AifxManifestEntry] = Field(default_factory=list, max_length=MAX_MANIFEST_ENTRIES)

    @model_validator(mode="after")
    def _unique(self) -> AifxManifest:
        if len({entry.id for entry in self.entries}) != len(self.entries):
            raise ValueError("Entry ids must be unique")
        if len({entry.path for entry in self.entries}) != len(self.entries):
            raise ValueError("Each file may be declared once")
        return self


def _first_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        try:
            first: Any = exc.errors()[0]
        except IndexError:
            return "invalid manifest"
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "__root__")
        message = str(first.get("msg", "invalid value")).removeprefix("Value error, ")
        return f"{location}: {message}" if location else message
    return str(exc)


def parse_manifest(text: str, *, expected_role: AifxRole | None = None) -> AifxManifest:
    """Parse and validate ``aifx.yaml`` text; raise ``AifxManifestError`` on any problem."""

    try:
        data = load_yaml_safely(
            text,
            subject="aifx.yaml",
            max_depth=_MANIFEST_MAX_DEPTH,
            max_nodes=_MANIFEST_MAX_NODES,
        )
    except SafeYamlError as exc:
        raise AifxManifestError(str(exc)) from None
    if not isinstance(data, dict):
        raise AifxManifestError("aifx.yaml must be a mapping with schema, role and entries")
    try:
        manifest = AifxManifest.model_validate(data)
    except ValueError as exc:
        raise AifxManifestError(f"aifx.yaml {_first_error(exc)}") from None
    if manifest.role not in SUPPORTED_ROLES:
        raise AifxManifestError(f"aifx.yaml role '{manifest.role}' is not supported yet")
    if expected_role is not None and manifest.role is not expected_role:
        raise AifxManifestError(
            f"aifx.yaml declares role '{manifest.role}' but this repository is mapped as "
            f"'{expected_role}'"
        )
    return manifest


# --- conventions -------------------------------------------------------------------


def suggest_repo(pattern: str | None, apm_id: str) -> str | None:
    """Fill ``{apm}`` in an organization's naming convention; None when unusable."""

    if not pattern:
        return None
    candidate = pattern.replace("{apm}", apm_id.lower())
    try:
        return validate_repo_name(candidate)
    except ValueError:
        return None


# --- the pinned snapshot -----------------------------------------------------------


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AifxSnapshotStatus(StrEnum):
    PINNED = "pinned"
    # Dev-only: a source could not be read and the launch went ahead without it.
    DEGRADED = "degraded"


class AifxSource(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    role: AifxRole
    application_id: NoNulStr | None = Field(default=None, min_length=1, max_length=64)
    application_name: NoNulStr | None = Field(default=None, min_length=1, max_length=255)
    repo: NoNulStr = Field(min_length=3, max_length=201)  # owner/name
    ref: NoNulStr = Field(min_length=1, max_length=255)
    commit_sha: Sha1
    manifest_sha256: Sha256
    web_url: NoNulStr | None = Field(default=None, max_length=2_048)

    def key(self) -> tuple[str, str | None, str]:
        return (self.role.value, self.application_id, self.repo)


class AifxEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str = Field(min_length=1, max_length=64)
    role: AifxRole
    application_id: NoNulStr | None = Field(default=None, min_length=1, max_length=64)
    repo: NoNulStr = Field(min_length=3, max_length=201)
    type: AifxEntryType
    phases: Literal["all"] | list[Phase] = "all"
    title: NoNulStr = Field(min_length=1, max_length=200)
    description: NoNulStr | None = Field(default=None, max_length=500)
    path: NoNulStr = Field(min_length=1, max_length=1_024)
    content_sha256: Sha256
    content: NoNulStr = Field(max_length=MAX_ENTRY_CHARS)

    def source_key(self) -> tuple[str, str | None, str]:
        return (self.role.value, self.application_id, self.repo)

    def applies_to(self, phase: Phase | str) -> bool:
        return self.phases == "all" or Phase(phase) in self.phases


class AifxSnapshotV1(BaseModel):
    """Everything AIFx contributed to one run, frozen at launch."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    status: AifxSnapshotStatus = AifxSnapshotStatus.PINNED
    sources: list[AifxSource] = Field(default_factory=list, max_length=MAX_SNAPSHOT_SOURCES)
    entries: list[AifxEntry] = Field(default_factory=list, max_length=MAX_SNAPSHOT_ENTRIES)
    issues: list[Annotated[NoNulStr, Field(max_length=500)]] = Field(
        default_factory=list, max_length=MAX_SNAPSHOT_ISSUES
    )
    snapshot_sha256: Sha256

    @classmethod
    def seal(cls, **fields: Any) -> AifxSnapshotV1:
        """Build a snapshot and compute its integrity hash."""

        body = {
            "schema_version": 1,
            "status": AifxSnapshotStatus(fields.get("status", AifxSnapshotStatus.PINNED)).value,
            "sources": [
                AifxSource.model_validate(source).model_dump(mode="json")
                for source in fields.get("sources", [])
            ],
            "entries": [
                AifxEntry.model_validate(entry).model_dump(mode="json")
                for entry in fields.get("entries", [])
            ],
            "issues": list(fields.get("issues", [])),
        }
        return cls.model_validate({**body, "snapshot_sha256": canonical_sha256(body)})

    @model_validator(mode="after")
    def _integrity(self) -> AifxSnapshotV1:
        keys = [source.key() for source in self.sources]
        if len(set(keys)) != len(keys):
            raise ValueError("AIFx snapshot lists a source twice")
        known = set(keys)
        seen: set[tuple[str, str | None, str, str]] = set()
        for entry in self.entries:
            if entry.source_key() not in known:
                raise ValueError("AIFx snapshot entry has no matching source")
            identity = (*entry.source_key(), entry.id)
            if identity in seen:
                raise ValueError("AIFx snapshot lists an entry twice")
            seen.add(identity)
            if text_sha256(entry.content) != entry.content_sha256:
                raise ValueError("AIFx snapshot entry content does not match its hash")
        if self.status is AifxSnapshotStatus.DEGRADED and not self.issues:
            raise ValueError("A degraded AIFx snapshot must list its issues")
        body = self.model_dump(mode="json", exclude={"snapshot_sha256"})
        if canonical_sha256(body) != self.snapshot_sha256:
            raise ValueError("AIFx snapshot does not match its integrity hash")
        return self

    def application_ids(self) -> set[str]:
        return {source.application_id for source in self.sources if source.application_id}

    def total_chars(self) -> int:
        return sum(len(entry.content) for entry in self.entries)


# --- rendering into a phase prompt -------------------------------------------------

_ROLE_HEADINGS = {
    AifxRole.GLOBAL_RULES: "AIFX GLOBAL RULES",
    AifxRole.TEAM_DOCUMENTS: "AIFX TEAM RULES",
    AifxRole.APPLICATION_CONTEXT: "AIFX APPLICATION CONTEXT (ACB)",
}


class AifxRenderError(ValueError):
    """The pinned content for a phase exceeds its prompt budget."""


@dataclass(frozen=True)
class RenderedAifx:
    """The AIFx parts of one phase's model input, plus what they were built from."""

    #: Rules, team documents and skills: placed after the platform policy.
    system_head: str
    #: ACB application context: placed after the catalog application contexts.
    system_application: str
    #: Memories as (heading, content) pairs, fenced as evidence in the user text.
    memories: tuple[tuple[str, str], ...]
    entry_ids: tuple[str, ...]
    snapshot_sha256: str

    @property
    def system_chars(self) -> int:
        return len(self.system_head) + len(self.system_application)


def _source_label(source: AifxSource) -> str:
    label = f"{source.repo}@{source.commit_sha[:7]}"
    if source.role is AifxRole.APPLICATION_CONTEXT and source.application_name:
        return f"{source.application_name} — {label}"
    return label


def render_aifx(
    snapshot: AifxSnapshotV1,
    phase: Phase | str,
    *,
    preamble: str,
    max_system_chars: int,
    max_memory_chars: int,
) -> RenderedAifx | None:
    """Select this phase's entries and lay them out; None when nothing applies."""

    phase = Phase(phase)
    applicable = [entry for entry in snapshot.entries if entry.applies_to(phase)]
    if not applicable:
        return None
    sources = {source.key(): source for source in snapshot.sources}
    head: list[str] = []
    application: list[str] = []
    skills: list[str] = []
    memories: list[tuple[str, str]] = []
    for source in snapshot.sources:
        own = [entry for entry in applicable if entry.source_key() == source.key()]
        body = "\n\n".join(
            f"--- {entry.title} ({entry.path}) ---\n{entry.content.strip()}"
            for entry in own
            if entry.type in (AifxEntryType.RULE, AifxEntryType.CONTEXT)
        )
        if body:
            section = f"=== {_ROLE_HEADINGS[source.role]}: {_source_label(source)} ===\n{body}"
            (application if source.role is AifxRole.APPLICATION_CONTEXT else head).append(section)
    for entry in applicable:
        source = sources[entry.source_key()]
        if entry.type is AifxEntryType.SKILL:
            skills.append(
                f"--- Skill: {entry.title} — {entry.description} "
                f"({entry.repo}:{entry.path}) ---\n{entry.content.strip()}"
            )
        elif entry.type is AifxEntryType.MEMORY:
            memories.append(
                (
                    f"{entry.title} ({_source_label(source)}:{entry.path})",
                    entry.content.strip(),
                )
            )
    if skills:
        head.append("=== AIFX SKILLS (apply when relevant) ===\n" + "\n\n".join(skills))
    system_head = ""
    if head or application:
        # The preamble states precedence and ACB authority, so it must lead any
        # AIFx instruction text, including a phase whose only content is ACB.
        system_head = "\n\n".join([f"=== AIFX CONTEXT SERVICES ===\n{preamble}", *head])
    rendered = RenderedAifx(
        system_head=system_head,
        system_application="\n\n".join(application),
        memories=tuple(memories),
        entry_ids=tuple(f"{entry.repo}:{entry.id}" for entry in applicable),
        snapshot_sha256=snapshot.snapshot_sha256,
    )
    if rendered.system_chars > max_system_chars:
        raise AifxRenderError(
            f"AIFx instructions for {phase.value} are {rendered.system_chars:,} characters; "
            f"the per-phase limit is {max_system_chars:,}"
        )
    memory_chars = sum(len(heading) + len(content) for heading, content in memories)
    if memory_chars > max_memory_chars:
        raise AifxRenderError(
            f"AIFx memories for {phase.value} are {memory_chars:,} characters; "
            f"the per-phase limit is {max_memory_chars:,}"
        )
    return rendered
