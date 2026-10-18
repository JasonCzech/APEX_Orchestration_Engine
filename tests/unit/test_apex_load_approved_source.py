"""Approved Cycles source must survive the native provider boundary unchanged."""

import copy
import hashlib
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from apex.adapters.apex_load.engine import (
    ApexLoadExecutionEngine,
    approved_workload_digest,
    qualify_approved_http_source,
)
from apex.adapters.registry import ConnectionConfig, PortKind
from apex.domain.integrations import LoadTestSpec, SecretValue
from apex.ports.execution_engine import ApprovedScriptExecutionPort

BASE = "https://approved-load.example"
SOURCE = {
    "name": "Reviewed source",
    "version": "1",
    "protocol": "http",
    "config": {"base_url": "https://target.example", "http": {"max_redirects": -1}},
    "metadata": {"runtime_policy": {"allowed_target_hosts": ["target.example"]}},
    "actions": [{"name": "Reviewed request", "type": "http", "method": "GET", "target": "/owned"}],
}


def adapter(**options):
    return ApexLoadExecutionEngine(
        ConnectionConfig(
            id="connection",
            kind=PortKind.EXECUTION_ENGINE,
            provider="apex_load",
            name="Load",
            options={
                "base_url": BASE,
                "project_id": "project",
                "native_control_enabled": True,
                **options,
            },
        ),
        SecretValue(value="test-service-credential"),
    )


def spec(**changes):
    return LoadTestSpec.model_validate(
        {
            "title": "Approved plan",
            "idempotency_key": "approved-attempt-1",
            "vusers": 1,
            "ramp_s": 0,
            "duration_s": 5,
            "slas": {"p95_ms": 1000, "error_rate": 0.1},
            "target_environment": "https://target.example",
            **changes,
        }
    )


class Provider:
    def __init__(self):
        self.script: dict[str, Any] = {}
        self.test: dict[str, Any] = {}
        self.posts = 0
        self.uploads = 0
        self.lose_script_response = False
        self.start_count = 0
        self.export_change: Callable[[dict[str, Any]], None] | None = None
        self.test_change: Callable[[dict[str, Any]], None] | None = None
        self.allowed = True
        self.lose_response = False

    def mount(self):
        respx.post(BASE + "/api/v1/scripts/validate").respond(
            200, json={"valid": True, "issues": []}
        )
        respx.post(BASE + "/api/v1/scripts").mock(side_effect=self.upload)
        respx.get(BASE + "/api/v1/scripts").mock(
            side_effect=lambda _: httpx.Response(
                200,
                json={
                    "scripts": [self.script] if self.script else [],
                    "count": int(bool(self.script)),
                },
            )
        )
        respx.get(BASE + "/api/v1/scripts/script-1").mock(side_effect=self.export)
        respx.get(BASE + "/api/v1/tests").mock(
            side_effect=lambda _: httpx.Response(
                200,
                json={
                    "tests": [self.test] if self.test else [],
                    "count": int(bool(self.test)),
                },
            )
        )
        respx.post(BASE + "/api/v1/tests/estimate").respond(200, json={"recommended_generators": 1})
        respx.post(BASE + "/api/v1/tests/preflight").mock(
            side_effect=lambda _: httpx.Response(
                200,
                json={"start_now_allowed": self.allowed, "queue_allowed": False, "warnings": []},
            )
        )
        respx.post(BASE + "/api/v1/tests").mock(side_effect=self.create)
        respx.get(BASE + "/api/v1/tests/test-1").mock(side_effect=self.read)
        respx.post(BASE + "/api/v1/tests/test-1/start").mock(side_effect=self.start)

    def upload(self, request):
        document = json.loads(request.content)
        self.uploads += 1
        self.script = {
            **document["script"],
            "id": f"script-{self.uploads}",
            "project_id": "project",
        }
        if self.lose_script_response:
            raise httpx.ReadTimeout("script response lost", request=request)
        return httpx.Response(201, json=self.script)

    def export(self, _):
        value = copy.deepcopy(self.script)
        if self.export_change:
            self.export_change(value)
        return httpx.Response(200, json={**value, "script": value})

    def create(self, request):
        self.posts += 1
        self.test = {
            **json.loads(request.content),
            "id": "test-1",
            "project_id": "project",
            "status": "QUEUED",
            "scenario_mode": "vuser_driven",
        }
        if self.lose_response:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(201, json=self.test)

    def read(self, _):
        value = copy.deepcopy(self.test)
        if self.test_change:
            self.test_change(value)
        return httpx.Response(200, json=value)

    def start(self, _):
        self.start_count += 1
        return httpx.Response(200, json={"status": "started", "test_id": "test-1"})


@respx.mock
async def test_validate_preserves_exact_source_without_materializing():
    provider = Provider()
    provider.mount()
    engine = adapter()
    try:
        assert isinstance(engine, ApprovedScriptExecutionPort)
        result = await engine.validate_approved_source(spec(), json.dumps(SOURCE))
        assert result.ok
        assert len(respx.calls) == 1
        assert json.loads(respx.calls.last.request.content)["script"] == SOURCE
        assert "capacity" in result.warnings[0].lower()
    finally:
        await engine.aclose()


@pytest.mark.parametrize(
    "change",
    [
        {"script_refs": ["unreviewed-script"]},
        {"slas": {"unsupported": 1}},
        {"release_id": "unreviewed-release"},
        {"target_environment": "https://different.example"},
    ],
)
async def test_invalid_plan_never_contacts_provider(change):
    engine = adapter()
    try:
        assert not (await engine.validate_approved_source(spec(**change), json.dumps(SOURCE))).ok
        with pytest.raises(ValueError):
            await engine.provision_approved_source(spec(**change), json.dumps(SOURCE))
    finally:
        await engine.aclose()


@pytest.mark.parametrize(
    "config",
    [
        {"run_logic": {"iterations": 1}},
        {"run_logic": {"global_iterations": 1}},
        {"run_logic": {"max_transactions": 1}},
        {"run_logic": {"termination_mode": "iterations"}},
        {"run_logic": {"workload_model": "open"}},
        {"jmeter": {"threads": 1}},
        {"sampler_schedules": [{"name": "unreviewed"}]},
    ],
)
async def test_source_runtime_conflicts_are_rejected_before_io(config):
    engine = adapter()
    source = {**SOURCE, "config": {**SOURCE["config"], **config}}
    try:
        result = await engine.validate_approved_source(spec(), json.dumps(source))
        assert not result.ok and result.issues
    finally:
        await engine.aclose()


@respx.mock
@pytest.mark.parametrize("lose_response", [False, True])
@pytest.mark.parametrize("lose_script_response", [False, True])
async def test_provision_verifies_source_and_recovers_exact_test_on_retry(
    lose_response, lose_script_response
):
    provider = Provider()
    provider.lose_response = lose_response
    provider.lose_script_response = lose_script_response
    provider.mount()
    engine = adapter()
    try:
        first = await engine.provision_approved_source(spec(), json.dumps(SOURCE))
        assert (
            first.extras["approved_artifact_sha256"]
            == hashlib.sha256(json.dumps(SOURCE).encode()).hexdigest()
        )
        assert first.extras["approved_workload_digest"] == approved_workload_digest(spec())
        assert provider.script["actions"] == SOURCE["actions"]
        assert provider.script["config"] == SOURCE["config"]
        assert provider.test["duration"] == "5s"
        assert provider.test["groups"] == [
            {"name": "group-1", "script_id": "script-1", "vusers": 1, "ramp_up": "0s"}
        ]
        await engine.aclose()
        engine = adapter()
        second = await engine.provision_approved_source(spec(), json.dumps(SOURCE))
        assert first == second and provider.posts == 1 and provider.uploads == 1
        await engine.start(second)
        assert provider.start_count == 1
    finally:
        await engine.aclose()


@respx.mock
@pytest.mark.parametrize(
    "alter",
    [
        lambda source: source["actions"][0].update(target="/changed"),
        lambda source: source["config"].update(http={"dns_cache": False}),
        lambda source: source.update(actions=[]),
    ],
)
async def test_changed_export_blocks_test_creation(alter):
    provider = Provider()
    provider.export_change = alter
    provider.mount()
    engine = adapter()
    try:
        with pytest.raises(RuntimeError, match="differs from the approved"):
            await engine.provision_approved_source(spec(), json.dumps(SOURCE))
        assert provider.posts == 0 and provider.start_count == 0
    finally:
        await engine.aclose()


@respx.mock
async def test_empty_native_struct_padding_is_allowed_but_start_rechecks_bound_source():
    provider = Provider()
    provider.export_change = lambda source: source["config"].update(
        browser={"http_credentials": {}}, think_time={"mean_ms": 0}
    )
    provider.mount()
    engine = adapter()
    try:
        handle = await engine.provision_approved_source(spec(), json.dumps(SOURCE))
        provider.export_change = lambda source: source["actions"][0].update(method="DELETE")
        with pytest.raises(RuntimeError, match="changed before execution"):
            await engine.start(handle)
        assert provider.start_count == 0
    finally:
        await engine.aclose()


@respx.mock
@pytest.mark.parametrize(
    "change",
    [
        lambda test: test["groups"][0].update(vusers=100),
        lambda test: test["groups"][0].update(script_id="different-script"),
        lambda test: test.update(duration="500s"),
        lambda test: test.update(scenario_mode="goal_oriented"),
        lambda test: test.update(start_delay="10s"),
        lambda test: test.update(script_shakeout=True),
        lambda test: test.update(resource_baseline=True),
        lambda test: test.update(runtime_settings={"iterations": 100}),
        lambda test: test.update(goal_config={"p95_latency_ms": 1}),
    ],
)
async def test_start_rechecks_exact_provider_workload(change):
    provider = Provider()
    provider.mount()
    engine = adapter()
    try:
        handle = await engine.provision_approved_source(spec(), json.dumps(SOURCE))
        provider.test_change = change
        with pytest.raises(RuntimeError):
            await engine.start(handle)
        assert provider.start_count == 0
    finally:
        await engine.aclose()


@respx.mock
async def test_native_capacity_failure_never_creates_or_starts_test():
    provider = Provider()
    provider.allowed = False
    provider.mount()
    engine = adapter()
    try:
        with pytest.raises(ValueError, match="does not allow"):
            await engine.provision_approved_source(spec(), json.dumps(SOURCE))
        assert provider.posts == 0 and provider.start_count == 0
    finally:
        await engine.aclose()


@pytest.mark.parametrize(
    "alter",
    [
        lambda source: source.update(protocol="browser"),
        lambda source: source.pop("metadata"),
        lambda source: source["metadata"]["runtime_policy"].update(allowed_target_hosts=["*"]),
        lambda source: source["actions"][0].update(target="https://outside.example/"),
        lambda source: source["actions"][0].update(target="https://target.example:8443/"),
        lambda source: source["actions"][0].update(target="https://target.example:0/"),
        lambda source: source["actions"][0].update(target="//outside.example/"),
        lambda source: source["actions"][0].update(target="{{var.target}}"),
        lambda source: source["actions"][0].update(headers={"Host": "outside.example"}),
        lambda source: source["actions"][0].update(
            header_list=[{"name": "Host", "value": "outside.example"}]
        ),
        lambda source: source["actions"][0].update(
            params={"resources": ["https://outside.example/"]}
        ),
        lambda source: source["config"]["http"].update(proxy_url="https://outside.example/"),
        lambda source: source["config"]["http"].pop("max_redirects"),
        lambda source: source["actions"][0].update(max_redirects=1),
        lambda source: source.update(data_sources=[{"type": "csv", "path": "/tmp/data"}]),
    ],
)
@respx.mock
async def test_target_qualification_rejects_unsupported_source_before_io(alter):
    source = copy.deepcopy(SOURCE)
    alter(source)
    with pytest.raises(ValueError):
        qualify_approved_http_source(spec(), json.dumps(source))
    engine = adapter()
    try:
        assert not (await engine.validate_approved_source(spec(), json.dumps(source))).ok
        assert not respx.calls
    finally:
        await engine.aclose()


@respx.mock
@pytest.mark.parametrize("project", ["unassigned", "project"])
async def test_project_default_filing_is_verified_and_bound_before_start(project):
    provider = Provider()
    provider.mount()
    suffix = (
        "" if project == "unassigned" else "-" + hashlib.sha1(project.encode()).hexdigest()[:12]
    )
    application_id, release_id = "legacy" + suffix, "legacy-imported" + suffix
    original_create = provider.create

    def create(request):
        response = original_create(request)
        provider.test.update(
            project_id=project, application_id=application_id, release_id=release_id
        )
        return httpx.Response(response.status_code, json=provider.test)

    respx.post(BASE + "/api/v1/tests").mock(side_effect=create)
    original_upload = provider.upload

    def upload(request):
        original_upload(request)
        provider.script.update(project_id=project)
        return httpx.Response(201, json=provider.script)

    respx.post(BASE + "/api/v1/scripts").mock(side_effect=upload)
    respx.get(BASE + f"/api/v1/applications/{application_id}").respond(
        200, json={"id": application_id, "project_id": project}
    )
    release_route = respx.get(BASE + f"/api/v1/releases/{release_id}").respond(
        200, json={"id": release_id, "application_id": application_id, "project_id": project}
    )
    engine = adapter(project_id=project)
    try:
        handle = await engine.provision_approved_source(spec(), json.dumps(SOURCE))
        assert handle.extras["approved_release_id"] == release_id
        await engine.start(handle)
        assert provider.start_count == 1
        release_route.respond(
            200, json={"id": release_id, "application_id": application_id, "project_id": "other"}
        )
        with pytest.raises(ValueError, match="belong to the bound project"):
            await engine.start(handle)
        assert provider.start_count == 1
    finally:
        await engine.aclose()


@respx.mock
async def test_nondefault_release_is_never_accepted_as_implicit_filing():
    provider = Provider()
    provider.mount()
    original_create = provider.create

    def create(request):
        original_create(request)
        provider.test.update(application_id="legacy", release_id="reviewed-but-unbound-release")
        return httpx.Response(201, json=provider.test)

    respx.post(BASE + "/api/v1/tests").mock(side_effect=create)
    engine = adapter()
    try:
        with pytest.raises(ValueError, match="unreviewed application or release"):
            await engine.provision_approved_source(spec(), json.dumps(SOURCE))
        assert provider.start_count == 0
    finally:
        await engine.aclose()


def test_environment_target_port_zero_is_not_treated_as_default():
    source = copy.deepcopy(SOURCE)
    source["config"]["base_url"] = "https://target.example:0"
    with pytest.raises(ValueError, match="literal HTTP"):
        qualify_approved_http_source(
            spec(target_environment="https://target.example:0"), json.dumps(source)
        )
