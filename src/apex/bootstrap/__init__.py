"""Idempotent bootstrap of a fresh APEX deployment from a declarative document.

Consolidates the dev seed scripts (prompts, applications/environments,
connections) plus initial-admin provisioning behind one CLI that the Helm
``bootstrap`` hook Job runs in-image (``python -m apex.bootstrap``). The seed
document carries NO secret values — connections reference secrets by
``secret_ref`` name (e.g. ``env:APEX_MINIO_SECRET_KEY``); the initial admin
key is read from an environment variable (sourced from a K8s Secret / Key
Vault), hashed with sha256, and never logged in plaintext.
"""

from apex.bootstrap.runner import BootstrapReport, apply_document
from apex.bootstrap.schema import (
    AdminConsumerSpec,
    ApplicationSpec,
    BootstrapDocument,
    ConnectionSpec,
    EnvironmentSpec,
    HostSpec,
)

__all__ = [
    "AdminConsumerSpec",
    "ApplicationSpec",
    "BootstrapDocument",
    "BootstrapReport",
    "ConnectionSpec",
    "EnvironmentSpec",
    "HostSpec",
    "apply_document",
]
