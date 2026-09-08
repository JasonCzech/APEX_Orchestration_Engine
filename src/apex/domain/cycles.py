"""Version-bound engineering deliverables. Pure transitions shared by every channel."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apex.domain.deliverables import content_issues
from apex.domain.diagnostics import contains_credential_material
from apex.domain.input_limits import NoNulStr, ScopeId, validate_json_object

Kind = Literal["scope", "test_plan", "readiness", "script", "report", "postmortem"]


class CycleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: NoNulStr = Field(min_length=1, max_length=255)
    project_id: ScopeId
    app_id: ScopeId
    environment_id: ScopeId
    mode: Literal["live", "simulation"] = "live"

    @model_validator(mode="after")
    def safe(self) -> "CycleCreate":
        if contains_credential_material(self.model_dump()):
            raise ValueError("cycle metadata must not contain credentials")
        return self


class VersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=1)
    kind: Kind
    title: NoNulStr = Field(min_length=1, max_length=255)
    content: dict[str, Any]
    # Exact upstream version ids; snapshots are immutable and never auto-refreshed.
    dependencies: list[NoNulStr] = Field(default_factory=list, max_length=100)
    replaces: NoNulStr | None = Field(default=None, max_length=32)
    change_summary: NoNulStr = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def bounded(self) -> "VersionCreate":
        validate_json_object(self.content, label="deliverable", max_bytes=150_000, max_nodes=4000)
        if contains_credential_material(self.model_dump()):
            raise ValueError("deliverables must not contain credentials")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependency versions must be unique")
        return self


class CycleAction(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=1)
    action: Literal["submit", "approve", "request_revision", "archive", "restore"]
    version_id: NoNulStr = Field(min_length=1, max_length=32)
    note: NoNulStr = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def safe(self) -> "CycleAction":
        if contains_credential_material(self.note):
            raise ValueError("review note must not contain credentials")
        return self


def version_digest(content: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def content_integrity(version: dict[str, Any]) -> bool:
    try:
        return version_digest(version["content"]) == version["checksum"]
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def add_version(
    versions: list[dict[str, Any]],
    request: VersionCreate,
    actor: str,
    *,
    trusted_evidence: bool = False,
) -> dict[str, Any]:
    by_id = {v["id"]: v for v in versions}
    if request.replaces:
        previous = by_id.get(request.replaces)
        if previous is None or previous["kind"] != request.kind:
            raise ValueError("replacement must refer to a version of the same deliverable kind")
        if not content_integrity(previous):
            raise ValueError("Previous version content does not match its recorded checksum")
        if any(v.get("replaces") == request.replaces for v in versions):
            raise ValueError("version has already been superseded; refresh before editing")
    if not trusted_evidence:
        previous = by_id.get(request.replaces) if request.replaces else None
        for key, kind in (("_execution_evidence", "report"), ("_source_evidence", "scope")):
            frozen = previous["content"].get(key) if previous else None
            supplied = request.content.get(key)
            if (
                (key in request.content and supplied is None)
                or supplied != frozen
                or (supplied is not None and request.kind != kind)
            ):
                raise ValueError("Frozen evidence can only be captured by its evidence operation")
    for dependency in request.dependencies:
        upstream = by_id.get(dependency)
        if upstream is None or not approval_current(versions, dependency):
            raise ValueError("upstream versions must be approved and active")
        if any(v.get("replaces") == dependency for v in versions):
            raise ValueError("upstream version has been superseded")
    return {
        "id": uuid4().hex,
        "kind": request.kind,
        "title": request.title,
        "content": json.loads(json.dumps(request.content)),
        "checksum": version_digest(request.content),
        "dependencies": list(request.dependencies),
        "replaces": request.replaces,
        "change_summary": request.change_summary,
        "status": "draft",
        "created_by": actor,
        "created_at": datetime.now(UTC).isoformat(),
        "archived": False,
        "reviews": [],
    }


def transition(
    versions: list[dict[str, Any]], action: CycleAction, actor: str, *, independent_review: bool
) -> list[dict[str, Any]]:
    # Copy-on-write protects immutable content and callers' historical projections.
    result = json.loads(json.dumps(versions))
    target = next((v for v in result if v["id"] == action.version_id), None)
    if target is None:
        raise ValueError("deliverable version not found")
    if action.action in {"archive", "restore"}:
        target["archived"] = action.action == "archive"
    else:
        if target.get("archived") or any(v.get("replaces") == target["id"] for v in result):
            raise ValueError("archived or superseded versions cannot be reviewed")
        expected = "draft" if action.action == "submit" else "in_review"
        if target["status"] != expected:
            raise ValueError(f"action requires {expected} status")
        if action.action in {"submit", "approve"}:
            if not content_integrity(target):
                raise ValueError("Version content does not match its recorded checksum")
            issues = content_issues(target["kind"], target["content"])
            if issues:
                raise ValueError("Complete the deliverable before review: " + "; ".join(issues))
        if action.action == "approve":
            if independent_review and actor == target["created_by"]:
                raise ValueError("project policy requires a distinct authenticated reviewer")
            for dep in target["dependencies"]:
                upstream = next((v for v in result if v["id"] == dep), None)
                if (
                    upstream is None
                    or not approval_current(result, dep)
                    or upstream.get("archived")
                    or any(v.get("replaces") == dep for v in result)
                ):
                    raise ValueError("upstream approval is stale; create a revised draft")
        target["status"] = {
            "submit": "in_review",
            "approve": "approved",
            "request_revision": "draft",
        }[action.action]
    target["reviews"].append(
        {
            "action": action.action,
            "actor": actor,
            "note": action.note,
            "at": datetime.now(UTC).isoformat(),
        }
    )
    return result


def approval_current(versions: list[dict[str, Any]], version_id: str) -> bool:
    by_id = {v["id"]: v for v in versions}
    superseded = {v.get("replaces") for v in versions}
    memo: dict[str, bool] = {}

    def current(key: str) -> bool:
        if key in memo:
            return memo[key]
        # Provisional False also prevents malformed historical dependency cycles.
        memo[key] = False
        target = by_id.get(key)
        if (
            target is None
            or target["status"] != "approved"
            or target.get("archived")
            or key in superseded
            or not content_integrity(target)
            or bool(content_issues(target["kind"], target["content"]))
        ):
            return False
        memo[key] = all(current(dep) for dep in target["dependencies"])
        return memo[key]

    return current(version_id)


def cycle_blockers(versions: list[dict[str, Any]]) -> list[str]:
    active = [
        v
        for v in versions
        if not v.get("archived") and not any(other.get("replaces") == v["id"] for other in versions)
    ]
    blockers = []
    for kind in ("scope", "test_plan", "readiness", "script"):
        if not any(v["kind"] == kind and approval_current(versions, v["id"]) for v in active):
            blockers.append(f"Approve an active {kind.replace('_', ' ')} version")
    # This endpoint does not perform provider preflight or launch engines.
    blockers.append(
        "Provider access, script binding, capacity and readiness freshness "
        "require execution preflight"
    )
    return blockers
