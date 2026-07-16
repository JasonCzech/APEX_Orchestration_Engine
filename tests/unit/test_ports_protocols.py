"""Every stub/sim adapter must satisfy its runtime_checkable port Protocol."""

from collections.abc import Iterator, Mapping

import pytest
from pydantic import ValidationError

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
from apex.ports.artifact_store import transcript_artifact_key, validate_stored_artifact_ack
from apex.services.work_tracking import parse_work_query

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
    assert secret.model_dump() == {"value": "***"}
    assert "hunter2" not in secret.model_dump_json()


def test_secret_value_validation_errors_do_not_render_raw_secret() -> None:
    raw_secret = "sentinel-provider-secret\x00tail"

    with pytest.raises(ValidationError) as raised:
        SecretValue(value=raw_secret)

    rendered = str(raised.value)
    assert "input_value=" not in rendered
    assert "sentinel-provider-secret" not in rendered


def test_load_test_spec_defaults() -> None:
    spec = LoadTestSpec(title="demo")
    assert spec.vusers == 10
    assert spec.ramp_s == 5
    assert spec.duration_s == 2
    assert spec.slas == {}
    assert spec.idempotency_key  # auto-generated

    other = LoadTestSpec(title="demo")
    assert other.idempotency_key != spec.idempotency_key


def test_artifact_ack_rejects_hostile_mapping_without_invoking_hooks() -> None:
    calls: list[str] = []

    class HostileMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            calls.append(f"getitem:{key}")
            raise AssertionError("hostile provider hook ran")

        def __iter__(self) -> Iterator[str]:
            calls.append("iter")
            raise AssertionError("hostile provider hook ran")

        def __len__(self) -> int:
            calls.append("len")
            raise AssertionError("hostile provider hook ran")

    with pytest.raises(RuntimeError, match="invalid object metadata") as raised:
        validate_stored_artifact_ack(HostileMapping(), "expected")

    assert raised.value.__cause__ is None
    assert calls == []


def test_transcript_key_rejects_noncanonical_or_unbounded_components_before_hooks() -> None:
    calls: list[str] = []

    class HostileText(str):
        def strip(self, *_args: object, **_kwargs: object) -> str:
            calls.append("strip")
            raise AssertionError("hostile text hook ran")

    for thread_id, phase in (
        (HostileText("thread-a"), "execution"),
        ("thread-a", HostileText("execution")),
        ("t" * 256, "execution"),
        (" thread-a", "execution"),
        ("thread-a", "../execution"),
    ):
        with pytest.raises(ValueError, match="transcript artifact key"):
            transcript_artifact_key(thread_id, phase, 1)

    assert calls == []
    assert (
        transcript_artifact_key("thread-a", "execution", 2)
        == "transcripts/thread-a/execution/attempt-2.txt"
    )


def test_work_query_rejects_hostile_and_unbounded_text_before_normalization() -> None:
    calls: list[str] = []

    class HostileText(str):
        def strip(self, *_args: object, **_kwargs: object) -> str:
            calls.append("strip")
            raise AssertionError("hostile text hook ran")

    for value in (HostileText("open bugs"), "x" * 20_001, "   "):
        with pytest.raises(ValueError, match="natural-language query"):
            parse_work_query(value)

    assert calls == []
