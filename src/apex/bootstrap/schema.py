"""Declarative bootstrap document schema (validated, secret-free).

The document is authored as JSON (the Helm ConfigMap form) or YAML and validated
into these models before anything touches the database. Connections reference
secrets by name only — a raw secret value here is a validation error.
"""

from __future__ import annotations

import re
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apex.adapters.registry import PortKind
from apex.auth.identity import ConsumerType, Role
from apex.domain.input_limits import (
    MAX_CHILD_ITEMS,
    MAX_DESCRIPTION_CHARS,
    NoNulStr,
    ScopeId,
)
from apex.services.connection_credentials import reject_raw_secret_options, validate_secret_ref


class HostSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: NoNulStr = Field(min_length=1, max_length=1024)
    role: NoNulStr | None = Field(default=None, max_length=255)


class ApplicationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: ScopeId
    name: NoNulStr = Field(min_length=1, max_length=255)
    description: NoNulStr | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)


class EnvironmentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: ScopeId
    application: NoNulStr = Field(min_length=1, max_length=255)  # name within the project
    name: NoNulStr = Field(min_length=1, max_length=255)
    kind: NoNulStr | None = Field(default=None, max_length=64)
    base_url: NoNulStr | None = Field(default=None, max_length=1024)
    options: dict[str, Any] = Field(default_factory=dict)
    hosts: list[HostSpec] = Field(default_factory=list, max_length=MAX_CHILD_ITEMS)

    @field_validator("options")
    @classmethod
    def _options_are_bounded_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        return reject_raw_secret_options(
            value,
            label="environment options",
            reference="an external connection secret_ref",
        )

    @field_validator("hosts")
    @classmethod
    def _hosts_are_unique(cls, hosts: list[HostSpec]) -> list[HostSpec]:
        keys = [(host.hostname, host.role) for host in hosts]
        if len(keys) != len(set(keys)):
            raise ValueError("hosts must not contain duplicate hostname/role entries")
        return hosts


class ConnectionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NoNulStr = Field(min_length=1, max_length=255)
    kind: PortKind
    provider: NoNulStr = Field(min_length=1, max_length=64)
    project_id: ScopeId | None = None  # null = global
    base_url: NoNulStr | None = Field(default=None, max_length=1024)
    options: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None
    enabled: bool = True

    @field_validator("options")
    @classmethod
    def _options_are_secret_free(cls, value: dict[str, Any]) -> dict[str, Any]:
        return reject_raw_secret_options(value)

    @field_validator("secret_ref")
    @classmethod
    def _ref_not_literal(cls, value: str | None) -> str | None:
        return validate_secret_ref(value)


class ScopeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: ScopeId
    app_id: ScopeId | None = None


class AdminConsumerSpec(BaseModel):
    """Initial API consumer. The plaintext key is read from ``key_env`` at apply
    time (sourced from a Secret/Key Vault), hashed, and never persisted/logged."""

    model_config = ConfigDict(extra="forbid")

    name: NoNulStr = Field(default="apex-admin", min_length=1, max_length=255)
    consumer_type: ConsumerType = ConsumerType.INTERNAL
    role: Role = Role.ADMIN
    key_env: NoNulStr = Field(default="APEX_BOOTSTRAP_ADMIN_KEY", min_length=1, max_length=255)
    scopes: list[ScopeSpec] = Field(default_factory=list, max_length=MAX_CHILD_ITEMS)

    @field_validator("scopes")
    @classmethod
    def _scopes_are_unambiguous(cls, scopes: list[ScopeSpec]) -> list[ScopeSpec]:
        keys = [(scope.project_id, scope.app_id) for scope in scopes]
        if len(keys) != len(set(keys)):
            raise ValueError("scopes must not contain duplicate project/app entries")
        project_wide = {scope.project_id for scope in scopes if scope.app_id is None}
        if any(scope.app_id is not None and scope.project_id in project_wide for scope in scopes):
            raise ValueError("app scopes are redundant when a project-wide scope exists")
        return scopes

    @field_validator("key_env")
    @classmethod
    def _key_env_is_an_environment_variable(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,254}", value) is None:
            raise ValueError("key_env must be a valid environment-variable name")
        return value


class BootstrapDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed_default_prompts: bool = False
    applications: list[ApplicationSpec] = Field(default_factory=list, max_length=MAX_CHILD_ITEMS)
    environments: list[EnvironmentSpec] = Field(default_factory=list, max_length=MAX_CHILD_ITEMS)
    connections: list[ConnectionSpec] = Field(default_factory=list, max_length=MAX_CHILD_ITEMS)
    admin: AdminConsumerSpec | None = None

    @model_validator(mode="after")
    def _natural_keys_are_unique(self) -> Self:
        collections = (
            (
                "applications",
                [(spec.project_id, spec.name) for spec in self.applications],
            ),
            (
                "environments",
                [(spec.project_id, spec.application, spec.name) for spec in self.environments],
            ),
            ("connections", [spec.name for spec in self.connections]),
        )
        for label, keys in collections:
            if len(keys) != len(set(keys)):
                raise ValueError(f"{label} must not contain duplicate natural keys")
        return self
