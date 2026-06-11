"""Consumer identity domain model shared by both API surfaces (ADR-0003).

Identity = API consumer: hashed key, type, ordered role, explicit project/app scopes.
Pure data + predicates; no IO so handlers and dependencies stay unit-testable.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class ConsumerType(StrEnum):
    DASHBOARD = "dashboard"
    HEADLESS = "headless"
    INTERNAL = "internal"


class Role(StrEnum):
    """Ordered roles: viewer < operator < admin."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]

    def at_least(self, minimum: "Role") -> bool:
        return self.rank >= minimum.rank


_ROLE_RANK: dict[Role, int] = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}


class ScopeRef(BaseModel):
    """A project (optionally narrowed to one app) the consumer may act on."""

    project_id: str
    app_id: str | None = None


class ConsumerIdentity(BaseModel):
    consumer_id: str
    name: str
    consumer_type: ConsumerType
    role: Role
    scopes: list[ScopeRef] = Field(default_factory=list)

    @property
    def is_unscoped(self) -> bool:
        """Admins with no explicit scopes may act on every project."""
        return self.role is Role.ADMIN and not self.scopes

    def scoped_project_ids(self) -> tuple[str, ...]:
        """Distinct scoped project ids, in declaration order."""
        seen: dict[str, None] = {}
        for scope in self.scopes:
            seen.setdefault(scope.project_id)
        return tuple(seen)

    def allows_project(self, project_id: str) -> bool:
        return self.is_unscoped or project_id in self.scoped_project_ids()
