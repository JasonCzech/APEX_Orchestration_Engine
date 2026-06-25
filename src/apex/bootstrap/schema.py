"""Declarative bootstrap document schema (validated, secret-free).

The document is authored as JSON (the Helm ConfigMap form) or YAML and validated
into these models before anything touches the database. Connections reference
secrets by name only — a raw secret value here is a validation error.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from apex.adapters.registry import PortKind
from apex.auth.identity import ConsumerType, Role

# A secret reference is "<scheme>:<name>" (e.g. env:APEX_MINIO_SECRET_KEY), never a
# literal secret. Schemes are resolved by the secrets adapters at runtime.
_SECRET_REF_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:.+$")


class HostSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str
    role: str | None = None


class ApplicationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    name: str
    description: str | None = None


class EnvironmentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    application: str  # application name within the project
    name: str
    kind: str | None = None
    base_url: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    hosts: list[HostSpec] = Field(default_factory=list)


class ConnectionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: PortKind
    provider: str
    project_id: str | None = None  # null = global
    base_url: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None
    enabled: bool = True

    @field_validator("secret_ref")
    @classmethod
    def _ref_not_literal(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _SECRET_REF_RE.match(value):
            raise ValueError(
                f"secret_ref {value!r} must be a reference like 'env:NAME' or 'vault:path' "
                "(names only — never a raw secret value)"
            )
        return value


class ScopeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    app_id: str | None = None


class AdminConsumerSpec(BaseModel):
    """Initial API consumer. The plaintext key is read from ``key_env`` at apply
    time (sourced from a Secret/Key Vault), hashed, and never persisted/logged."""

    model_config = ConfigDict(extra="forbid")

    name: str = "apex-admin"
    consumer_type: ConsumerType = ConsumerType.INTERNAL
    role: Role = Role.ADMIN
    key_env: str = "APEX_BOOTSTRAP_ADMIN_KEY"
    scopes: list[ScopeSpec] = Field(default_factory=list)


class BootstrapDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed_default_prompts: bool = False
    applications: list[ApplicationSpec] = Field(default_factory=list)
    environments: list[EnvironmentSpec] = Field(default_factory=list)
    connections: list[ConnectionSpec] = Field(default_factory=list)
    admin: AdminConsumerSpec | None = None
