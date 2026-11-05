"""AIFx snapshot ownership at launch and resume.

The pinned ``aifx_snapshot`` is server-owned: clients can neither supply it on a
native run nor store it in an assistant, a native resume restores it from the
durable checkpoint, and pinning failures surface as launch-readiness errors.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from langgraph_sdk import Auth

import apex.auth.handlers as auth_handlers
import apex.routers.pipelines as pipelines
import apex.services.aifx_context as aifx_context
from apex.auth.handlers import on_assistants_write, on_threads_create_run
from apex.auth.identity import Role, ScopeRef
from apex.domain.aifx import AifxSnapshotStatus, AifxSnapshotV1, text_sha256
from apex.domain.integrations import FileContent, RepoRef
from apex.domain.pipeline import PHASE_ORDER, Phase
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.graphs.pipeline.gates import build_prompt_review_payload
from apex.persistence.repositories.documents import DocumentsRepository
from apex.services.aifx_context import AifxIssue, AifxUnavailableError, gate_metadata
from apex.services.pipeline_public import public_gate
from apex.services.pipeline_read import PipelineReadService
from tests.unit import test_aifx_context as context_tests
from tests.unit import test_auth_handlers as auth_tests
from tests.unit import test_pipelines_create_run as create_run_tests
from tests.unit import test_pipelines_resume as resume_tests

# Reuse the native-hook fixtures (catalog, assistant defaults, durable scope rows)
# from the auth handler suite; registering them here makes them apply to this module.
_catalog_application = auth_tests._catalog_application
native_launch_defaults = auth_tests.native_launch_defaults
public_pipeline = auth_tests.public_pipeline
_fake_projection_writer = create_run_tests._fake_projection_writer

make_ctx = auth_tests.make_ctx
make_identity = auth_tests.make_identity

GLOBAL = "aifx/global-rules"
ACB = "aifx/app-a-acb"


def _snapshot(content: str = "Checkout depends on payments.") -> AifxSnapshotV1:
    return AifxSnapshotV1.seal(
        sources=[
            {
                "role": "application-context",
                "application_id": "app-a",
                "application_name": "Storefront",
                "repo": ACB,
                "ref": "main",
                "commit_sha": "2" * 40,
                "manifest_sha256": "b" * 64,
            }
        ],
        entries=[
            {
                "id": "architecture",
                "role": "application-context",
                "repo": ACB,
                "type": "context",
                "title": "Architecture",
                "path": "contexts/architecture.md",
                "content_sha256": text_sha256(content),
                "content": content,
                "application_id": "app-a",
            }
        ],
    )


def _global_snapshot() -> AifxSnapshotV1:
    content = "Cite evidence for every target."
    return AifxSnapshotV1.seal(
        sources=[
            {
                "role": "global-rules",
                "repo": GLOBAL,
                "ref": "main",
                "commit_sha": "1" * 40,
                "manifest_sha256": "a" * 64,
            }
        ],
        entries=[
            {
                "id": "evidence-first",
                "role": "global-rules",
                "repo": GLOBAL,
                "type": "rule",
                "title": "Evidence First",
                "path": "rules/evidence-first.md",
                "content_sha256": text_sha256(content),
                "content": content,
            }
        ],
    )


@pytest.fixture(autouse=True)
def _hermetic_model_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    async def admit(configurable, *, thread_id, actor_id, resume=False):
        return dict(configurable.get("model_by_phase") or {})

    monkeypatch.setattr("apex.services.model_pricing.admit_models", admit)


@pytest.fixture
def aifx_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Record AIFx launch preparation without onboarding or GitHub I/O."""

    calls: dict[str, list[dict[str, Any]]] = {"prepare": [], "size": []}

    async def prepare(configurable: dict[str, Any]) -> None:
        calls["prepare"].append(dict(configurable))

    def size(configurable: dict[str, Any]) -> None:
        calls["size"].append(dict(configurable))

    monkeypatch.setattr(aifx_context, "prepare_aifx_snapshot", prepare)
    monkeypatch.setattr(aifx_context, "check_configurable_size", size)
    return calls


# --- 1. durable resume restores the server-owned snapshot -------------------------


def _durable_config(snapshot: AifxSnapshotV1 | None) -> PipelineConfigurable:
    return PipelineConfigurable(project_id="proj-1", app_id="app-a", aifx_snapshot=snapshot)


def test_resume_without_live_snapshot_restores_the_pinned_one() -> None:
    pinned = _snapshot()
    durable = _durable_config(pinned)

    restored = PipelineConfigurable.from_state(
        {"run_config": durable.snapshot()},
        {"configurable": {"project_id": "proj-1", "app_id": "app-a"}},
    )

    assert restored.aifx_snapshot is not None
    assert restored.aifx_snapshot.snapshot_sha256 == pinned.snapshot_sha256
    assert restored.snapshot() == durable.snapshot()


def test_resume_with_explicit_null_live_snapshot_restores_the_pinned_one() -> None:
    pinned = _snapshot()

    restored = PipelineConfigurable.from_state(
        {"run_config": _durable_config(pinned).snapshot()},
        {"configurable": {"project_id": "proj-1", "app_id": "app-a", "aifx_snapshot": None}},
    )

    assert restored.aifx_snapshot is not None
    assert restored.aifx_snapshot.snapshot_sha256 == pinned.snapshot_sha256


def test_resume_with_the_same_live_snapshot_matches() -> None:
    pinned = _snapshot()

    restored = PipelineConfigurable.from_state(
        {"run_config": _durable_config(pinned).snapshot()},
        {
            "configurable": {
                "project_id": "proj-1",
                "app_id": "app-a",
                "aifx_snapshot": pinned.model_dump(mode="json"),
            }
        },
    )

    assert restored.aifx_snapshot is not None
    assert restored.aifx_snapshot.snapshot_sha256 == pinned.snapshot_sha256


def test_resume_with_a_different_live_snapshot_is_rejected() -> None:
    pinned = _snapshot()
    other = _snapshot("Checkout no longer depends on payments.")
    assert other.snapshot_sha256 != pinned.snapshot_sha256

    with pytest.raises(ValueError, match="does not match its durable checkpoint"):
        PipelineConfigurable.from_state(
            {"run_config": _durable_config(pinned).snapshot()},
            {
                "configurable": {
                    "project_id": "proj-1",
                    "app_id": "app-a",
                    "aifx_snapshot": other.model_dump(mode="json"),
                }
            },
        )


def test_live_snapshot_on_an_unpinned_checkpoint_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not match its durable checkpoint"):
        PipelineConfigurable.from_state(
            {"run_config": _durable_config(None).snapshot()},
            {
                "configurable": {
                    "project_id": "proj-1",
                    "app_id": "app-a",
                    "aifx_snapshot": _snapshot().model_dump(mode="json"),
                }
            },
        )


def test_restoring_the_snapshot_does_not_mask_other_drift() -> None:
    with pytest.raises(ValueError, match="does not match its durable checkpoint"):
        PipelineConfigurable.from_state(
            {"run_config": _durable_config(_snapshot()).snapshot()},
            {"configurable": {"project_id": "proj-1", "app_id": "app-a", "engine": "k6"}},
        )


# --- 2. native run hook --------------------------------------------------------


@pytest.mark.parametrize("surface", ["configurable", "context"])
async def test_untrusted_native_run_cannot_supply_aifx_snapshot(
    public_pipeline: auth_tests.PublicPipelineFixture,
    aifx_calls: dict[str, list[dict[str, Any]]],
    surface: str,
) -> None:
    value: Any = {"assistant_id": "pipeline", "thread_id": "thread-1"}
    public_pipeline.attach(value)
    # A well-formed sealed snapshot, so only server ownership can refuse it.
    forged = {"aifx_snapshot": _global_snapshot().model_dump(mode="json")}
    if surface == "configurable":
        value["config"]["configurable"].update(forged)
    else:
        value["context"] = forged

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"), value=value
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == "aifx_snapshot is server-owned"
    assert aifx_calls["prepare"] == []


async def test_untrusted_native_launch_pins_aifx_and_checks_size(
    public_pipeline: auth_tests.PublicPipelineFixture,
    aifx_calls: dict[str, list[dict[str, Any]]],
) -> None:
    value: Any = {"assistant_id": "pipeline", "thread_id": "thread-1", "context": {}}
    public_pipeline.attach(value)

    await on_threads_create_run(
        ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"), value=value
    )

    assert len(aifx_calls["prepare"]) == 1
    assert len(aifx_calls["size"]) == 1


async def test_untrusted_native_launch_copies_the_pinned_snapshot_into_context(
    public_pipeline: auth_tests.PublicPipelineFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned = {"marker": "pinned-by-server"}

    async def prepare(configurable: dict[str, Any]) -> None:
        configurable["aifx_snapshot"] = pinned

    monkeypatch.setattr(aifx_context, "prepare_aifx_snapshot", prepare)
    monkeypatch.setattr(aifx_context, "check_configurable_size", lambda _cfg: None)
    value: Any = {"assistant_id": "pipeline", "thread_id": "thread-1", "context": {}}
    public_pipeline.attach(value)

    await on_threads_create_run(
        ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"), value=value
    )

    assert value["config"]["configurable"]["aifx_snapshot"] == pinned
    assert value["context"]["aifx_snapshot"] == pinned


@pytest.mark.parametrize("scoped", [False, True])
async def test_untrusted_native_resume_is_refused_before_any_repin(
    public_pipeline: auth_tests.PublicPipelineFixture,
    aifx_calls: dict[str, list[dict[str, Any]]],
    scoped: bool,
) -> None:
    # Untrusted native resume commands never reach AIFx pinning: the run-control
    # check refuses them (resume goes through the facade gate endpoint, which
    # resumes over trusted loopback and restores the durable snapshot).
    identity = (
        make_identity(Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")])
        if scoped
        else make_identity(Role.ADMIN)
    )
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "command": {"resume": {"action": "approve"}},
    }
    public_pipeline.attach(value)

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(ctx=make_ctx(identity, action="create_run"), value=value)

    assert excinfo.value.status_code == 422
    assert "server-owned" in str(excinfo.value.detail)
    assert aifx_calls["prepare"] == []
    assert aifx_calls["size"] == []


async def test_trusted_loopback_resume_keeps_the_durable_snapshot(
    aifx_calls: dict[str, list[dict[str, Any]]],
) -> None:
    value: Any = {
        "assistant_id": "pipeline",
        "thread_id": "thread-1",
        "kwargs": {"command": {"resume": {"action": "approve"}}},
    }

    await on_threads_create_run(
        ctx=make_ctx(make_identity(Role.ADMIN), action="create_run", trusted_loopback=True),
        value=value,
    )

    assert aifx_calls["prepare"] == []
    assert aifx_calls["size"] == []


async def test_trusted_loopback_native_run_does_not_repin(
    aifx_calls: dict[str, list[dict[str, Any]]],
) -> None:
    value: Any = {"assistant_id": "pipeline", "thread_id": "thread-1"}

    await on_threads_create_run(
        ctx=make_ctx(make_identity(Role.ADMIN), action="create_run", trusted_loopback=True),
        value=value,
    )

    assert aifx_calls["prepare"] == []
    assert aifx_calls["size"] == []


async def test_native_launch_maps_unexpected_pin_failure_to_503(
    public_pipeline: auth_tests.PublicPipelineFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(_configurable: dict[str, Any]) -> None:
        raise RuntimeError("postgresql://admin:pin-secret-canary@db.internal/apex")

    monkeypatch.setattr(aifx_context, "prepare_aifx_snapshot", broken)
    value: Any = {"assistant_id": "pipeline", "thread_id": "thread-1"}
    public_pipeline.attach(value)

    with pytest.raises(HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"), value=value
        )

    assert excinfo.value.status_code == 503
    assert "pin-secret-canary" not in str(excinfo.value.detail)
    assert excinfo.value.__cause__ is None


async def test_native_launch_maps_aifx_unavailable_to_422(
    public_pipeline: auth_tests.PublicPipelineFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(_configurable: dict[str, Any]) -> None:
        raise AifxUnavailableError([AifxIssue(repo=ACB, message="manifest missing")])

    monkeypatch.setattr(aifx_context, "prepare_aifx_snapshot", unavailable)
    value: Any = {"assistant_id": "pipeline", "thread_id": "thread-1"}
    public_pipeline.attach(value)

    with pytest.raises(HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"), value=value
        )

    assert excinfo.value.status_code == 422
    assert "manifest missing" in str(excinfo.value.detail)


async def test_native_launch_maps_oversize_configurable_to_422(
    public_pipeline: auth_tests.PublicPipelineFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def prepare(_configurable: dict[str, Any]) -> None:
        return None

    def oversize(_configurable: dict[str, Any]) -> None:
        raise AifxUnavailableError(
            [AifxIssue(repo="launch configuration", message="exceeds the limit")]
        )

    monkeypatch.setattr(aifx_context, "prepare_aifx_snapshot", prepare)
    monkeypatch.setattr(aifx_context, "check_configurable_size", oversize)
    value: Any = {"assistant_id": "pipeline", "thread_id": "thread-1"}
    public_pipeline.attach(value)

    with pytest.raises(HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"), value=value
        )

    assert excinfo.value.status_code == 422
    assert "exceeds the limit" in str(excinfo.value.detail)


def test_real_size_check_rejects_an_oversized_configurable() -> None:
    with pytest.raises(AifxUnavailableError, match="character"):
        aifx_context.check_configurable_size(
            {"blob": "x" * (aifx_context.MAX_CONFIGURABLE_CHARS + 1)}
        )
    aifx_context.check_configurable_size({"blob": "x"})


# --- 3. assistants cannot carry a snapshot ---------------------------------------


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("surface", ["configurable", "context"])
async def test_assistant_write_rejects_aifx_snapshot(operation: str, surface: str) -> None:
    forged = {"aifx_snapshot": {"schema_version": 1}}
    value: dict[str, Any] = (
        {"config": {"configurable": forged}} if surface == "configurable" else {"context": forged}
    )

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_assistants_write(
            ctx=make_ctx(make_identity(Role.ADMIN), resource="assistants", action=operation),
            value=value,
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == "aifx_snapshot cannot be stored in an assistant"


def test_aifx_snapshot_is_an_assistant_server_owned_key() -> None:
    assert "aifx_snapshot" in auth_handlers._ASSISTANT_SERVER_OWNED_CONFIG


async def test_native_launch_rejects_assistant_defaults_carrying_a_snapshot(
    public_pipeline: auth_tests.PublicPipelineFixture,
    native_launch_defaults: dict[str, Any],
    aifx_calls: dict[str, list[dict[str, Any]]],
) -> None:
    native_launch_defaults["assistant"] = {"aifx_snapshot": {"schema_version": 1}}
    value: Any = {"assistant_id": "pipeline", "thread_id": "thread-1"}
    public_pipeline.attach(value)

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        await on_threads_create_run(
            ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"), value=value
        )

    assert excinfo.value.status_code == 422
    assert "server-owned" in str(excinfo.value.detail)
    assert aifx_calls["prepare"] == []


async def test_native_launch_drops_null_assistant_and_thread_snapshot_defaults(
    public_pipeline: auth_tests.PublicPipelineFixture,
    native_launch_defaults: dict[str, Any],
    aifx_calls: dict[str, list[dict[str, Any]]],
) -> None:
    native_launch_defaults["assistant"] = {"aifx_snapshot": None}
    native_launch_defaults["thread"] = {"aifx_snapshot": None}
    value: Any = {"assistant_id": "pipeline", "thread_id": "thread-1"}
    public_pipeline.attach(value)

    await on_threads_create_run(
        ctx=make_ctx(make_identity(Role.ADMIN), action="create_run"), value=value
    )

    assert len(aifx_calls["prepare"]) == 1


# --- 4. facade launch maps pin failures to launch readiness ----------------------


def _facade() -> tuple[Any, Any]:
    client = create_run_tests.FakeClient()
    client.threads = create_run_tests.AmbiguousThreads()
    service = PipelineReadService(client)
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )
    return client, request


async def _launch(request: Any) -> Any:
    return await pipelines.create_pipeline_run(
        create_run_tests._scope_request(title="AIFx run"),
        create_run_tests._identity(["proj-1"]),
        cast(DocumentsRepository, create_run_tests.FakeDocsRepo({})),
        cast(Any, create_run_tests.FakeCatalogRepo()),
        cast(Any, request),
    )


@pytest.mark.parametrize(
    "error",
    [RuntimeError("pin-secret-canary"), ConnectionError("db pin-secret-canary unreachable")],
)
async def test_facade_maps_unexpected_pin_failure_to_readiness_and_cleans_thread(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    async def broken(_configurable: dict[str, Any]) -> None:
        raise error

    monkeypatch.setattr(aifx_context, "prepare_aifx_snapshot", broken)
    client, request = _facade()

    with pytest.raises(HTTPException) as excinfo:
        await _launch(request)

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == (
        "AIFx context could not be pinned; retry later or check the AIFx admin page"
    )
    assert "pin-secret-canary" not in str(excinfo.value.detail)
    assert client.threads.deleted == ["thr-1"]
    assert client.runs.args == ()


async def test_facade_keeps_the_aifx_unavailable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(_configurable: dict[str, Any]) -> None:
        raise AifxUnavailableError([AifxIssue(repo=ACB, message="manifest missing")])

    monkeypatch.setattr(aifx_context, "prepare_aifx_snapshot", unavailable)
    client, request = _facade()

    with pytest.raises(HTTPException) as excinfo:
        await _launch(request)

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == f"AIFx context could not be pinned: {ACB} — manifest missing"
    assert client.threads.deleted == ["thr-1"]
    assert client.runs.args == ()


async def test_facade_maps_oversize_configurable_to_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def prepare(_configurable: dict[str, Any]) -> None:
        return None

    def oversize(_configurable: dict[str, Any]) -> None:
        raise AifxUnavailableError(
            [AifxIssue(repo="launch configuration", message="exceeds the limit")]
        )

    monkeypatch.setattr(aifx_context, "prepare_aifx_snapshot", prepare)
    monkeypatch.setattr(aifx_context, "check_configurable_size", oversize)
    client, request = _facade()

    with pytest.raises(HTTPException) as excinfo:
        await _launch(request)

    assert excinfo.value.status_code == 422
    assert "exceeds the limit" in str(excinfo.value.detail)
    assert client.threads.deleted == ["thr-1"]


async def test_facade_launch_submits_the_pinned_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned = _snapshot().model_dump(mode="json")

    async def prepare(configurable: dict[str, Any]) -> None:
        configurable["aifx_snapshot"] = pinned

    monkeypatch.setattr(aifx_context, "prepare_aifx_snapshot", prepare)
    client, request = _facade()

    response = await _launch(request)

    assert response.thread_id == "thr-1"
    assert client.runs.args[3]["configurable"]["aifx_snapshot"] == pinned
    assert client.threads.deleted == []


# --- 5. reruns carry the original pin; they never repin -------------------------


@pytest.fixture
def no_repin(
    monkeypatch: pytest.MonkeyPatch, aifx_calls: dict[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    """Fail loudly if a rerun reaches onboarding or any source-control adapter."""

    async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a rerun must not re-read AIFx sources")

    def forbidden_resolver() -> Any:
        raise AssertionError("a rerun must not resolve a source-control connection")

    async def admit(configurable, *, thread_id, actor_id, resume=False):
        return dict(configurable.get("model_by_phase") or {})

    monkeypatch.setattr(aifx_context, "load_onboarding", forbidden)
    monkeypatch.setattr(aifx_context, "open_adapters", forbidden)
    monkeypatch.setattr(aifx_context, "read_sources", forbidden)
    monkeypatch.setattr("apex.services.connections.get_connection_resolver", forbidden_resolver)
    monkeypatch.setattr("apex.services.pipeline_read.admit_models", admit)
    return aifx_calls


def _degraded_snapshot() -> AifxSnapshotV1:
    body = _snapshot().model_dump(mode="json")
    return AifxSnapshotV1.seal(
        status="degraded",
        sources=body["sources"],
        entries=body["entries"],
        issues=["aifx/app-a-team-documents — the repository could not be read"],
    )


def _pinned_rerun_config(pinned: AifxSnapshotV1, **overrides: Any) -> dict[str, Any]:
    config = resume_tests.trusted_rerun_snapshot()
    config.update(overrides)
    config["aifx_snapshot"] = pinned.model_dump(mode="json")
    return PipelineConfigurable.model_validate(config).snapshot()


async def _rerun(run_config: dict[str, Any], **arguments: Any) -> dict[str, Any]:
    threads = resume_tests.FakeThreads(
        {"t-1": resume_tests.rerun_state(run_config)},
        {"t-1": {"project_id": "project-a", "app_id": "app-a"}},
    )
    runs = resume_tests.DurableRerunRuns()
    service = PipelineReadService(resume_tests.FakeClient(threads, runs))
    await service.rerun_pipeline(
        "t-1",
        **{
            "phases": ["execution"],
            "gates_mode": "inherit",
            "idempotency_key": "aifx-rerun",
            "principal_id": "consumer-a",
            **arguments,
        },
    )
    assert len(runs.create_calls) == 1
    return dict(runs.create_calls[0]["config"]["configurable"])


@pytest.mark.parametrize("make_snapshot", [_snapshot, _degraded_snapshot])
@pytest.mark.parametrize("gates_mode", ["inherit", "gated", "auto"])
async def test_rerun_carries_the_pinned_snapshot_unchanged_without_repinning(
    no_repin: dict[str, list[dict[str, Any]]],
    make_snapshot: Any,
    gates_mode: str,
) -> None:
    pinned = make_snapshot()
    durable = _pinned_rerun_config(pinned)

    submitted = await _rerun(durable, phases=["execution"], gates_mode=gates_mode)

    assert submitted["aifx_snapshot"] == durable["aifx_snapshot"]
    assert submitted["aifx_snapshot"] == pinned.model_dump(mode="json")
    restored = AifxSnapshotV1.model_validate(submitted["aifx_snapshot"])
    assert restored.snapshot_sha256 == pinned.snapshot_sha256
    assert restored.status is pinned.status
    assert restored.issues == pinned.issues
    assert no_repin["prepare"] == []
    assert no_repin["size"] == []


@pytest.mark.parametrize("owner", ["langgraph", "hitl"])
async def test_owned_phase_rerun_carries_the_pinned_snapshot_without_repinning(
    no_repin: dict[str, list[dict[str, Any]]], owner: str
) -> None:
    pinned = _snapshot()
    durable = _pinned_rerun_config(
        pinned,
        engine="sim",
        phases=[p.value for p in PHASE_ORDER],
        phase_execution={"schema_version": 1, "owners": {p.value: owner for p in PHASE_ORDER}},
    )

    submitted = await _rerun(durable, phases=["reporting"], gates_mode="auto")

    assert submitted["aifx_snapshot"] == durable["aifx_snapshot"]
    submitted.pop("apex_actor_id")
    cfg = PipelineConfigurable.model_validate(submitted)
    assert cfg.aifx_snapshot is not None
    assert cfg.aifx_snapshot.snapshot_sha256 == pinned.snapshot_sha256
    # The rerun replays against its own checkpoint without snapshot drift.
    replay = PipelineConfigurable.from_state_for_phase(
        {"run_config": submitted, "phases_plan": ["reporting"]},
        {"configurable": submitted},
        Phase.REPORTING,
    )
    assert replay.aifx_snapshot == cfg.aifx_snapshot
    assert no_repin["prepare"] == []


async def test_rerun_of_an_unpinned_run_stays_unpinned(
    no_repin: dict[str, list[dict[str, Any]]],
) -> None:
    durable = resume_tests.trusted_rerun_snapshot()
    assert durable.get("aifx_snapshot") is None

    submitted = await _rerun(durable)

    assert submitted.get("aifx_snapshot") is None
    assert no_repin["prepare"] == []


async def test_rerun_keeps_the_pin_even_when_aifx_is_now_disabled(
    no_repin: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    from apex.settings import get_settings

    monkeypatch.setenv("APEX_AIFX_ENABLED", "false")
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    pinned = _snapshot()

    submitted = await _rerun(_pinned_rerun_config(pinned))

    assert submitted["aifx_snapshot"] == pinned.model_dump(mode="json")
    assert no_repin["prepare"] == []


# --- 6. a dev launch's degraded pin reaches the prompt-review gate ---------------


class _MissingTeamDocuments(context_tests.StubSourceControlAdapter):
    async def get_file(self, repo: RepoRef, path: str, ref: str = "HEAD") -> FileContent:
        if repo.name == "missing-team-documents":
            raise KeyError(f"aifx/{repo.name}:{path}@{ref} is unavailable to the GitHub App")
        return await super().get_file(repo, path, ref)


@pytest.fixture
def degraded_dev_onboarding(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """A dev (not locked-down) deployment with one unreadable AIFx repository."""

    from apex.services import connections
    from apex.settings import ApexSettings, get_settings

    monkeypatch.setenv("APEX_AIFX_ENABLED", "true")
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    monkeypatch.setattr(ApexSettings, "is_locked_down", property(lambda self: False))

    async def load(application_ids: Any) -> Any:
        rows = [
            context_tests.mapping("app-a", "apm1", team_documents_repo="missing-team-documents")
        ]
        return rows, {"org-1": context_tests.org()}, {"app-a": "Storefront"}

    class Resolver(context_tests.StubResolver):
        async def build_from_metadata(self, metadata: Any) -> Any:
            return _MissingTeamDocuments()

    monkeypatch.setattr(aifx_context, "load_onboarding", load)
    monkeypatch.setattr(connections, "get_connection_resolver", lambda: Resolver())


def _prompt_gate(cfg: PipelineConfigurable, phase: Phase) -> dict[str, Any]:
    # The prompt-review node builds exactly this payload (phase_subgraph:
    # ``aifx=gate_metadata(cfg.aifx_snapshot, phase)``) before interrupting.
    payload = build_prompt_review_payload(
        phase,
        {"system": "s", "user": "u", "application": None},
        {"origin": "catalog", "ref": None},
        [],
        [],
        aifx=gate_metadata(cfg.aifx_snapshot, phase),
    )
    return {
        "interrupt_id": "prompt-1",
        "kind": "prompt_review",
        "phase": phase.value,
        "payload": payload,
    }


async def test_degraded_dev_launch_reaches_the_prompt_gate_and_public_projection(
    degraded_dev_onboarding: None,
) -> None:
    client, request = _facade()

    response = await _launch(request)

    assert response.thread_id == "thr-1"
    submitted = client.runs.args[3]["configurable"]
    snapshot = AifxSnapshotV1.model_validate(submitted["aifx_snapshot"])
    assert snapshot.status is AifxSnapshotStatus.DEGRADED
    assert any("missing-team-documents" in issue for issue in snapshot.issues)
    # The readable sources are still pinned.
    assert {source.repo for source in snapshot.sources} == {
        "aifx/global-rules",
        "aifx/apm1-acb",
    }

    cfg = PipelineConfigurable.model_validate(
        {k: v for k, v in submitted.items() if k in PipelineConfigurable.model_fields}
    )
    assert cfg.aifx_snapshot == snapshot
    gate = _prompt_gate(cfg, Phase.TEST_PLANNING)
    meta = gate["payload"]["aifx"]
    assert meta["status"] == "degraded"
    assert meta["issues"] == list(snapshot.issues)
    assert meta["issue_count"] == len(snapshot.issues) >= 1
    assert meta["snapshot_sha256"] == snapshot.snapshot_sha256

    projected = public_gate(gate, include_payload=True)
    assert projected is not None
    public_aifx = projected["payload"]["aifx"]
    assert public_aifx["status"] == "degraded"
    assert public_aifx["issues"] == meta["issues"]
    assert public_aifx["issue_count"] == meta["issue_count"]
    assert public_aifx["snapshot_sha256"] == snapshot.snapshot_sha256
    assert [s["repo"] for s in public_aifx["sources"]] == [s["repo"] for s in meta["sources"]]
    # Titles only: the pinned file bodies never reach the public gate.
    for entry in snapshot.entries:
        assert entry.content not in str(projected)


async def test_degraded_dev_launch_with_no_readable_source_still_reaches_the_gate(
    degraded_dev_onboarding: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apex.services import connections

    class Unopenable(context_tests.StubResolver):
        async def build_from_metadata(self, metadata: Any) -> Any:
            raise RuntimeError("token=resolver-secret-canary")

    monkeypatch.setattr(connections, "get_connection_resolver", lambda: Unopenable())
    client, request = _facade()

    await _launch(request)

    submitted = client.runs.args[3]["configurable"]
    snapshot = AifxSnapshotV1.model_validate(submitted["aifx_snapshot"])
    assert snapshot.status is AifxSnapshotStatus.DEGRADED
    assert snapshot.sources == [] and snapshot.issues
    assert "resolver-secret-canary" not in str(submitted["aifx_snapshot"])
    cfg = PipelineConfigurable.model_validate(
        {k: v for k, v in submitted.items() if k in PipelineConfigurable.model_fields}
    )
    projected = public_gate(_prompt_gate(cfg, Phase.TEST_PLANNING), include_payload=True)
    assert projected is not None
    assert projected["payload"]["aifx"]["status"] == "degraded"
    assert projected["payload"]["aifx"]["issues"] == list(snapshot.issues)


# --- 7. launch order and real budget enforcement --------------------------------


async def test_facade_pins_aifx_before_the_prompt_manifest_and_size_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The manifest's model-input admission must count the AIFx text, so the
    # snapshot has to exist before the manifest is prepared.
    from apex.services import prompt_selections

    order: list[str] = []

    async def prepare(configurable: dict[str, Any]) -> None:
        order.append("aifx")

    async def manifest(configurable: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        order.append("manifest")

    def size(configurable: dict[str, Any]) -> None:
        order.append("size")

    monkeypatch.setattr(aifx_context, "prepare_aifx_snapshot", prepare)
    monkeypatch.setattr(prompt_selections, "prepare_prompt_manifest", manifest)
    monkeypatch.setattr(aifx_context, "check_configurable_size", size)
    _client, request = _facade()

    await _launch(request)

    assert order == ["aifx", "manifest", "size"]


async def test_facade_refuses_a_launch_whose_phase_aifx_exceeds_its_budget(
    degraded_dev_onboarding: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real prepare_aifx_snapshot: check_phase_budgets must refuse the launch,
    # not a later prompt gate, even in a dev deployment.
    from apex.settings import get_settings

    monkeypatch.setenv("APEX_AIFX__MAX_PHASE_SYSTEM_CHARS", "1000")
    get_settings.cache_clear()
    client, request = _facade()

    with pytest.raises(HTTPException) as excinfo:
        await _launch(request)

    assert excinfo.value.status_code == 422
    assert "AIFx context could not be pinned" in str(excinfo.value.detail)
    assert client.threads.deleted == ["thr-1"]
    assert client.runs.args == ()
