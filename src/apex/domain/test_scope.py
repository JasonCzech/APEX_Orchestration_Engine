"""Canonical topology of one end-to-end test (not an environment matrix)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apex.domain.input_limits import RecordId, ResourceId, ScopeId


class TestParticipant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: ResourceId
    project_id: ScopeId
    application_id: ScopeId
    environment_id: RecordId
    layer: Literal["frontend", "middle", "backend"]


class TestScopeV1(BaseModel):
    __test__ = False
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    participants: list[TestParticipant] = Field(min_length=1, max_length=32)
    primary_participant_id: ResourceId
    traffic_entry_participant_id: ResourceId | None = None

    @model_validator(mode="after")
    def validate_topology(self):
        ids = [p.id for p in self.participants]
        pairs = [(p.application_id, p.environment_id) for p in self.participants]
        if len(set(ids)) != len(ids) or len(set(pairs)) != len(pairs):
            raise ValueError("Duplicate participant or application/environment pair")
        if sum(p.layer == "frontend" for p in self.participants) > 1:
            raise ValueError("At most one frontend is allowed")
        if sum(p.layer == "backend" for p in self.participants) != 1:
            raise ValueError("Exactly one backend is required")
        if self.primary_participant_id not in ids:
            raise ValueError("Select a primary system from the test participants")
        if (
            self.traffic_entry_participant_id is not None
            and self.traffic_entry_participant_id not in ids
        ):
            raise ValueError("Traffic entry must be a test participant")
        return self

    def validate_admission(self) -> None:
        """Deployment limits apply to new setups, never historical checkpoint reads."""
        from apex.settings import get_settings

        if len(self.participants) > get_settings().runs.max_test_participants:
            raise ValueError("Test scope exceeds the deployment participant limit")

    @property
    def primary(self) -> TestParticipant:
        return next(p for p in self.participants if p.id == self.primary_participant_id)

    @property
    def traffic_entry(self) -> TestParticipant | None:
        return next(
            (p for p in self.participants if p.id == self.traffic_entry_participant_id), None
        )
