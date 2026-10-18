"""Immutable execution preparation and honest prerequisite projections."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apex.domain.cycles import approval_current, version_digest
from apex.domain.diagnostics import contains_credential_material
from apex.domain.input_limits import RecordId
from apex.domain.integrations import LoadTestSpec


class PrepareExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=1)
    idempotency_key: RecordId
    plan_version_id: RecordId
    script_version_id: RecordId
    readiness_version_id: RecordId | None = None
    connection_id: RecordId | None = None
    launch_authority: Literal["pipeline", "native_schedule"] = "pipeline"

    @model_validator(mode="after")
    def safe(self) -> "PrepareExecution":
        if contains_credential_material(self.model_dump()):
            raise ValueError("execution preparation must not contain credentials")
        return self


class LinkCycleRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    engine_run_id: RecordId


class ArchiveExecutionPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)


class PreflightCheck(BaseModel):
    key: str
    label: str
    status: Literal["ready", "blocked", "not_checked"]
    detail: str


class ExecutionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_version_id: str
    plan_title: str
    plan_checksum: str
    script_version_id: str
    script_title: str
    script_checksum: str
    script_source_checksum: str
    script_format: str
    readiness_version_id: str | None
    readiness_checksum: str | None
    engine: Literal["apex-load", "loadrunner"]
    mode: Literal["live", "simulation"]
    environment_id: str
    environment_version: int | None
    connection_id: str | None
    connection_version: str | None
    launch_authority: Literal["pipeline", "native_schedule"]
    script_artifact_id: str | None = None
    script_artifact_stored_at: str | None = None
    execution_policy: Literal["sequential"] = "sequential"
    workload: dict[str, Any]


def _approved(versions: list[dict[str, Any]], version_id: str, kind: str) -> dict[str, Any]:
    version = next((v for v in versions if v["id"] == version_id), None)
    if version is None or version["kind"] != kind or not approval_current(versions, version_id):
        raise ValueError(f"Select a current approved {kind.replace('_', ' ')} version")
    if version_digest(version["content"]) != version["checksum"]:
        raise ValueError("Deliverable content no longer matches its approved checksum")
    return version


def prepare_snapshot(
    request: PrepareExecution,
    versions: list[dict[str, Any]],
    *,
    mode: Literal["live", "simulation"],
    environment_id: str,
    environment_version: int | None,
    connection_version: str | None,
) -> ExecutionSnapshot:
    plan = _approved(versions, request.plan_version_id, "test_plan")
    script = _approved(versions, request.script_version_id, "script")
    if plan["id"] not in script["dependencies"]:
        raise ValueError("The script must reference the selected approved test-plan version")
    readiness = None
    if request.readiness_version_id:
        readiness = _approved(versions, request.readiness_version_id, "readiness")
        if plan["id"] not in readiness["dependencies"]:
            raise ValueError("Readiness must reference the selected approved test-plan version")
    engine = script["content"]["engine"]
    if request.launch_authority == "native_schedule" and engine != "apex-load":
        raise ValueError("Native schedule ownership is supported only for APEX Load")
    workload = {key: plan["content"].get(key) for key in ("vusers", "ramp_s", "duration_s")}
    # Missing threshold numbers are explicit prerequisites, never translated from prose.
    slas = {}
    for field, sla in (("p95_ms_threshold", "p95_ms"), ("error_rate_threshold", "error_rate")):
        if plan["content"].get(field) not in (None, ""):
            slas[sla] = plan["content"][field]
    workload["slas"] = slas
    if plan["content"].get("arrival_rate") not in (None, ""):
        workload["arrival_rate"] = plan["content"]["arrival_rate"]
    return ExecutionSnapshot(
        plan_version_id=plan["id"],
        plan_title=plan["title"],
        plan_checksum=plan["checksum"],
        script_version_id=script["id"],
        script_title=script["title"],
        script_checksum=script["checksum"],
        script_source_checksum=hashlib.sha256(script["content"]["source"].encode()).hexdigest(),
        script_format=script["content"]["format"],
        readiness_version_id=readiness["id"] if readiness else None,
        readiness_checksum=readiness["checksum"] if readiness else None,
        engine=engine,
        mode=mode,
        environment_id=environment_id,
        environment_version=environment_version,
        connection_id=request.connection_id,
        connection_version=connection_version,
        launch_authority=request.launch_authority,
        workload=workload,
    )


def execution_checks(
    snapshot: ExecutionSnapshot,
    versions: list[dict[str, Any]],
    *,
    target_version: int | None,
    target_valid: bool,
    connection_valid: bool,
    connection_version: str | None,
    inventory_id: str | None,
    inventory_at: datetime | None,
    now: datetime | None = None,
) -> list[PreflightCheck]:
    now = now or datetime.now(UTC)
    checks: list[PreflightCheck] = []

    def check(key: str, label: str, valid: bool, good: str, bad: str) -> None:
        checks.append(
            PreflightCheck(
                key=key,
                label=label,
                status="ready" if valid else "blocked",
                detail=good if valid else bad,
            )
        )

    for kind in ("plan", "script", "readiness"):
        version_id = getattr(snapshot, f"{kind}_version_id")
        version = next((v for v in versions if v["id"] == version_id), None)
        valid = bool(
            version
            and approval_current(versions, version_id)
            and version["checksum"] == getattr(snapshot, f"{kind}_checksum")
            and version_digest(version["content"]) == version["checksum"]
        )
        check(
            kind,
            f"{kind.capitalize()} approval",
            valid,
            "Exact approved content is still current.",
            f"Select a current approved {kind} and prepare a new execution binding.",
        )
    check(
        "target",
        "Approved environment target",
        target_valid and target_version == snapshot.environment_version,
        "The approved target version matches this binding.",
        "Approve the environment target or prepare a new binding after its configuration changes.",
    )
    check(
        "connection",
        "Execution connection configuration",
        snapshot.mode == "simulation"
        or (connection_valid and connection_version == snapshot.connection_version),
        "Simulation selected; no live connection will be used."
        if snapshot.mode == "simulation"
        else "Connection is enabled, project-scoped and matches the recorded runtime version.",
        "Select an enabled connection for this project and engine, then prepare a new binding.",
    )
    readiness = next((v for v in versions if v["id"] == snapshot.readiness_version_id), None)
    content = readiness["content"] if readiness else {}
    inventory_fresh = inventory_at is not None and timedelta(0) <= now - inventory_at <= timedelta(
        hours=24
    )
    check(
        "readiness_evidence",
        "Environment readiness evidence",
        content.get("status") == "ready"
        and content.get("inventory_snapshot_id") == inventory_id
        and inventory_id is not None
        and inventory_fresh
        and content.get("target_version") == target_version,
        (
            "Approved readiness references the latest inventory scan, under "
            "24 hours old, and target version."
        ),
        (
            "Rescan the environment and approve readiness with the latest "
            "snapshot ID, target version and Ready assessment."
        ),
    )
    workload = dict(snapshot.workload)
    workload.pop("arrival_rate", None)
    try:
        LoadTestSpec.model_validate({"title": "Prepared execution", **workload}, strict=True)
        valid_workload = True
    except ValueError:
        valid_workload = False
    check(
        "workload",
        "Engine workload limits",
        valid_workload,
        "Concurrency, ramp and duration fit the current execution contract.",
        (
            "Revise the plan: execution supports 1–10,000 integer concurrent "
            "users and at most 24 hours ramp/duration."
        ),
    )
    check(
        "thresholds",
        "Machine-readable thresholds",
        bool(snapshot.workload.get("slas")),
        "Threshold values are frozen with the workload.",
        (
            "Add numeric latency or error-rate thresholds to the approved "
            "plan; prose is not execution configuration."
        ),
    )
    if "arrival_rate" in snapshot.workload:
        checks.append(
            PreflightCheck(
                key="arrival_rate",
                label="Arrival-rate scheduling",
                status="blocked",
                detail=(
                    "The pipeline execution contract does not support arrival-rate "
                    "scheduling. Revise the workload; arrival rate will not be silently ignored."
                ),
            )
        )
    checks.append(
        PreflightCheck(
            key="provider_access",
            label="Provider readiness and capacity",
            status="not_checked",
            detail=(
                "Provider capacity and launch bindings have not been verified. "
                "Source validation is recorded separately."
            )
            if snapshot.mode == "live"
            else (
                "This is a simulation preparation; it does not verify live "
                "provider access or capacity."
            ),
        )
    )
    check(
        "script_storage",
        "Approved source stored in APEX",
        snapshot.script_artifact_id is not None,
        "A checksum-verified source snapshot was stored when this preparation was created. "
        "Availability must be rechecked before launch.",
        "Store this approved script in the script library, then prepare a new execution.",
    )
    checks.append(
        PreflightCheck(
            key="script_binding",
            label="Immutable provider script binding",
            status="not_checked",
            detail=(
                "Approved content is frozen in APEX. Dispatch checks re-read the stored source "
                "and verify the exact provider copy before launching."
            ),
        )
    )
    checks.append(
        PreflightCheck(
            key="launch_authority",
            label="Launch ownership",
            status="blocked" if snapshot.launch_authority == "native_schedule" else "not_checked",
            detail=(
                "The native APEX Load schedule owns launching. Reconcile its "
                "runs; do not launch a duplicate pipeline execution."
            )
            if snapshot.launch_authority == "native_schedule"
            else (
                "Use this preparation's Dispatch checks to launch its exact approved source. "
                "Creating a preparation alone does not launch a pipeline."
            ),
        )
    )
    return checks
