"""Every stub/sim adapter must satisfy its runtime_checkable port Protocol."""

import pytest

from apex.adapters.sim_engine import SimExecutionEngine
from apex.adapters.stubs import (
    EnvSecretsAdapter,
    MemoryArtifactStore,
    StubClusterInventoryAdapter,
    StubDocumentsAdapter,
    StubLogSearchAdapter,
    StubObservabilityAdapter,
    StubSourceControlAdapter,
    StubWorkTrackingAdapter,
)
from apex.domain.integrations import LoadTestSpec, SecretValue
from apex.ports import (
    ArtifactStorePort,
    ClusterInventoryPort,
    DocumentRetrievalPort,
    ExecutionEnginePort,
    LogSearchPort,
    ObservabilityPort,
    SecretsPort,
    SourceControlPort,
    WorkTrackingPort,
)

ADAPTER_PORT_PAIRS = [
    (StubWorkTrackingAdapter, WorkTrackingPort),
    (StubLogSearchAdapter, LogSearchPort),
    (StubObservabilityAdapter, ObservabilityPort),
    (StubDocumentsAdapter, DocumentRetrievalPort),
    (StubClusterInventoryAdapter, ClusterInventoryPort),
    (StubSourceControlAdapter, SourceControlPort),
    (EnvSecretsAdapter, SecretsPort),
    (MemoryArtifactStore, ArtifactStorePort),
    (SimExecutionEngine, ExecutionEnginePort),
]


@pytest.mark.parametrize(
    ("adapter_cls", "port"), ADAPTER_PORT_PAIRS, ids=[a.__name__ for a, _ in ADAPTER_PORT_PAIRS]
)
def test_adapter_satisfies_port(adapter_cls: type, port: type) -> None:
    assert isinstance(adapter_cls(), port)


def test_unrelated_object_fails_port_check() -> None:
    assert not isinstance(object(), WorkTrackingPort)


def test_secret_value_repr_and_str_are_redacted() -> None:
    secret = SecretValue(value="hunter2")
    assert "hunter2" not in repr(secret)
    assert "hunter2" not in str(secret)
    assert "hunter2" not in f"{secret}"
    assert secret.value == "hunter2"  # raw value still accessible by design


def test_load_test_spec_defaults() -> None:
    spec = LoadTestSpec(title="demo")
    assert spec.vusers == 10
    assert spec.ramp_s == 5
    assert spec.duration_s == 2
    assert spec.slas == {}
    assert spec.idempotency_key  # auto-generated

    other = LoadTestSpec(title="demo")
    assert other.idempotency_key != spec.idempotency_key
