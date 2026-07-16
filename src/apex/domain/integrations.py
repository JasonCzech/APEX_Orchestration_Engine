"""Integration-facing domain models (pydantic, lean; zero IO).

These shapes cross the port boundary: adapters normalize provider payloads into
them, and graph nodes/services/routers consume them. Keep them provider- and
engine-neutral; provider quirks belong inside adapters.
"""

from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    field_serializer,
    field_validator,
    model_validator,
)

from apex.domain.diagnostics import contains_credential_material
from apex.domain.input_limits import (
    MAX_DESCRIPTION_CHARS,
    NoNulStr,
    ScopeId,
    validate_json_object,
)
from apex.domain.pipeline import new_id, utcnow_iso

# --- shared paging / time -------------------------------------------------


class Page(BaseModel):
    offset: int = Field(default=0, ge=0, le=1_000)
    limit: int = Field(default=50, ge=1, le=200)


class TimeWindow(BaseModel):
    """ISO-8601 bounds; None means unbounded on that side."""

    start: NoNulStr | None = Field(default=None, max_length=64)
    end: NoNulStr | None = Field(default=None, max_length=64)


# --- work tracking ---------------------------------------------------------


class WorkItem(BaseModel):
    # Provider responses cross a trust boundary and may be persisted in mutation
    # outbox JSONB. Bound every scalar before it reaches graph state or storage.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    key: NoNulStr = Field(max_length=255)
    title: NoNulStr = Field(max_length=500)
    kind: NoNulStr = Field(default="story", max_length=64)
    status: NoNulStr = Field(default="open", max_length=255)
    description: NoNulStr = Field(default="", max_length=MAX_DESCRIPTION_CHARS)
    url: NoNulStr | None = Field(default=None, max_length=4096)


class WorkItemPage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    items: list[WorkItem] = Field(default_factory=list, max_length=200)
    total: int = Field(default=0, ge=0, le=9_223_372_036_854_775_807)
    page: Page = Field(default_factory=Page)


class QueryContext(BaseModel):
    project_id: ScopeId | None = None
    app_id: ScopeId | None = None
    hints: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("hints")
    @classmethod
    def validate_hints(cls, values: dict[str, str]) -> dict[str, str]:
        if any(
            not key or len(key) > 128 or "\x00" in key or len(value) > 2_048 or "\x00" in value
            for key, value in values.items()
        ):
            raise ValueError("query hints must contain bounded NUL-free names and values")
        return values


class TranslatedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: NoNulStr = Field(min_length=1, max_length=64)
    query: NoNulStr = Field(min_length=1, max_length=20_000)
    confidence: FiniteFloat = Field(default=1.0, ge=0, le=1)


class WorkItemFilters(BaseModel):
    status: NoNulStr | None = Field(default=None, max_length=255)
    kind: NoNulStr | None = Field(default=None, max_length=64)
    text: NoNulStr | None = Field(default=None, max_length=2_000)


class WorkItemDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    title: NoNulStr = Field(min_length=1, max_length=500)
    kind: NoNulStr = Field(default="story", min_length=1, max_length=64)
    description: NoNulStr = Field(default="", max_length=MAX_DESCRIPTION_CHARS)
    fields: dict[str, Any] = Field(default_factory=dict, max_length=64)

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, values: dict[str, Any]) -> dict[str, Any]:
        validate_json_object(values, label="work item fields")
        return values

    @model_validator(mode="after")
    def reject_credential_material(self) -> "WorkItemDraft":
        if contains_credential_material(self.model_dump(mode="json")):
            raise ValueError("work item draft must not contain credential material")
        return self


class Enrichment(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    fields: dict[str, Any] = Field(default_factory=dict, max_length=64)
    comment: NoNulStr | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, values: dict[str, Any]) -> dict[str, Any]:
        validate_json_object(values, label="work item enrichment fields")
        return values

    @model_validator(mode="after")
    def reject_credential_material(self) -> "Enrichment":
        if contains_credential_material(self.model_dump(mode="json")):
            raise ValueError("work item enrichment must not contain credential material")
        return self


# --- log search ------------------------------------------------------------


class LogQuery(BaseModel):
    query: NoNulStr = Field(max_length=2_048)
    filters: dict[str, NoNulStr] = Field(default_factory=dict, max_length=12)

    @field_validator("filters")
    @classmethod
    def validate_log_filters(cls, values: dict[str, str]) -> dict[str, str]:
        for key, value in values.items():
            if not key or len(key) > 255 or "\x00" in key:
                raise ValueError("log filter names must be 1-255 characters without U+0000")
            if not value or len(value) > 512:
                raise ValueError("log filter values must be 1-512 characters")
        return values


class LogEntry(BaseModel):
    at: NoNulStr = Field(max_length=128)
    level: NoNulStr = Field(default="INFO", max_length=64)
    service: NoNulStr = Field(default="", max_length=255)
    message: NoNulStr = Field(max_length=20_000)


class LogSearchResult(BaseModel):
    entries: list[LogEntry] = Field(default_factory=list, max_length=500)
    total: int = Field(default=0, ge=0, le=9_223_372_036_854_775_807)


# --- observability ---------------------------------------------------------


class MetricQuery(BaseModel):
    query: NoNulStr = Field(min_length=1, max_length=20_000)
    step_s: FiniteFloat = Field(default=60.0, gt=0, le=86_400)


class MetricPoint(BaseModel):
    at: NoNulStr = Field(min_length=1, max_length=64)
    value: FiniteFloat = Field(ge=-1_000_000_000_000, le=1_000_000_000_000)


class MetricSeries(BaseModel):
    name: NoNulStr = Field(min_length=1, max_length=255)
    points: list[MetricPoint] = Field(default_factory=list, max_length=10_000)


class ServiceHealth(BaseModel):
    service: NoNulStr = Field(min_length=1, max_length=255)
    healthy: bool = True
    status: NoNulStr = Field(default="healthy", min_length=1, max_length=255)
    indicators: dict[str, FiniteFloat] = Field(default_factory=dict, max_length=64)

    @field_validator("indicators")
    @classmethod
    def validate_indicators(cls, values: dict[str, float]) -> dict[str, float]:
        if any(
            not name or len(name) > 128 or "\x00" in name or abs(value) > 1_000_000_000_000
            for name, value in values.items()
        ):
            raise ValueError("health indicators must have bounded names and finite values")
        return values


# --- documents -------------------------------------------------------------


class DocScope(BaseModel):
    project_id: ScopeId | None = None
    collections: list[NoNulStr] = Field(default_factory=list, max_length=32)

    @field_validator("collections")
    @classmethod
    def validate_collections(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 255 for value in values):
            raise ValueError("document collections must contain 1-255 characters")
        return values


class DocRef(BaseModel):
    id: NoNulStr = Field(min_length=1, max_length=255)
    source: NoNulStr = Field(default="stub", min_length=1, max_length=64)
    uri: NoNulStr | None = Field(default=None, max_length=4_096)


class DocHit(BaseModel):
    ref: DocRef
    title: NoNulStr = Field(max_length=500)
    snippet: NoNulStr = Field(default="", max_length=20_000)
    score: FiniteFloat = Field(default=0.0, ge=-1_000_000_000_000, le=1_000_000_000_000)


class DocumentContent(BaseModel):
    ref: DocRef
    media_type: NoNulStr = Field(default="text/markdown", min_length=1, max_length=255)
    text: NoNulStr = Field(max_length=2_000_000)


# --- cluster inventory -----------------------------------------------------

MAX_INVENTORY_SERVICES = 5_000


class EnvRef(BaseModel):
    id: NoNulStr = Field(min_length=1, max_length=255)
    name: NoNulStr | None = Field(default=None, max_length=500)


class ServiceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: NoNulStr = Field(min_length=1, max_length=253)
    replicas: int = Field(default=1, ge=0, le=10_000_000)
    image: NoNulStr = Field(default="", max_length=2_048)


class EnvironmentSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    services: list[ServiceInfo] = Field(default_factory=list, max_length=MAX_INVENTORY_SERVICES)
    scanned_at: NoNulStr = Field(default_factory=utcnow_iso, min_length=1, max_length=64)


# --- source control ----------------------------------------------------------


class RepoRef(BaseModel):
    name: NoNulStr = Field(min_length=1, max_length=255)
    url: NoNulStr | None = Field(default=None, max_length=4_096)


class FileContent(BaseModel):
    path: NoNulStr = Field(min_length=1, max_length=1_024)
    ref: NoNulStr = Field(default="HEAD", min_length=1, max_length=255)
    text: NoNulStr = Field(max_length=2_000_000)
    media_type: NoNulStr = Field(default="text/plain", min_length=1, max_length=255)


# --- secrets -----------------------------------------------------------------


class SecretValue(BaseModel):
    """Carries a resolved secret. repr/str are redacted; never log or persist
    the model."""

    # Validation can fail before a SecretValue instance exists (for example an
    # invalid environment-backed secret containing a NUL). Keep Pydantic from
    # embedding that raw input in provider errors, graph checkpoints, or an
    # unhandled traceback.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    value: NoNulStr = Field(min_length=1, max_length=1_048_576)

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

    model_config = ConfigDict(extra="forbid")

    idempotency_key: NoNulStr = Field(default_factory=new_id, min_length=1, max_length=256)
    title: NoNulStr = Field(min_length=1, max_length=1_000)
    script_refs: list[NoNulStr] = Field(default_factory=list, max_length=100)
    vusers: int = Field(default=10, ge=1, le=10_000)
    ramp_s: FiniteFloat = Field(default=5, ge=0, le=86_400)
    duration_s: FiniteFloat = Field(default=2, gt=0, le=86_400)
    slas: dict[str, FiniteFloat] = Field(default_factory=dict, max_length=32)
    target_environment: NoNulStr | None = Field(default=None, max_length=2_048)

    @field_validator("script_refs")
    @classmethod
    def validate_script_refs(cls, values: list[str]) -> list[str]:
        for ref in values:
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError("script_refs entries must be non-empty strings")
            if len(ref) > 2_048 or "\x00" in ref:
                raise ValueError("script_refs entries must not exceed 2048 characters")
        return values

    @field_validator("slas")
    @classmethod
    def validate_slas(cls, values: dict[str, float]) -> dict[str, float]:
        for name, value in values.items():
            if not name.strip() or len(name) > 64 or "\x00" in name:
                raise ValueError("SLA names must be 1-64 characters")
            if value < 0 or value > 1_000_000_000_000:
                raise ValueError("SLA values must be between 0 and 1e12")
            if name == "error_rate" and value > 1:
                raise ValueError("error_rate SLA must be a fraction between 0 and 1")
        return values


class ValidationReport(BaseModel):
    ok: bool = True
    issues: list[NoNulStr] = Field(default_factory=list, max_length=128)

    @field_validator("issues")
    @classmethod
    def validate_issues(cls, values: list[str]) -> list[str]:
        if any(not issue.strip() or len(issue) > 2_048 or "\x00" in issue for issue in values):
            raise ValueError("validation issues must be 1-2048 characters")
        return values


class TestResultSummary(BaseModel):
    """Normalized KPIs handed to the reporting phase.

    Conventional kpis keys: tps_avg, p95_ms, error_rate, vusers_peak.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    engine: NoNulStr = Field(min_length=1, max_length=64)
    passed: bool
    kpis: dict[str, FiniteFloat] = Field(default_factory=dict, max_length=64)
    sla_breaches: list[NoNulStr] = Field(default_factory=list, max_length=128)
    notes: NoNulStr | None = Field(default=None, max_length=20_000)

    @field_validator("kpis")
    @classmethod
    def validate_result_kpis(cls, values: dict[str, float]) -> dict[str, float]:
        for name, value in values.items():
            if not name.strip() or len(name) > 64 or "\x00" in name:
                raise ValueError("result KPI names must be 1-64 characters")
            if abs(value) > 1_000_000_000_000:
                raise ValueError("result KPI values must be between -1e12 and 1e12")
        return values

    @field_validator("sla_breaches")
    @classmethod
    def validate_sla_breaches(cls, values: list[str]) -> list[str]:
        if any(not breach.strip() or len(breach) > 2_048 or "\x00" in breach for breach in values):
            raise ValueError("SLA breach descriptions must be 1-2048 characters")
        return values
