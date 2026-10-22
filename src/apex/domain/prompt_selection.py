"""Versioned prompt selections and immutable runtime snapshots."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apex.domain.diagnostics import contains_credential_material
from apex.domain.input_limits import NoNulStr, RecordId, ScopeId
from apex.domain.pipeline import Phase


class PromptApplicability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespace: str
    phase: Phase | None = None
    application_id: ScopeId | None = None
    environment_id: RecordId | None = None
    is_shared: bool = False
    is_default: bool = False
    selection_enabled: bool = True

    @model_validator(mode="after")
    def valid_category(self):
        if self.selection_enabled:
            valid = {
                "system": self.phase is None
                and self.application_id is None
                and self.environment_id is None
                and not self.is_shared,
                "phase": self.phase is not None
                and self.application_id is None
                and self.environment_id is None
                and not self.is_shared,
                "application": self.phase is None
                and self.environment_id is None
                and (bool(self.application_id) != self.is_shared),
                "environment": self.phase is None
                and self.application_id is None
                and self.environment_id is not None
                and not self.is_shared,
            }
            if not valid.get(self.namespace, False):
                raise ValueError("Invalid prompt category or applicability")
        if self.is_default and (
            not self.selection_enabled or self.namespace not in {"system", "phase"}
        ):
            raise ValueError("Only selectable system and phase prompts can be defaults")
        return self


class PromptSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt_id: RecordId
    version_id: RecordId
    override_content: NoNulStr | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def valid_override(self):
        if self.override_content is not None:
            if not self.override_content.strip():
                raise ValueError("Prompt override must not be blank")
            if contains_credential_material(self.override_content):
                raise ValueError("Prompt override must not contain credential material")
        return self


class PromptSelectionsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    system: PromptSelection
    phases: dict[Phase, PromptSelection] = Field(max_length=7)
    application: list[PromptSelection] = Field(default_factory=list, max_length=32)
    environment: list[PromptSelection] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def distinct_contexts(self):
        for entries in (self.application, self.environment):
            if len({item.prompt_id for item in entries}) != len(entries):
                raise ValueError("Duplicate context prompt selection")
        return self


class PromptManifestEntry(PromptApplicability):
    prompt_id: RecordId
    version_id: RecordId
    key: NoNulStr = Field(max_length=255)
    version: int = Field(ge=1)
    content: NoNulStr = Field(min_length=1, max_length=100_000)
    overridden: bool = False

    @model_validator(mode="after")
    def valid_effective_entry(self):
        if not self.selection_enabled:
            raise ValueError("Manifest entries must be selectable prompt categories")
        if not self.content.strip():
            raise ValueError("Manifest prompt content must not be blank")
        return self


class PromptManifestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    system: PromptManifestEntry
    phases: dict[Phase, PromptManifestEntry] = Field(max_length=7)
    application: list[PromptManifestEntry] = Field(default_factory=list, max_length=32)
    environment: list[PromptManifestEntry] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_manifest_categories(self):
        if self.system.namespace != "system":
            raise ValueError("Manifest requires a system prompt")
        for phase, entry in self.phases.items():
            if entry.namespace != "phase" or entry.phase != phase:
                raise ValueError("Manifest phase context does not match its phase")
        for category in ("application", "environment"):
            entries = getattr(self, category)
            if any(entry.namespace != category for entry in entries):
                raise ValueError("Manifest context category does not match")
            if len({entry.prompt_id for entry in entries}) != len(entries):
                raise ValueError("Manifest contains duplicate context prompts")
        return self


class PromptContextEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt_id: RecordId
    content: NoNulStr = Field(min_length=1, max_length=100_000)


def launch_config_json_schema() -> dict:
    """Document versioned contracts inside the extensible configuration map."""
    from apex.domain.phase_execution import AnalysisConfiguration, PhaseExecution
    from apex.domain.test_scope import TestScopeV1

    def inline(value, definitions):
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].rsplit("/", 1)[-1]], definitions)
            return {key: inline(item, definitions) for key, item in value.items()}
        if isinstance(value, list):
            return [inline(item, definitions) for item in value]
        return value

    properties = {}
    for name, model in (
        ("prompt_selections", PromptSelectionsV1),
        ("phase_execution", PhaseExecution),
        ("analysis", AnalysisConfiguration),
        ("test_scope", TestScopeV1),
    ):
        schema = model.model_json_schema()
        definitions = schema.pop("$defs", {})
        properties[name] = inline(schema, definitions)

    return {
        "type": "object",
        "additionalProperties": True,
        "properties": properties,
        "description": (
            "Per-run configuration. prompt_manifest, test_scope_snapshot, and rerun_phases "
            "are server-owned "
            "and cannot be submitted."
        ),
    }
