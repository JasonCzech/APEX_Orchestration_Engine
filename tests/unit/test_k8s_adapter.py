"""Kubernetes cluster-inventory adapter against respx-mocked Kube API fixtures.

Fixture JSON mirrors the real apps/v1 / core/v1 / networking.k8s.io/v1 wire
formats (DeploymentList/ServiceList/Status bodies as the API server emits them).
"""

from typing import Any

import httpx
import pytest
import respx

from apex.adapters.k8s import cluster_inventory as k8s_mod
from apex.adapters.k8s.cluster_inventory import (
    KubernetesClusterInventoryAdapter,
    _in_cluster_base_url,
    _resolve_verify,
)
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import EnvRef, SecretValue

BASE_URL = "https://kube-api.test:6443"
TOKEN = "sa-token-abc123"

DEPLOYMENTS_PATH = f"{BASE_URL}/apis/apps/v1/namespaces/{{ns}}/deployments"
SERVICES_PATH = f"{BASE_URL}/api/v1/namespaces/{{ns}}/services"
INGRESSES_PATH = f"{BASE_URL}/apis/networking.k8s.io/v1/namespaces/{{ns}}/ingresses"

# ── recorded-style fixtures ──────────────────────────────────────────────────

DEPLOYMENT_LIST: dict[str, Any] = {
    "kind": "DeploymentList",
    "apiVersion": "apps/v1",
    "metadata": {"resourceVersion": "812245"},
    "items": [
        {
            "metadata": {
                "name": "checkout-api",
                "namespace": "staging",
                "uid": "0e7c1f3a-8e2b-4f7d-9c64-1f0b6f2a9d11",
                "labels": {"app": "checkout-api"},
            },
            "spec": {
                "replicas": 3,
                "selector": {"matchLabels": {"app": "checkout-api"}},
                "template": {
                    "metadata": {"labels": {"app": "checkout-api"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "checkout-api",
                                "image": "registry.internal/checkout-api:2.7.1",
                                "ports": [{"containerPort": 8080, "protocol": "TCP"}],
                            },
                            {"name": "envoy-sidecar", "image": "envoyproxy/envoy:v1.30.1"},
                        ]
                    },
                },
            },
            "status": {
                "observedGeneration": 14,
                "replicas": 3,
                "updatedReplicas": 3,
                "readyReplicas": 3,
                "availableReplicas": 3,
            },
        },
        {
            "metadata": {
                "name": "cart-svc",
                "namespace": "staging",
                "uid": "5a2b9d40-77f1-4f0c-8a3e-d2c4e6b81f22",
                "labels": {"app": "cart-svc"},
            },
            "spec": {
                "replicas": 2,
                "selector": {"matchLabels": {"app": "cart-svc"}},
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "cart-svc", "image": "registry.internal/cart-svc:1.9.3"}
                        ]
                    }
                },
            },
            # Scaled to zero ready: the API server omits readyReplicas entirely.
            "status": {"observedGeneration": 9, "replicas": 2, "updatedReplicas": 2},
        },
    ],
}

SERVICE_LIST: dict[str, Any] = {
    "kind": "ServiceList",
    "apiVersion": "v1",
    "metadata": {"resourceVersion": "812246"},
    "items": [
        {
            "metadata": {"name": "checkout-api", "namespace": "staging"},
            "spec": {
                "type": "ClusterIP",
                "clusterIP": "10.96.12.34",
                "selector": {"app": "checkout-api"},
                "ports": [{"port": 80, "targetPort": 8080, "protocol": "TCP"}],
            },
        }
    ],
}

INGRESS_LIST: dict[str, Any] = {
    "kind": "IngressList",
    "apiVersion": "networking.k8s.io/v1",
    "metadata": {"resourceVersion": "812247"},
    "items": [
        {
            "metadata": {"name": "checkout", "namespace": "staging"},
            "spec": {"rules": [{"host": "checkout.staging.internal"}]},
        }
    ],
}

NAMESPACE_404_STATUS: dict[str, Any] = {
    "kind": "Status",
    "apiVersion": "v1",
    "metadata": {},
    "status": "Failure",
    "message": 'namespaces "ghost" not found',
    "reason": "NotFound",
    "details": {"name": "ghost", "kind": "namespaces"},
    "code": 404,
}

FORBIDDEN_403_STATUS: dict[str, Any] = {
    "kind": "Status",
    "apiVersion": "v1",
    "metadata": {},
    "status": "Failure",
    "message": (
        'deployments.apps is forbidden: User "system:serviceaccount:apex:scanner" cannot '
        'list resource "deployments" in API group "apps" in the namespace "staging"'
    ),
    "reason": "Forbidden",
    "code": 403,
}

UNAUTHORIZED_401_STATUS: dict[str, Any] = {
    "kind": "Status",
    "apiVersion": "v1",
    "metadata": {},
    "status": "Failure",
    "message": "Unauthorized",
    "reason": "Unauthorized",
    "code": 401,
}

EMPTY_DEPLOYMENTS: dict[str, Any] = {"kind": "DeploymentList", "apiVersion": "apps/v1", "items": []}
EMPTY_SERVICES: dict[str, Any] = {"kind": "ServiceList", "apiVersion": "v1", "items": []}


# ── helpers ──────────────────────────────────────────────────────────────────


def make_adapter(**options: Any) -> KubernetesClusterInventoryAdapter:
    conn = ConnectionConfig(
        id="conn-k8s-staging",
        kind=PortKind.CLUSTER_INVENTORY,
        provider="kubernetes",
        name="Staging cluster",
        options={"base_url": BASE_URL, **options},
        secret_ref="env:APEX_K8S_TOKEN",
    )
    return KubernetesClusterInventoryAdapter(conn, SecretValue(value=TOKEN))


def mock_namespace(
    ns: str,
    *,
    deployments: dict[str, Any] | None = None,
    services: dict[str, Any] | None = None,
    ingress_response: httpx.Response | None = None,
) -> respx.Route:
    """Mount happy-path routes for one namespace; returns the deployments route."""
    deployments_route = respx.get(DEPLOYMENTS_PATH.format(ns=ns)).mock(
        return_value=httpx.Response(200, json=deployments or DEPLOYMENT_LIST)
    )
    respx.get(SERVICES_PATH.format(ns=ns)).mock(
        return_value=httpx.Response(200, json=services or SERVICE_LIST)
    )
    respx.get(INGRESSES_PATH.format(ns=ns)).mock(
        return_value=ingress_response or httpx.Response(200, json=INGRESS_LIST)
    )
    return deployments_route


# ── scans ────────────────────────────────────────────────────────────────────


@respx.mock
async def test_scan_maps_deployments_and_sends_bearer_token() -> None:
    deployments_route = mock_namespace(
        "staging",
        # API group networking.k8s.io absent: plain-text 404, must be tolerated.
        ingress_response=httpx.Response(404, text="404 page not found"),
    )
    adapter = make_adapter(namespace="staging")

    snapshot = await adapter.scan_environment(EnvRef(id="env-1", name="Staging 2"))

    assert [(s.name, s.replicas, s.image) for s in snapshot.services] == [
        ("checkout-api", 3, "registry.internal/checkout-api:2.7.1"),
        ("cart-svc", 0, "registry.internal/cart-svc:1.9.3"),  # missing readyReplicas -> 0
    ]
    assert snapshot.scanned_at  # ISO timestamp populated by the domain default
    request = deployments_route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


@respx.mock
async def test_scan_aggregates_multiple_namespaces() -> None:
    mock_namespace("staging")
    mock_namespace(
        "staging-jobs",
        deployments={
            "kind": "DeploymentList",
            "apiVersion": "apps/v1",
            "items": [
                {
                    "metadata": {"name": "report-worker", "namespace": "staging-jobs"},
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "report-worker",
                                        "image": "registry.internal/report-worker:0.4.0",
                                    }
                                ]
                            }
                        }
                    },
                    "status": {"replicas": 1, "readyReplicas": 1},
                }
            ],
        },
        services=EMPTY_SERVICES,
    )
    adapter = make_adapter(namespaces=["staging", "staging-jobs"])

    snapshot = await adapter.scan_environment(EnvRef(id="env-1", name="staging"))

    assert [s.name for s in snapshot.services] == ["checkout-api", "cart-svc", "report-worker"]


@respx.mock
async def test_namespace_falls_back_to_env_ref_name() -> None:
    mock_namespace("perf-lab", deployments=EMPTY_DEPLOYMENTS, services=EMPTY_SERVICES)
    adapter = make_adapter()  # no namespace/namespaces in connection options

    snapshot = await adapter.scan_environment(EnvRef(id="env-9", name="perf-lab"))

    assert snapshot.services == []


async def test_no_namespace_anywhere_is_value_error() -> None:
    adapter = make_adapter()
    with pytest.raises(ValueError, match="no namespace configured"):
        await adapter.scan_environment(EnvRef(id="env-9", name=None))


# ── error mapping ────────────────────────────────────────────────────────────


@respx.mock
async def test_rbac_403_raises_runtime_error_with_actionable_message() -> None:
    respx.get(DEPLOYMENTS_PATH.format(ns="staging")).mock(
        return_value=httpx.Response(403, json=FORBIDDEN_403_STATUS)
    )
    adapter = make_adapter(namespace="staging")

    with pytest.raises(RuntimeError, match="ServiceAccount token") as excinfo:
        await adapter.scan_environment(EnvRef(id="env-1", name="staging"))
    assert "RBAC" in str(excinfo.value)
    assert "deployments.apps is forbidden" in str(excinfo.value)


@respx.mock
async def test_401_raises_runtime_error() -> None:
    respx.get(DEPLOYMENTS_PATH.format(ns="staging")).mock(
        return_value=httpx.Response(401, json=UNAUTHORIZED_401_STATUS)
    )
    adapter = make_adapter(namespace="staging")

    with pytest.raises(RuntimeError, match="check the ServiceAccount token"):
        await adapter.scan_environment(EnvRef(id="env-1", name="staging"))


@respx.mock
async def test_unknown_namespace_404_raises_value_error() -> None:
    respx.get(DEPLOYMENTS_PATH.format(ns="ghost")).mock(
        return_value=httpx.Response(404, json=NAMESPACE_404_STATUS)
    )
    adapter = make_adapter(namespace="ghost")

    with pytest.raises(ValueError, match="namespace 'ghost' not found"):
        await adapter.scan_environment(EnvRef(id="env-1", name="ghost"))


@respx.mock
async def test_server_error_raises_runtime_error_with_status() -> None:
    respx.get(DEPLOYMENTS_PATH.format(ns="staging")).mock(
        return_value=httpx.Response(500, text="etcdserver: request timed out")
    )
    adapter = make_adapter(namespace="staging")

    with pytest.raises(RuntimeError, match="returned 500"):
        await adapter.scan_environment(EnvRef(id="env-1", name="staging"))


@respx.mock
async def test_connect_error_raises_runtime_error() -> None:
    respx.get(DEPLOYMENTS_PATH.format(ns="staging")).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    adapter = make_adapter(namespace="staging")

    with pytest.raises(RuntimeError, match="request failed"):
        await adapter.scan_environment(EnvRef(id="env-1", name="staging"))


# ── construction + registration ──────────────────────────────────────────────


def test_missing_secret_is_value_error() -> None:
    conn = ConnectionConfig(
        id="conn-k8s",
        kind=PortKind.CLUSTER_INVENTORY,
        provider="kubernetes",
        name="k8s",
        options={"base_url": BASE_URL},
    )
    with pytest.raises(ValueError, match="bearer ServiceAccount token"):
        KubernetesClusterInventoryAdapter(conn, None)


def test_missing_base_url_is_value_error() -> None:
    conn = ConnectionConfig(
        id="conn-k8s", kind=PortKind.CLUSTER_INVENTORY, provider="kubernetes", name="k8s"
    )
    with pytest.raises(ValueError, match="base_url"):
        KubernetesClusterInventoryAdapter(conn, SecretValue(value=TOKEN))


def test_provider_registered_via_adapters_package_import() -> None:
    import apex.adapters  # noqa: F401  (side effect: registers real providers)

    assert "kubernetes" in AdapterRegistry.providers_for(PortKind.CLUSTER_INVENTORY)


# ── in_cluster auth mode ──────────────────────────────────────────────────────


def _mount_sa(
    monkeypatch: Any,
    tmp_path: Any,
    *,
    token: str = "pod-sa-token",
    write_ca: bool = True,
    namespace: str | None = None,
) -> Any:
    """Point the SA mount constants at fixtures and return the token file path."""
    token_file = tmp_path / "token"
    token_file.write_text(token)
    monkeypatch.setattr(k8s_mod, "IN_CLUSTER_TOKEN_PATH", str(token_file))

    ca_file = tmp_path / "ca.crt"
    if write_ca:
        ca_file.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(k8s_mod, "IN_CLUSTER_CA_PATH", str(ca_file))

    ns_file = tmp_path / "namespace"
    if namespace is not None:
        ns_file.write_text(namespace)
    monkeypatch.setattr(k8s_mod, "IN_CLUSTER_NAMESPACE_PATH", str(ns_file))
    return token_file


def make_in_cluster_adapter(**options: Any) -> KubernetesClusterInventoryAdapter:
    conn = ConnectionConfig(
        id="conn-k8s-incluster",
        kind=PortKind.CLUSTER_INVENTORY,
        provider="kubernetes",
        name="In-cluster",
        options={"auth_mode": "in_cluster", **options},
    )
    return KubernetesClusterInventoryAdapter(conn, None)  # no secret_ref in in_cluster mode


@respx.mock
async def test_in_cluster_scan_uses_pod_token_and_env_api_server(
    monkeypatch: Any, tmp_path: Any
) -> None:
    _mount_sa(monkeypatch, tmp_path, token="rotating-pod-token")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "kube-api.test")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "6443")
    base = "https://kube-api.test:6443"
    deployments_route = respx.get(f"{base}/apis/apps/v1/namespaces/staging/deployments").mock(
        return_value=httpx.Response(200, json=DEPLOYMENT_LIST)
    )
    respx.get(f"{base}/api/v1/namespaces/staging/services").mock(
        return_value=httpx.Response(200, json=SERVICE_LIST)
    )
    respx.get(f"{base}/apis/networking.k8s.io/v1/namespaces/staging/ingresses").mock(
        return_value=httpx.Response(200, json=INGRESS_LIST)
    )
    # base_url omitted -> derived from KUBERNETES_SERVICE_HOST/PORT. verify off to skip
    # building a real SSL context from the fixture CA.
    adapter = make_in_cluster_adapter(namespace="staging", verify_tls=False)

    snapshot = await adapter.scan_environment(EnvRef(id="env-1", name=None))

    assert [s.name for s in snapshot.services] == ["checkout-api", "cart-svc"]
    request = deployments_route.calls.last.request
    assert request.headers["authorization"] == "Bearer rotating-pod-token"


@respx.mock
async def test_in_cluster_namespace_falls_back_to_pod_namespace_file(
    monkeypatch: Any, tmp_path: Any
) -> None:
    _mount_sa(monkeypatch, tmp_path, namespace="team-a")
    mock_namespace("team-a", deployments=EMPTY_DEPLOYMENTS, services=EMPTY_SERVICES)
    # Explicit base_url, no options namespace, env_ref.name None -> pod namespace file.
    adapter = make_in_cluster_adapter(base_url=BASE_URL, verify_tls=False)

    snapshot = await adapter.scan_environment(EnvRef(id="env-9", name=None))

    assert snapshot.services == []


def test_in_cluster_construction_without_secret_succeeds(monkeypatch: Any, tmp_path: Any) -> None:
    _mount_sa(monkeypatch, tmp_path)
    # Must NOT raise the bearer-token ValueError that bearer mode raises.
    make_in_cluster_adapter(base_url=BASE_URL)


def test_in_cluster_without_api_server_is_value_error(monkeypatch: Any, tmp_path: Any) -> None:
    _mount_sa(monkeypatch, tmp_path)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with pytest.raises(ValueError, match="in_cluster auth requires"):
        make_in_cluster_adapter()  # no base_url, no env


def test_unknown_auth_mode_is_value_error() -> None:
    conn = ConnectionConfig(
        id="conn-k8s",
        kind=PortKind.CLUSTER_INVENTORY,
        provider="kubernetes",
        name="k8s",
        options={"base_url": BASE_URL, "auth_mode": "sidecar"},
    )
    with pytest.raises(ValueError, match="unknown auth_mode"):
        KubernetesClusterInventoryAdapter(conn, SecretValue(value=TOKEN))


def test_in_cluster_base_url_from_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "6443")
    assert _in_cluster_base_url() == "https://10.0.0.1:6443"
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert _in_cluster_base_url() == ""


def test_resolve_verify_matrix(monkeypatch: Any, tmp_path: Any) -> None:
    # bearer: pass the option through unchanged.
    assert _resolve_verify(False, None) is True
    assert _resolve_verify(False, "false") is False
    # in_cluster + explicit false disables TLS regardless of the CA bundle.
    assert _resolve_verify(True, "false") is False
    # in_cluster + CA bundle present -> verify against it (path string).
    ca_file = tmp_path / "ca.crt"
    ca_file.write_text("ca")
    monkeypatch.setattr(k8s_mod, "IN_CLUSTER_CA_PATH", str(ca_file))
    assert _resolve_verify(True, None) == str(ca_file)
    # in_cluster + CA absent -> fall back to True.
    monkeypatch.setattr(k8s_mod, "IN_CLUSTER_CA_PATH", str(tmp_path / "missing.crt"))
    assert _resolve_verify(True, None) is True
