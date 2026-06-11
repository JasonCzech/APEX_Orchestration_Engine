import pytest

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef


def make_identity(role: Role, scopes: list[ScopeRef] | None = None) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1",
        name="test",
        consumer_type=ConsumerType.HEADLESS,
        role=role,
        scopes=scopes or [],
    )


@pytest.mark.parametrize(
    ("role", "minimum", "expected"),
    [
        (Role.VIEWER, Role.VIEWER, True),
        (Role.VIEWER, Role.OPERATOR, False),
        (Role.VIEWER, Role.ADMIN, False),
        (Role.OPERATOR, Role.VIEWER, True),
        (Role.OPERATOR, Role.OPERATOR, True),
        (Role.OPERATOR, Role.ADMIN, False),
        (Role.ADMIN, Role.VIEWER, True),
        (Role.ADMIN, Role.OPERATOR, True),
        (Role.ADMIN, Role.ADMIN, True),
    ],
)
def test_role_ordering(role: Role, minimum: Role, expected: bool) -> None:
    assert role.at_least(minimum) is expected


def test_unscoped_requires_admin_and_empty_scopes() -> None:
    assert make_identity(Role.ADMIN).is_unscoped
    assert not make_identity(Role.ADMIN, [ScopeRef(project_id="p1")]).is_unscoped
    assert not make_identity(Role.VIEWER).is_unscoped
    assert not make_identity(Role.OPERATOR).is_unscoped


def test_scoped_project_ids_distinct_ordered() -> None:
    identity = make_identity(
        Role.OPERATOR,
        [
            ScopeRef(project_id="p2", app_id="a1"),
            ScopeRef(project_id="p1"),
            ScopeRef(project_id="p2", app_id="a2"),
        ],
    )
    assert identity.scoped_project_ids() == ("p2", "p1")


def test_allows_project() -> None:
    scoped = make_identity(Role.OPERATOR, [ScopeRef(project_id="p1")])
    assert scoped.allows_project("p1")
    assert not scoped.allows_project("p2")
    assert make_identity(Role.ADMIN).allows_project("anything")
    # scoped admin is NOT unscoped
    scoped_admin = make_identity(Role.ADMIN, [ScopeRef(project_id="p1")])
    assert not scoped_admin.allows_project("p2")


def test_scope_ref_app_id_optional() -> None:
    assert ScopeRef(project_id="p1").app_id is None
    assert ScopeRef(project_id="p1", app_id="a1").app_id == "a1"
