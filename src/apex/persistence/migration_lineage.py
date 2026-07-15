"""Trusted Alembic revision lineage shared by migrations and startup readiness."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

from alembic.script import ScriptDirectory

LINEAGE_TABLE = "apex.alembic_revision_lineage"
BASE_REVISION_SENTINEL = ""

CREATE_LINEAGE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {LINEAGE_TABLE} (
    revision_num VARCHAR(255) NOT NULL,
    parent_revision_num VARCHAR(255) NOT NULL,
    CONSTRAINT pk_alembic_revision_lineage
        PRIMARY KEY (revision_num, parent_revision_num),
    CONSTRAINT ck_alembic_revision_lineage_no_self_parent
        CHECK (revision_num <> parent_revision_num)
)
""".strip()

INSERT_LINEAGE_SQL = f"""
INSERT INTO {LINEAGE_TABLE} (revision_num, parent_revision_num)
VALUES (:revision_num, :parent_revision_num)
ON CONFLICT (revision_num, parent_revision_num) DO NOTHING
""".strip()


def packaged_revision_lineage(
    scripts: ScriptDirectory,
) -> frozenset[tuple[str, str]]:
    """Return every packaged revision edge, including a sentinel for each base."""

    edges: set[tuple[str, str]] = set()
    for script in scripts.walk_revisions():
        revision = str(script.revision)
        raw_parents = script.down_revision
        if raw_parents is None:
            parents: tuple[str, ...] = (BASE_REVISION_SENTINEL,)
        elif isinstance(raw_parents, str):
            parents = (raw_parents,)
        else:
            parents = tuple(str(parent) for parent in raw_parents)
        edges.update((revision, parent) for parent in parents)
    return frozenset(edges)


def revision_graph(
    edges: Iterable[tuple[str, str]],
) -> dict[str, frozenset[str]]:
    """Normalize stored edges into revision -> real parent revisions."""

    parents_by_revision: defaultdict[str, set[str]] = defaultdict(set)
    for revision, parent in edges:
        parents_by_revision[str(revision)].add(str(parent))
    return {
        revision: frozenset(parent for parent in parents if parent != BASE_REVISION_SENTINEL)
        for revision, parents in parents_by_revision.items()
    }


def database_heads_descend_from_packaged_heads(
    *,
    current_heads: frozenset[str],
    packaged_heads: frozenset[str],
    database_graph: Mapping[str, frozenset[str]],
    packaged_graph: Mapping[str, frozenset[str]],
) -> bool:
    """Prove that the database is exact or strictly ahead on trusted branches.

    Known packaged edges must be immutable in the database. Every database head
    must descend from a packaged head, and every packaged head must still be
    represented by a database head. Traversal also rejects missing parents and
    cycles instead of treating incomplete provenance as compatible.
    """

    if not current_heads or not packaged_heads:
        return False
    if any(database_graph.get(revision) != parents for revision, parents in packaged_graph.items()):
        return False

    ancestors_by_head: dict[str, frozenset[str]] = {}
    try:
        for head in current_heads:
            ancestors_by_head[head] = _ancestor_closure(head, database_graph)
    except ValueError:
        return False

    # Alembic's version table contains only current heads. An ancestor and its
    # descendant appearing together is a malformed/tampered state, even though
    # both individually trace back to the packaged schema. Do not mistake that
    # redundant set for a valid rollback-compatible database.
    if any(
        other != head and other in ancestors
        for head, ancestors in ancestors_by_head.items()
        for other in current_heads
    ):
        return False

    return all(
        any(expected in ancestors_by_head[current] for expected in packaged_heads)
        for current in current_heads
    ) and all(
        any(expected in ancestors for ancestors in ancestors_by_head.values())
        for expected in packaged_heads
    )


def _ancestor_closure(
    head: str,
    graph: Mapping[str, frozenset[str]],
) -> frozenset[str]:
    if head not in graph:
        raise ValueError("unregistered schema revision")

    visited: set[str] = set()
    active: set[str] = set()

    def visit(revision: str) -> None:
        if revision in active:
            raise ValueError("cyclic schema revision lineage")
        if revision in visited:
            return
        if revision not in graph:
            raise ValueError("incomplete schema revision lineage")
        active.add(revision)
        for parent in graph[revision]:
            visit(parent)
        active.remove(revision)
        visited.add(revision)

    visit(head)
    return frozenset(visited)
