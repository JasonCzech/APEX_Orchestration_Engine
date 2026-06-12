"""Kubernetes cluster-inventory adapter against respx-mocked Kube API fixtures.

Fixture JSON mirrors the real apps/v1 / core/v1 / networking.k8s.io/v1 wire
formats (DeploymentList/ServiceList/Status bodies as the API server emits them).
"""

from typing import Any

import httpx
import pytest
import respx

from apex.adapters.k8s.cluster_inventory import KubernetesClusterInventoryAdapter
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
