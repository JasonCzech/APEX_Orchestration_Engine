"""Consumer identity domain model shared by both API surfaces (ADR-0003).

Identity = API consumer: hashed key, type, ordered role, explicit project/app scopes.
Pure data + predicates; no IO so handlers and dependencies stay unit-testable.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from apex.domain.input_limits import MAX_CHILD_ITEMS, ScopeId


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

    model_config = ConfigDict(extra="forbid")

    project_id: ScopeId
    app_id: ScopeId | None = None


class ConsumerIdentity(BaseModel):
    consumer_id: str
    name: str
    consumer_type: ConsumerType
    role: Role
    scopes: list[ScopeRef] = Field(default_factory=list, max_length=MAX_CHILD_ITEMS)

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

    def allows_app(self, project_id: str, app_id: str | None) -> bool:
        """True when the consumer may access an app or project-level resource.

        A project-only scope (`app_id is None`) grants all apps in that project.
        An app-narrowed scope grants that specific app. For project-level resources
        with no app id, any scope in that project is sufficient to preserve the
        current project-level behavior.
        """

        if self.is_unscoped:
            return True
        if app_id is None:
            return self.allows_project(project_id)
        return any(
            scope.project_id == project_id and (scope.app_id is None or scope.app_id == app_id)
            for scope in self.scopes
        )

    def allows_scope(self, *, project_id: str, app_id: str | None = None) -> bool:
        return self.allows_app(project_id, app_id)

    def contains_scope(self, scope: ScopeRef) -> bool:
        """Whether this identity may delegate ``scope`` without widening access.

        Resource access and scope delegation intentionally differ for project-level
        targets.  An app-only identity may read project-level resources, but it may
        not mint a project-wide credential that reaches sibling apps.
        """

        if self.is_unscoped:
            return True
        if scope.app_id is None:
            return any(
                own.project_id == scope.project_id and own.app_id is None for own in self.scopes
            )
        return any(
            own.project_id == scope.project_id
            and (own.app_id is None or own.app_id == scope.app_id)
            for own in self.scopes
        )
