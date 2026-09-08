"""Tracked actions and conservative next-cycle draft reuse."""

import copy
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apex.domain.cycles import content_integrity, version_digest
from apex.domain.diagnostics import contains_credential_material
from apex.domain.input_limits import NoNulStr, RecordId


class FollowupWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    idempotency_key: RecordId
    action_id: RecordId | None = None
    title: NoNulStr = Field(min_length=1, max_length=500)
    owner: NoNulStr = Field(min_length=1, max_length=255)
    status: Literal["open", "in_progress", "done", "cancelled"] = "open"
    note: NoNulStr = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def safe(self):
        if contains_credential_material(self.model_dump()):
            raise ValueError("Action records must not contain credentials")
        return self


class NextCycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    idempotency_key: RecordId
    name: NoNulStr = Field(min_length=1, max_length=255)
    version_ids: list[RecordId] = Field(default_factory=list, max_length=50)
    carry_unresolved_actions: bool = True

    @model_validator(mode="after")
    def safe(self):
        if contains_credential_material(self.model_dump()) or len(set(self.version_ids)) != len(
            self.version_ids
        ):
            raise ValueError("Use unique version IDs and credential-free metadata")
        return self


def write_followup(actions: list[dict[str, Any]], body: FollowupWrite, actor: str):
    fingerprint = version_digest(body.model_dump(exclude={"expected_revision", "idempotency_key"}))
    for action in actions:
        for event in action["history"]:
            if event["request_key"] == body.idempotency_key:
                if event["fingerprint"] != fingerprint:
                    raise ValueError("Operation key was already used for a different action change")
                return actions, True
    result = copy.deepcopy(actions)
    action = next((a for a in result if a["id"] == body.action_id), None)
    if body.action_id and action is None:
        raise ValueError("Action not found")
    now = datetime.now(UTC).isoformat()
    if action is None:
        if len(result) >= 200:
            raise ValueError("Action limit reached")
        action = {"id": uuid4().hex, "created_by": actor, "created_at": now, "history": []}
        result.append(action)
    if len(action["history"]) >= 100:
        raise ValueError("Action history limit reached")
    changes = {key: getattr(body, key) for key in ("title", "owner", "status", "note")}
    action.update(changes, updated_at=now)
    action["history"].append(
        {
            **changes,
            "actor": actor,
            "at": now,
            "request_key": body.idempotency_key,
            "fingerprint": fingerprint,
        }
    )
    return result, False


def reuse_drafts(
    versions: list[dict[str, Any]], selected: list[str], actor: str, source_cycle: str
):
    by_id = {v["id"]: v for v in versions}
    superseded = {v.get("replaces") for v in versions}
    mapping = {key: uuid4().hex for key in selected}
    drafts = []
    for key in selected:
        source = by_id.get(key)
        if (
            source is None
            or source["kind"] not in {"scope", "test_plan", "script"}
            or source["archived"]
            or key in superseded
            or not content_integrity(source)
        ):
            raise ValueError("Select active, intact scope, plan and script versions only")
        if not set(source["dependencies"]).issubset(mapping):
            raise ValueError("Include every upstream version required by the selected deliverables")
        draft = copy.deepcopy(source)
        draft.update(
            id=mapping[key],
            status="draft",
            reviews=[],
            archived=False,
            replaces=None,
            dependencies=[mapping[dep] for dep in source["dependencies"]],
            created_by=actor,
            created_at=datetime.now(UTC).isoformat(),
            origin={"cycle_id": source_cycle, "version_id": key, "checksum": source["checksum"]},
            change_summary="Reused as a draft; recheck source freshness, target and approvals.",
        )
        if draft["kind"] == "script":
            draft["content"]["validation"] = "Reused source requires fresh provider validation."
            draft["checksum"] = version_digest(draft["content"])
        drafts.append(draft)
    return drafts


def reuse_actions(actions: list[dict[str, Any]], source_cycle: str, actor: str):
    result = []
    for old in actions:
        if old["status"] in {"done", "cancelled"}:
            continue
        body = FollowupWrite(
            expected_revision=1,
            idempotency_key=uuid4().hex,
            title=old["title"],
            owner=old["owner"],
            status="open",
            note="Carried forward; reassess before beginning work.",
        )
        result, _ = write_followup(result, body, actor)
        result[-1]["origin"] = {"cycle_id": source_cycle, "action_id": old["id"]}
    return result
