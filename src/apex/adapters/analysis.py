"""HTTP clients for the proposed analysis v1 gateway contract."""

from urllib.parse import quote

from apex.adapters.http_resilience import parse_json_bytes, read_bounded_response
from apex.adapters.network_safety import private_hosts_allowed, safe_async_http_client
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.diagnostics import contains_credential_material
from apex.domain.integrations import SecretValue
from apex.ports.analysis import AnalysisCapabilities, AnalysisJob, AnalysisResult


@AdapterRegistry.register(PortKind.ANALYSIS, "lra_engine")
@AdapterRegistry.register(PortKind.ANALYSIS, "perfreportforge")
@AdapterRegistry.register(PortKind.ANALYSIS, "other")
class HttpAnalysisAdapter:
    def __init__(self, conn: ConnectionConfig, secret: SecretValue | None = None):
        self.conn = conn
        self.secret = secret

    async def _request(self, method, path, body=None):
        from apex.services.connections import validate_adapter_base_url

        base_url = self.conn.options.get("base_url")
        if not isinstance(base_url, str):
            raise ValueError("Analysis connection requires base_url")
        validate_adapter_base_url(
            base_url, allow_private_hosts=private_hosts_allowed(self.conn.options)
        )
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret.value}"
        if body:
            headers["Idempotency-Key"] = body["idempotency_key"]
        async with safe_async_http_client(
            base_url=base_url,
            headers=headers,
            timeout=30,
            allow_private_hosts=private_hosts_allowed(self.conn.options),
        ) as client:
            async with client.stream(method, path, json=body) as response:
                response.raise_for_status()
                raw = await read_bounded_response(response, max_bytes=256000)
                data = parse_json_bytes(raw.content, context="analysis response")
        if contains_credential_material(data):
            raise ValueError("Analysis response contains credential material")
        return data

    async def capabilities(self):
        return AnalysisCapabilities.model_validate(await self._request("GET", "/v1/capabilities"))

    async def submit(self, request):
        return AnalysisJob.model_validate(await self._request("POST", "/v1/analyses", request))

    async def status(self, job_id):
        return AnalysisJob.model_validate(
            await self._request("GET", f"/v1/analyses/{quote(job_id, safe='')}")
        )

    async def result(self, job_id):
        return AnalysisResult.model_validate(
            await self._request("GET", f"/v1/analyses/{quote(job_id, safe='')}/result")
        )
