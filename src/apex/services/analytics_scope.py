"""Shared row-visibility predicate for app-aware analytics projections."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, and_, false, or_

from apex.auth.identity import ScopeRef


def analytics_scope_filter(
    model: Any,
    visible_scopes: Sequence[ScopeRef] | None,
) -> ColumnElement[bool] | None:
    """Return the exact analytics visibility predicate for ``visible_scopes``.

    ``None`` is reserved for an unscoped platform administrator and therefore
    applies no filter. Project-wide scopes include every app (and legacy rows
    whose app is unknown) in that project. App scopes match both columns, so a
    legacy ``app_id IS NULL`` row can never leak to an app-only identity.
    """
    if visible_scopes is None:
        return None

    predicates: list[ColumnElement[bool]] = []
    seen: set[tuple[str, str | None]] = set()
    for scope in visible_scopes:
        key = (scope.project_id, scope.app_id)
        if key in seen:
            continue
        seen.add(key)
        if scope.app_id is None:
            predicates.append(model.project_id == scope.project_id)
        else:
            predicates.append(
                and_(
                    model.project_id == scope.project_id,
                    model.app_id == scope.app_id,
                )
            )
    return or_(*predicates) if predicates else false()
