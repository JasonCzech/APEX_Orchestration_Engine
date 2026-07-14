"""Integration-facing domain models (pydantic, lean; zero IO).

These shapes cross the port boundary: adapters normalize provider payloads into
them, and graph nodes/services/routers consume them. Keep them provider- and
engine-neutral; provider quirks belong inside adapters.
"""

from typing import Any

from pydantic import BaseModel, Field, FiniteFloat, field_serializer, field_validator

from apex.domain.pipeline import new_id, utcnow_iso

# --- shared paging / time -------------------------------------------------


class Page(BaseModel):
    offset: int = 0
    limit: int = 50


class TimeWindow(BaseModel):
    """ISO-8601 bounds; None means unbounded on that side."""

    start: str | None = None
    end: str | None = None


# --- work tracking ---------------------------------------------------------


class WorkItem(BaseModel):
    key: str
    title: str
    kind: str = "story"
    status: str = "open"
    description: str = ""
    url: str | None = None


class WorkItemPage(BaseModel):
    items: list[WorkItem] = Field(default_factory=list)
    total: int = 0
    page: Page = Field(default_factory=Page)


class QueryContext(BaseModel):
    project_id: str | None = None
    app_id: str | None = None
    hints: dict[str, str] = Field(default_factory=dict)


class TranslatedQuery(BaseModel):
    provider: str
    query: str
    confidence: float = 1.0


class WorkItemFilters(BaseModel):
    status: str | None = None
    kind: str | None = None
    text: str | None = None


class WorkItemDraft(BaseModel):
    title: str
    kind: str = "story"
    description: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)


class Enrichment(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)
    comment: str | None = None


# --- log search ------------------------------------------------------------


class LogQuery(BaseModel):
    query: str
    filters: dict[str, str] = Field(default_factory=dict)


class LogEntry(BaseModel):
    at: str
    level: str = "INFO"
    service: str = ""
    message: str


class LogSearchResult(BaseModel):
    entries: list[LogEntry] = Field(default_factory=list)
    total: int = 0


# --- observability ---------------------------------------------------------


class MetricQuery(BaseModel):
    query: str
    step_s: float = 60.0


class MetricPoint(BaseModel):
    at: str
    value: float


class MetricSeries(BaseModel):
    name: str
    points: list[MetricPoint] = Field(default_factory=list)


class ServiceHealth(BaseModel):
    service: str
    healthy: bool = True
    status: str = "healthy"
    indicators: dict[str, float] = Field(default_factory=dict)


# --- documents -------------------------------------------------------------


class DocScope(BaseModel):
    project_id: str | None = None
    collections: list[str] = Field(default_factory=list)


class DocRef(BaseModel):
    id: str
    source: str = "stub"
    uri: str | None = None


class DocHit(BaseModel):
    ref: DocRef
    title: str
    snippet: str = ""
    score: float = 0.0


class DocumentContent(BaseModel):
    ref: DocRef
    media_type: str = "text/markdown"
    text: str


# --- cluster inventory -----------------------------------------------------


class EnvRef(BaseModel):
    id: str
    name: str | None = None


class ServiceInfo(BaseModel):
    name: str
    replicas: int = 1
    image: str = ""


class EnvironmentSnapshot(BaseModel):
    services: list[ServiceInfo] = Field(default_factory=list)
    scanned_at: str = Field(default_factory=utcnow_iso)


# --- source control ----------------------------------------------------------


class RepoRef(BaseModel):
    name: str
    url: str | None = None


class FileContent(BaseModel):
    path: str
    ref: str = "HEAD"
    text: str
    media_type: str = "text/plain"


# --- secrets -----------------------------------------------------------------


class SecretValue(BaseModel):
    """Carries a resolved secret. repr/str are redacted; never log or persist
    the model."""

    value: str

    @field_serializer("value")
    def _serialize_value(self, value: str) -> str:
        return "***"

    def __repr__(self) -> str:
        return "SecretValue(value='***')"

    def __str__(self) -> str:
        return "***"


# --- execution engine --------------------------------------------------------


class LoadTestSpec(BaseModel):
    """Engine-neutral output of script_scenario; input to every engine adapter."""

    idempotency_key: str = Field(default_factory=new_id, min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=1_000)
    script_refs: list[str] = Field(default_factory=list, max_length=100)
    vusers: int = Field(default=10, ge=1, le=10_000)
    ramp_s: FiniteFloat = Field(default=5, ge=0, le=86_400)
    duration_s: FiniteFloat = Field(default=2, gt=0, le=86_400)
    slas: dict[str, FiniteFloat] = Field(default_factory=dict, max_length=32)
    target_environment: str | None = Field(default=None, max_length=2_048)

    @field_validator("script_refs")
    @classmethod
    def validate_script_refs(cls, values: list[str]) -> list[str]:
        for ref in values:
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError("script_refs entries must be non-empty strings")
            if len(ref) > 2_048:
                raise ValueError("script_refs entries must not exceed 2048 characters")
        return values

    @field_validator("slas")
    @classmethod
    def validate_slas(cls, values: dict[str, float]) -> dict[str, float]:
        for name, value in values.items():
            if not name.strip() or len(name) > 64:
                raise ValueError("SLA names must be 1-64 characters")
            if value < 0 or value > 1_000_000_000_000:
                raise ValueError("SLA values must be between 0 and 1e12")
            if name == "error_rate" and value > 1:
                raise ValueError("error_rate SLA must be a fraction between 0 and 1")
        return values


class ValidationReport(BaseModel):
    ok: bool = True
    issues: list[str] = Field(default_factory=list)


class TestResultSummary(BaseModel):
    """Normalized KPIs handed to the reporting phase.

    Conventional kpis keys: tps_avg, p95_ms, error_rate, vusers_peak.
    """

    engine: str
    passed: bool
    kpis: dict[str, float] = Field(default_factory=dict)
    sla_breaches: list[str] = Field(default_factory=list)
    notes: str | None = None
