"""Stub document retrieval: two canned hits with fetchable content."""

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import DocHit, DocRef, DocScope, DocumentContent, SecretValue

_DOCS: dict[str, tuple[DocHit, str]] = {
    "doc-checkout-runbook": (
        DocHit(
            ref=DocRef(
                id="doc-checkout-runbook", source="stub", uri="stub://docs/checkout-runbook"
            ),
            title="Checkout service performance runbook",
            snippet="p95 budget 250ms; scale payment-svc before checkout-api under load...",
            score=0.92,
        ),
        "# Checkout service performance runbook\n\n"
        "- p95 budget: 250ms at 200 vusers\n"
        "- Scale payment-svc before checkout-api under load.\n"
        "- card-gateway timeouts cascade into pool exhaustion (pool_size=20).\n",
    ),
    "doc-load-test-standards": (
        DocHit(
            ref=DocRef(
                id="doc-load-test-standards", source="stub", uri="stub://docs/load-test-standards"
            ),
            title="Load testing standards and SLA catalog",
            snippet="Standard ramp: 20% of duration; abort on error rate > 2% sustained...",
            score=0.81,
        ),
        "# Load testing standards\n\n"
        "- Standard ramp: 20% of total duration.\n"
        "- Abort criteria: error rate > 2% sustained for 60s.\n"
        "- Report KPIs: tps_avg, p95_ms, error_rate, vusers_peak.\n",
    ),
}


@AdapterRegistry.register(PortKind.DOCUMENTS, "stub")
class StubDocumentsAdapter:
    def __init__(
        self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
    ) -> None:
        self._conn = conn

    async def search(self, query: str, *, scope: DocScope, k: int = 10) -> list[DocHit]:
        hits = [hit.model_copy(deep=True) for hit, _ in _DOCS.values()]
        return hits[:k]

    async def fetch(self, ref: DocRef) -> DocumentContent:
        try:
            hit, text = _DOCS[ref.id]
        except KeyError:
            raise KeyError(f"document {ref.id!r} not found in stub corpus") from None
        return DocumentContent(
            ref=hit.ref.model_copy(deep=True), media_type="text/markdown", text=text
        )
