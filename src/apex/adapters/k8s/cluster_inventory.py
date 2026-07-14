"""Kubernetes cluster-inventory adapter (provider "kubernetes", PortKind.CLUSTER_INVENTORY).

Talks to the plain Kubernetes REST API with httpx — no `kubernetes` client
dependency. Connection options: {"base_url": "https://kube-api.internal:6443",
"namespace": "staging" | "namespaces": ["staging", "staging-jobs"] |
"environment_namespaces": {"<immutable-environment-id>": "staging"},
"verify_tls": true, "auth_mode": "bearer" | "in_cluster"}.

Auth modes:
- ``bearer`` (default): the secret is a bearer ServiceAccount token resolved from
  the connection's secret_ref by AdapterRegistry.build, and ``base_url`` is
  required. Use this for clusters reached from outside (the common catalog case).
- ``in_cluster``: the adapter runs inside the cluster and authenticates with the
  pod's projected ServiceAccount token (``/var/run/secrets/kubernetes.io/...``),
  the in-cluster CA bundle, and the API server from ``KUBERNETES_SERVICE_HOST``/
  ``KUBERNETES_SERVICE_PORT``. No secret_ref is needed; the token is re-read on
  each client rebuild so projected-token rotation is honored. RBAC comes from the
  pod's ServiceAccount (see the Helm chart's workloadIdentity/rbac values).

Namespace precedence (documented contract): options["environment_namespaces"]
looked up by immutable environment id > options["namespaces"] >
options["namespace"] > the pod's own namespace file (in_cluster only). The
editable catalog display name is intentionally never an authorization input.

A scan lists deployments, services, and ingresses per namespace. Only
deployments are representable today: the domain ServiceInfo model carries
(name, replicas, image) and EnvironmentSnapshot has no fields for endpoints or
routing, so the service/ingress responses validate reachability + RBAC and
exercise the recorded wire contract without extending the domain models. A 404
on the ingress endpoint is tolerated (the networking.k8s.io group may be
absent from older or trimmed clusters).
"""

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx

from apex.adapters.http_resilience import resilient_request
from apex.adapters.network_safety import private_hosts_allowed, safe_async_http_client
from apex.adapters.options import coerce_bool
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import EnvironmentSnapshot, EnvRef, SecretValue, ServiceInfo

DEFAULT_TIMEOUT_S = 15.0

# Projected ServiceAccount mount (in_cluster mode). Module-level so tests can
# point them at fixtures via monkeypatch.
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
IN_CLUSTER_TOKEN_PATH = f"{SA_DIR}/token"
IN_CLUSTER_CA_PATH = f"{SA_DIR}/ca.crt"
IN_CLUSTER_NAMESPACE_PATH = f"{SA_DIR}/namespace"

_IN_CLUSTER_ALIASES = frozenset({"in_cluster", "in-cluster", "incluster"})


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

        raw_mode = str(options.get("auth_mode", "bearer")).strip().lower()
        in_cluster = raw_mode in _IN_CLUSTER_ALIASES
        if not in_cluster and raw_mode not in ("bearer", ""):
            raise ValueError(
                f"kubernetes connection {conn_id!r} has unknown auth_mode={raw_mode!r}; "
                "expected 'bearer' or 'in_cluster'"
            )
        self._in_cluster = in_cluster

        base_url = str(options.get("base_url", "")).strip()
        if in_cluster and not base_url:
            base_url = _in_cluster_base_url()
        if not base_url:
            hint = (
                "in_cluster auth requires running inside a pod "
                "(KUBERNETES_SERVICE_HOST unset) or an explicit options['base_url']"
                if in_cluster
                else "(e.g. 'https://kube-api.internal:6443')"
            )
            raise ValueError(
                f"kubernetes connection {conn_id!r} requires options['base_url'] {hint}"
            )

        if not in_cluster and secret is None:
            raise ValueError(
                f"kubernetes connection {conn_id!r} requires a bearer ServiceAccount token; "
                'set secret_ref on the connection (e.g. "env:APEX_INTEGRATION_K8S_TOKEN"), '
                'or set options["auth_mode"]="in_cluster" to use the pod ServiceAccount'
            )

        self._base_url = base_url.rstrip("/")
        self._allow_private_hosts = in_cluster or private_hosts_allowed(options)
        # bearer: static token from the resolved secret. in_cluster: the projected
        # token is read per client rebuild (rotation), unless a secret is supplied.
        self._static_token: str | None = secret.value if secret is not None else None
        self._verify = _resolve_verify(in_cluster, options.get("verify_tls"))
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
        bindings = self._options.get("environment_namespaces")
        if bindings is not None:
            if not isinstance(bindings, dict):
                raise ValueError("options['environment_namespaces'] must be an object")
            bound = bindings.get(env_ref.id)
            if bound is not None:
                values = bound if isinstance(bound, (list, tuple)) else [bound]
                namespaces = [str(namespace).strip() for namespace in values]
                if namespaces and all(namespaces):
                    return namespaces
                raise ValueError(f"environment {env_ref.id!r} has an empty namespace binding")
        configured = self._options.get("namespaces")
        if isinstance(configured, str):
            configured = [configured]
        if isinstance(configured, (list, tuple)) and configured:
            return [str(ns) for ns in configured]
        single = self._options.get("namespace")
        if single:
            return [str(single)]
        if self._in_cluster:
            pod_namespace = _read_file(IN_CLUSTER_NAMESPACE_PATH)
            if pod_namespace:
                return [pod_namespace]
        raise ValueError(
            f"no namespace configured for environment {env_ref.id!r}: set "
            "options['environment_namespaces'], options['namespace'], or "
            "options['namespaces'] on the connection, or (in_cluster) mount the "
            "pod's serviceaccount namespace file"
        )

    def _bearer_token(self) -> str:
        if self._static_token is not None:
            return self._static_token
        # in_cluster without an explicit secret: read (and thereby refresh) the
        # projected ServiceAccount token on every client rebuild.
        token = _read_file(IN_CLUSTER_TOKEN_PATH)
        if not token:
            raise RuntimeError(
                "in_cluster kubernetes auth: ServiceAccount token not readable at "
                f"{IN_CLUSTER_TOKEN_PATH}; is the pod's serviceaccount token mounted?"
            )
        return token

    def _client_for_loop(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop:
            self._client = safe_async_http_client(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._bearer_token()}"},
                verify=self._verify,
                timeout=DEFAULT_TIMEOUT_S,
                allow_private_hosts=self._allow_private_hosts,
            )
            self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        self._client_loop = None

    # ── HTTP + error mapping ──────────────────────────────────────────────────

    async def _get_json(
        self, path: str, *, namespace: str, tolerate_404: bool = False
    ) -> dict[str, Any] | None:
        client = self._client_for_loop()
        try:
            response = await resilient_request(client, "GET", path)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"kubernetes API request failed for GET {path} on {self._base_url}: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"kubernetes API denied GET {path} ({response.status_code}): "
                f"{_status_message(response)}; check the ServiceAccount token "
                "(connection secret_ref or in_cluster pod identity) and its RBAC bindings"
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


def _in_cluster_base_url() -> str:
    """API-server URL from the in-cluster env, or '' when not running in a pod."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    if not host:
        return ""
    port = (
        os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS")
        or os.environ.get("KUBERNETES_SERVICE_PORT")
        or "443"
    ).strip()
    host_part = f"[{host}]" if ":" in host else host  # bracket IPv6 literals
    return f"https://{host_part}:{port}"


def _resolve_verify(in_cluster: bool, verify_tls_option: Any) -> bool | str:
    """httpx ``verify`` value. in_cluster defaults to the SA CA bundle when present."""
    verify_tls = coerce_bool(verify_tls_option, default=True)
    if not in_cluster:
        return verify_tls
    if not verify_tls:
        return False
    return IN_CLUSTER_CA_PATH if os.path.exists(IN_CLUSTER_CA_PATH) else True


def _read_file(path: str) -> str:
    """Best-effort read of a mounted file; '' when absent/unreadable."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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
