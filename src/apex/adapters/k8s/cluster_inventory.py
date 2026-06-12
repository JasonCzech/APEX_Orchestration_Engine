"""Kubernetes cluster-inventory adapter (provider "kubernetes", PortKind.CLUSTER_INVENTORY).

Talks to the plain Kubernetes REST API with httpx — no `kubernetes` client
dependency. Connection options: {"base_url": "https://kube-api.internal:6443",
"namespace": "staging" | "namespaces": ["staging", "staging-jobs"],
"verify_tls": true}; the secret is a bearer ServiceAccount token resolved from
the connection's secret_ref by AdapterRegistry.build.

Namespace precedence (documented contract): options["namespaces"] (list) >
options["namespace"] (string) > env_ref.name (the catalog environment's name
doubles as the namespace). No namespace anywhere is a ValueError.

A scan lists deployments, services, and ingresses per namespace. Only
deployments are representable today: the domain ServiceInfo model carries
(name, replicas, image) and EnvironmentSnapshot has no fields for endpoints or
routing, so the service/ingress responses validate reachability + RBAC and
exercise the recorded wire contract without extending the domain models. A 404
on the ingress endpoint is tolerated (the networking.k8s.io group may be
absent from older or trimmed clusters).
"""

import asyncio
from typing import Any

import httpx

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import EnvironmentSnapshot, EnvRef, SecretValue, ServiceInfo

DEFAULT_TIMEOUT_S = 15.0


@AdapterRegistry.register(PortKind.CLUSTER_INVENTORY, "kubernetes")
class KubernetesClusterInventoryAdapter:
    """ClusterInventoryPort against a Kubernetes API server.

    The httpx client is lazy and per-instance — constructing the adapter never
    touches the network. Instances are cached process-wide by the
    ConnectionResolver and graph nodes run in short-lived event loops on worker
    threads, so the client is rebuilt whenever the running loop changes (pooled
    connections are loop-bound and must not cross loops).
    """

    def __init__(
        self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
    ) -> None:
        options: dict[str, Any] = dict(conn.options) if conn is not None else {}
        conn_id = conn.id if conn is not None else "<none>"
        base_url = str(options.get("base_url", "")).strip()
        if not base_url:
            raise ValueError(
                f"kubernetes connection {conn_id!r} requires options['base_url'] "
                "(e.g. 'https://kube-api.internal:6443')"
            )
        if secret is None:
            raise ValueError(
                f"kubernetes connection {conn_id!r} requires a bearer ServiceAccount token; "
                'set secret_ref on the connection (e.g. "env:APEX_K8S_TOKEN")'
            )
        self._base_url = base_url.rstrip("/")
        self._token = secret.value
        self._verify_tls = bool(options.get("verify_tls", True))
        self._options = options
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    # ── port surface ──────────────────────────────────────────────────────────

    async def scan_environment(self, env_ref: EnvRef) -> EnvironmentSnapshot:
        services: list[ServiceInfo] = []
        for namespace in self._namespaces_for(env_ref):
            deployments = (
                await self._get_json(
                    f"/apis/apps/v1/namespaces/{namespace}/deployments", namespace=namespace
                )
                or {}
            )
            # Fetched to validate connectivity/RBAC and pin the wire contract;
            # the domain model has no fields for endpoints/routing yet.
            await self._get_json(f"/api/v1/namespaces/{namespace}/services", namespace=namespace)
            await self._get_json(
                f"/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses",
                namespace=namespace,
                tolerate_404=True,
            )
            services.extend(_service_info(item) for item in deployments.get("items", []))
        return EnvironmentSnapshot(services=services)

    # ── namespace + client plumbing ───────────────────────────────────────────

    def _namespaces_for(self, env_ref: EnvRef) -> list[str]:
        configured = self._options.get("namespaces")
        if isinstance(configured, str):
            configured = [configured]
        if isinstance(configured, (list, tuple)) and configured:
            return [str(ns) for ns in configured]
        single = self._options.get("namespace")
        if single:
            return [str(single)]
        if env_ref.name:
            return [env_ref.name]
        raise ValueError(
            f"no namespace configured for environment {env_ref.id!r}: set "
            "options['namespace'] or options['namespaces'] on the connection, or give "
            "the catalog environment a name to use as the namespace"
        )

    def _client_for_loop(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._token}"},
                verify=self._verify_tls,
                timeout=DEFAULT_TIMEOUT_S,
            )
            self._client_loop = loop
        return self._client

    # ── HTTP + error mapping ──────────────────────────────────────────────────

    async def _get_json(
        self, path: str, *, namespace: str, tolerate_404: bool = False
    ) -> dict[str, Any] | None:
        client = self._client_for_loop()
        try:
            response = await client.get(path)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"kubernetes API request failed for GET {path} on {self._base_url}: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"kubernetes API denied GET {path} ({response.status_code}): "
                f"{_status_message(response)}; check the ServiceAccount token "
                "(connection secret_ref) and its RBAC bindings"
            )
        if response.status_code == 404:
            if tolerate_404:
                return None
            raise ValueError(
                f"namespace {namespace!r} not found on {self._base_url}: "
                f"{_status_message(response)}"
            )
        if response.is_error:
            raise RuntimeError(
                f"kubernetes API returned {response.status_code} for GET {path}: "
                f"{_status_message(response)}"
            )
        return response.json()


def _service_info(deployment: dict[str, Any]) -> ServiceInfo:
    """Deployment item -> ServiceInfo. k8s omits readyReplicas when zero are ready."""
    metadata = deployment.get("metadata") or {}
    status = deployment.get("status") or {}
    template_spec = ((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}
    containers = template_spec.get("containers") or []
    image = str(containers[0].get("image", "")) if containers else ""
    return ServiceInfo(
        name=str(metadata.get("name", "")),
        replicas=int(status.get("readyReplicas") or 0),
        image=image,
    )


def _status_message(response: httpx.Response) -> str:
    """Best-effort message from a Kubernetes Status body; falls back to raw text."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    text = response.text.strip()
    return text[:300] if text else response.reason_phrase
