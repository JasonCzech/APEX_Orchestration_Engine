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

Namespace selection for ``in_cluster`` ambient identity requires an exact
``options["environment_namespaces"]`` binding looked up by immutable environment
id. Bearer connections may instead use connection-wide ``namespaces`` or
``namespace`` fallbacks. The editable catalog display name and the pod namespace
are intentionally never authorization inputs.

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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from apex.adapters.http_resilience import (
    DEFAULT_JSON_RESPONSE_BYTES,
    ResponseTooLargeError,
    parse_json_response,
    resilient_request,
)
from apex.adapters.network_safety import private_hosts_allowed, safe_async_http_client
from apex.adapters.options import coerce_bool
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.diagnostics import bounded_diagnostic, safe_type_name
from apex.domain.integrations import (
    MAX_INVENTORY_SERVICES,
    EnvironmentSnapshot,
    EnvRef,
    SecretValue,
    ServiceInfo,
)

DEFAULT_TIMEOUT_S = 15.0
MAX_NAMESPACES_PER_SCAN = 16
MAX_SCAN_DECODED_BYTES = 16 * 1024 * 1024
MAX_SCAN_DURATION_S = 30.0
MAX_SERVICE_ACCOUNT_TOKEN_BYTES = 64 * 1024

# Projected ServiceAccount mount (in_cluster mode). Module-level so tests can
# point them at fixtures via monkeypatch.
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
IN_CLUSTER_TOKEN_PATH = f"{SA_DIR}/token"
IN_CLUSTER_CA_PATH = f"{SA_DIR}/ca.crt"
IN_CLUSTER_NAMESPACE_PATH = f"{SA_DIR}/namespace"

_IN_CLUSTER_ALIASES = frozenset({"in_cluster", "in-cluster", "incluster"})
_NAMESPACE_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


@dataclass(slots=True)
class _ScanBudget:
    limit: int
    decoded_bytes: int = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.decoded_bytes

    def next_response_limit(self) -> int:
        if self.remaining < 1:
            raise RuntimeError(
                f"kubernetes inventory scan exhausted its aggregate decoded-body "
                f"budget of {self.limit} bytes"
            )
        return min(DEFAULT_JSON_RESPONSE_BYTES, self.remaining)

    def consume(self, size: int) -> None:
        if size < 0 or size > self.remaining:
            raise RuntimeError(
                f"kubernetes inventory scan exceeded its aggregate decoded-body "
                f"budget of {self.limit} bytes"
            )
        self.decoded_bytes += size


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
        _validate_namespace_options(options)

        raw_mode = str(options.get("auth_mode", "bearer")).strip().lower()
        in_cluster = raw_mode in _IN_CLUSTER_ALIASES
        if not in_cluster and raw_mode not in ("bearer", ""):
            raise ValueError(
                f"kubernetes connection {conn_id!r} has unknown auth_mode={raw_mode!r}; "
                "expected 'bearer' or 'in_cluster'"
            )
        self._in_cluster = in_cluster

        configured_base_url = str(options.get("base_url", "")).strip()
        if in_cluster and configured_base_url:
            raise ValueError(
                f"kubernetes connection {conn_id!r} cannot set base_url with in_cluster auth; "
                "the API server is pinned to KUBERNETES_SERVICE_HOST"
            )
        base_url = _in_cluster_base_url() if in_cluster else configured_base_url
        if not base_url:
            hint = (
                "in_cluster auth requires running inside a pod (KUBERNETES_SERVICE_HOST is unset)"
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
        self._static_token: str | None = (
            _validated_bearer_token(secret.value, source="connection secret")
            if secret is not None
            else None
        )
        if in_cluster:
            if not os.path.isfile(IN_CLUSTER_CA_PATH):
                raise ValueError(
                    f"kubernetes connection {conn_id!r} requires the in-cluster "
                    f"ServiceAccount CA bundle at {IN_CLUSTER_CA_PATH}"
                )
            # Ambient credentials are sent only to the Kubernetes-injected API
            # endpoint and authenticated with its projected CA. A connection
            # option cannot disable or replace either trust anchor.
            self._verify = IN_CLUSTER_CA_PATH
        else:
            self._verify = _resolve_verify(False, options.get("verify_tls"))
        self._options = options
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    # ── port surface ──────────────────────────────────────────────────────────

    async def scan_environment(self, env_ref: EnvRef) -> EnvironmentSnapshot:
        snapshot: EnvironmentSnapshot | None = None
        timed_out = False
        try:
            async with asyncio.timeout(MAX_SCAN_DURATION_S):
                snapshot = await self._scan_environment(env_ref)
        except TimeoutError:
            timed_out = True
        if timed_out:
            raise RuntimeError(
                f"kubernetes inventory scan exceeded its {MAX_SCAN_DURATION_S:g}s deadline"
            )
        if snapshot is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("kubernetes inventory scan completed without a snapshot")
        return snapshot

    async def _scan_environment(self, env_ref: EnvRef) -> EnvironmentSnapshot:
        services: list[ServiceInfo] = []
        budget = _ScanBudget(MAX_SCAN_DECODED_BYTES)
        for namespace in self._namespaces_for(env_ref):
            deployments = (
                await self._get_json(
                    f"/apis/apps/v1/namespaces/{namespace}/deployments",
                    namespace=namespace,
                    budget=budget,
                )
                or {}
            )
            deployment_items = _deployment_items(deployments)
            if len(services) + len(deployment_items) > MAX_INVENTORY_SERVICES:
                raise RuntimeError(
                    "kubernetes inventory scan exceeded its aggregate service limit of "
                    f"{MAX_INVENTORY_SERVICES}"
                )
            # Check and map the aggregate before making the next provider call.
            # An oversized deployment list cannot trigger service/ingress fanout.
            services.extend(_service_info(item) for item in deployment_items)
            # Fetched to validate connectivity/RBAC and pin the wire contract;
            # the domain model has no fields for endpoints/routing yet.
            service_payload = await self._get_json(
                f"/api/v1/namespaces/{namespace}/services",
                namespace=namespace,
                budget=budget,
            )
            assert service_payload is not None
            _resource_items(service_payload, resource="service")
            ingress_payload = await self._get_json(
                f"/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses",
                namespace=namespace,
                tolerate_404=True,
                budget=budget,
            )
            if ingress_payload is not None:
                _resource_items(ingress_payload, resource="ingress")
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
                return _validated_namespaces(values, source=f"environment {env_ref.id!r}")
        if self._in_cluster:
            # The pod ServiceAccount is ambient, shared process identity. Never
            # let an arbitrary environment select a connection-wide or pod
            # namespace: a platform administrator must bind each immutable
            # environment id explicitly before any token-authenticated request.
            raise ValueError(
                f"in_cluster connection has no namespace binding for environment "
                f"{env_ref.id!r}; set options['environment_namespaces'][{env_ref.id!r}]"
            )
        configured = self._options.get("namespaces")
        if isinstance(configured, str):
            configured = [configured]
        if isinstance(configured, (list, tuple)) and configured:
            return _validated_namespaces(configured, source="connection")
        single = self._options.get("namespace")
        if single:
            return _validated_namespaces([single], source="connection")
        raise ValueError(
            f"no namespace configured for environment {env_ref.id!r}: set "
            "options['environment_namespaces'], options['namespace'], or "
            "options['namespaces'] on the connection"
        )

    def _bearer_token(self) -> str:
        if self._static_token is not None:
            return self._static_token
        # in_cluster without an explicit secret: read (and thereby refresh) the
        # projected ServiceAccount token on every client rebuild.
        return _read_service_account_token(IN_CLUSTER_TOKEN_PATH)

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
        self,
        path: str,
        *,
        namespace: str,
        budget: _ScanBudget,
        tolerate_404: bool = False,
    ) -> dict[str, Any] | None:
        client = self._client_for_loop()
        response: httpx.Response | None = None
        request_failure: RuntimeError | None = None
        try:
            response = await resilient_request(
                client,
                "GET",
                path,
                max_response_bytes=budget.next_response_limit(),
            )
        except ResponseTooLargeError:
            request_failure = RuntimeError(
                "kubernetes inventory scan exceeded its decoded-body response budget"
            )
        except httpx.HTTPError as exc:
            detail = bounded_diagnostic(exc)
            request_failure = RuntimeError(
                bounded_diagnostic(
                    f"kubernetes API request failed for GET {path} on {self._base_url}: "
                    f"{safe_type_name(exc)}: {detail}"
                )
            )
        if request_failure is not None:
            raise request_failure
        if response is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("kubernetes API request completed without a response")
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
        budget.consume(len(response.content))
        payload: Any = None
        invalid_json = False
        try:
            payload = parse_json_response(
                response,
                context=f"kubernetes API response for GET {path}",
            )
        except RuntimeError:
            invalid_json = True
        if invalid_json:
            raise RuntimeError(f"kubernetes API returned invalid JSON for GET {path}")
        if not isinstance(payload, dict):
            raise RuntimeError(f"kubernetes API returned a non-object JSON body for GET {path}")
        return payload


def _validated_namespaces(values: list[Any] | tuple[Any, ...], *, source: str) -> list[str]:
    if len(values) > MAX_NAMESPACES_PER_SCAN:
        raise ValueError(
            f"{source} may configure at most {MAX_NAMESPACES_PER_SCAN} Kubernetes namespaces"
        )
    if any(not isinstance(namespace, str) for namespace in values):
        raise ValueError(f"{source} has a non-string Kubernetes namespace")
    namespaces = [namespace.strip() for namespace in values]
    if not namespaces or not all(_NAMESPACE_RE.fullmatch(namespace) for namespace in namespaces):
        raise ValueError(f"{source} has an invalid Kubernetes namespace; expected a DNS-1123 label")
    if len(set(namespaces)) != len(namespaces):
        raise ValueError(f"{source} contains duplicate Kubernetes namespaces")
    return namespaces


def _validate_namespace_options(options: dict[str, Any]) -> None:
    """Fail connection construction before an oversized namespace fanout is usable."""

    bindings = options.get("environment_namespaces")
    if bindings is not None:
        if not isinstance(bindings, dict):
            raise ValueError("options['environment_namespaces'] must be an object")
        for environment_id, bound in bindings.items():
            if bound is None:
                continue
            values = bound if isinstance(bound, (list, tuple)) else [bound]
            _validated_namespaces(values, source=f"environment {environment_id!r}")

    configured = options.get("namespaces")
    if configured is not None:
        values = [configured] if isinstance(configured, str) else configured
        if not isinstance(values, (list, tuple)):
            raise ValueError("options['namespaces'] must be a namespace or list of namespaces")
        _validated_namespaces(values, source="connection")

    if "namespace" in options:
        _validated_namespaces([options["namespace"]], source="connection")


def _deployment_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _resource_items(payload, resource="deployment")


def _resource_items(payload: dict[str, Any], *, resource: str) -> list[dict[str, Any]]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not all(isinstance(item, dict) for item in raw_items):
        raise RuntimeError(f"kubernetes {resource} list has malformed items")
    return raw_items


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


def _validated_bearer_token(value: object, *, source: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"kubernetes {source} is not a string")
    token = value.strip()
    if not token or len(token.encode("utf-8")) > MAX_SERVICE_ACCOUNT_TOKEN_BYTES:
        raise RuntimeError(
            f"kubernetes {source} must be 1-{MAX_SERVICE_ACCOUNT_TOKEN_BYTES} UTF-8 bytes"
        )
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in token):
        raise RuntimeError(f"kubernetes {source} contains invalid HTTP header characters")
    return token


def _read_service_account_token(path: str) -> str:
    """Read a projected token with a hard byte cap and strict header validation."""
    raw: bytes | None = None
    unreadable = False
    try:
        with Path(path).open("rb") as token_file:
            raw = token_file.read(MAX_SERVICE_ACCOUNT_TOKEN_BYTES + 1)
    except OSError:
        unreadable = True
    if unreadable:
        raise RuntimeError(
            "in_cluster kubernetes auth: ServiceAccount token not readable at "
            f"{path}; is the pod's serviceaccount token mounted?"
        )
    if raw is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("in_cluster kubernetes auth: ServiceAccount token could not be read")
    if len(raw) > MAX_SERVICE_ACCOUNT_TOKEN_BYTES:
        raise RuntimeError(
            f"in_cluster kubernetes auth: ServiceAccount token exceeds "
            f"{MAX_SERVICE_ACCOUNT_TOKEN_BYTES} bytes"
        )
    decoded: str | None = None
    invalid_utf8 = False
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        invalid_utf8 = True
    if invalid_utf8:
        raise RuntimeError("in_cluster kubernetes auth: ServiceAccount token is not valid UTF-8")
    if decoded is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("in_cluster kubernetes auth: ServiceAccount token could not be decoded")
    return _validated_bearer_token(decoded, source="ServiceAccount token")


def _service_info(deployment: dict[str, Any]) -> ServiceInfo:
    """Deployment item -> ServiceInfo. k8s omits readyReplicas when zero are ready."""
    metadata = deployment.get("metadata")
    raw_status = deployment.get("status")
    spec = deployment.get("spec")
    status = {} if raw_status is None else raw_status
    if not isinstance(metadata, dict) or not isinstance(status, dict) or not isinstance(spec, dict):
        raise RuntimeError("kubernetes deployment response has malformed metadata/status/spec")
    template = spec.get("template")
    if not isinstance(template, dict):
        raise RuntimeError("kubernetes deployment response has malformed pod template")
    template_spec = template.get("spec")
    if not isinstance(template_spec, dict):
        raise RuntimeError("kubernetes deployment response has malformed pod template spec")
    containers = template_spec.get("containers")
    if not isinstance(containers, list) or not containers or not isinstance(containers[0], dict):
        raise RuntimeError("kubernetes deployment response has no valid container")
    name = metadata.get("name")
    image = containers[0].get("image")
    ready_replicas = status.get("readyReplicas", 0)
    if not isinstance(name, str) or not isinstance(image, str):
        raise RuntimeError("kubernetes deployment response contains non-string name or image")
    if isinstance(ready_replicas, bool) or not isinstance(ready_replicas, int):
        raise RuntimeError("kubernetes deployment readyReplicas must be an integer")
    service: ServiceInfo | None = None
    invalid_service = False
    try:
        service = ServiceInfo(
            name=name,
            replicas=ready_replicas,
            image=image,
        )
    except (TypeError, ValueError, ValidationError):
        invalid_service = True
    if invalid_service:
        raise RuntimeError("kubernetes deployment response contains invalid service data")
    if service is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("kubernetes deployment response contains invalid service data")
    return service


def _status_message(response: httpx.Response) -> str:
    """Best-effort message from a Kubernetes Status body; falls back to raw text."""
    try:
        body = parse_json_response(response, context="kubernetes error response")
    except RuntimeError:
        body = None
    if isinstance(body, dict) and body.get("message"):
        return bounded_diagnostic(body["message"])
    text = bounded_diagnostic(response.text.strip(), max_chars=300)
    return text if text else bounded_diagnostic(response.reason_phrase, max_chars=300)
