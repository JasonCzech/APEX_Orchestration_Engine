"""Shared natural-language → tracker-query translation for work-tracking adapters.

`parse_work_query` runs a DETERMINISTIC keyword/pattern ruleset over the user's
text and returns a provider-neutral `WorkQuerySpec`; the Jira and ADO adapters
render that spec into JQL / WIQL respectively. LLM-backed translation is a
planned upgrade — it will replace `parse_work_query` behind the exact same
WorkTrackingPort.translate_query surface, so adapters and routers won't change.

Recognized signals (each category counts once toward confidence):
- explicit project mentions ("project PHX") — context hints win over text,
  text wins over the adapter's configured default project;
- status words: open / in-progress / closed synonyms;
- type words: bug / story / task synonyms;
- "assigned to me" / "my ..." → current-user assignee;
- sprint mentions ("current sprint", "sprint 42");
- quoted phrases → full-text match terms;
- relative time windows ("today", "this week", "last week", "this month").

Confidence = 0.3 + 0.15 per matched category, clamped to [0.3, 0.9]. When no
category matches, the whole text becomes a single full-text fallback term.

This module also hosts the router dependency providers (resolver + saved-query
repository) so tests can override them, mirroring apex.services.documents.
"""

import re
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apex.domain.integrations import QueryContext
from apex.persistence.db import get_session
from apex.persistence.repositories.saved_queries import SavedQueriesRepository
from apex.services.connections import ConnectionResolver, get_connection_resolver

# Sentinel sprint value meaning "the currently active sprint/iteration".
CURRENT_SPRINT = "@current"

# canonical statuses / kinds / windows shared with both adapters
STATUS_OPEN = "open"
STATUS_IN_PROGRESS = "in_progress"
STATUS_CLOSED = "closed"

_STATUS_PATTERNS: tuple[tuple[str, str], ...] = (
    # in-progress first so "in progress" is not shadowed by other word hits
    (STATUS_IN_PROGRESS, r"\b(?:in[\s-]progress|wip|doing|started|active)\b"),
    (STATUS_CLOSED, r"\b(?:closed|done|resolved|completed|fixed|finished)\b"),
    (STATUS_OPEN, r"\b(?:open|unresolved|outstanding|todo|to\s+do|new)\b"),
)

_KIND_PATTERNS: tuple[tuple[str, str], ...] = (
    ("bug", r"\b(?:bugs?|defects?)\b"),
    ("story", r"\b(?:stor(?:y|ies)|features?)\b"),
    ("task", r"\btasks?\b"),
)

_WINDOW_PATTERNS: tuple[tuple[str, str], ...] = (
    ("last_week", r"\b(?:last|past)\s+week\b"),
    ("this_week", r"\bthis\s+week\b"),
    ("this_month", r"\bthis\s+month\b"),
    ("today", r"\btoday\b"),
)

_ASSIGNED_TO_ME = re.compile(r"\bassigned\s+to\s+me\b|\bmy\b|\bmine\b", re.IGNORECASE)
_CURRENT_SPRINT = re.compile(r"\b(?:current|this|active)\s+sprint\b", re.IGNORECASE)
_NAMED_SPRINT = re.compile(r"\bsprint\s+([\w.-]+)\b", re.IGNORECASE)
_PROJECT_MENTION = re.compile(r"\bproject\s+([A-Za-z][\w-]*)\b", re.IGNORECASE)
_DOUBLE_QUOTED = re.compile(r'"([^"]+)"')
_SINGLE_QUOTED = re.compile(r"(?:^|\s)'([^']+)'(?=\s|$|[.,;!?])")

MIN_CONFIDENCE = 0.3
MAX_CONFIDENCE = 0.9
_CONFIDENCE_STEP = 0.15


@dataclass(frozen=True)
class WorkQuerySpec:
    """Provider-neutral parse of a natural-language work-item query."""

    project: str | None = None
    statuses: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    assigned_to_me: bool = False
    sprint: str | None = None  # CURRENT_SPRINT or a captured sprint name/number
    phrases: tuple[str, ...] = ()
    created_window: str | None = None  # today | this_week | last_week | this_month
    fallback_text: str | None = None  # whole text, only when nothing matched
    rule_hits: int = field(default=0, compare=False)


def parse_work_query(
    natural_language: str,
    *,
    default_project: str | None = None,
    context: QueryContext | None = None,
) -> WorkQuerySpec:
    """Deterministically parse `natural_language` into a WorkQuerySpec.

    Project precedence: context hint "project" > explicit text mention >
    `default_project` (the adapter's configured project). Only an explicit
    text mention counts as a rule hit.
    """
    text = natural_language.strip()
    lowered = text.lower()
    hits = 0

    statuses: list[str] = []
    for canonical, pattern in _STATUS_PATTERNS:
        if re.search(pattern, lowered):
            statuses.append(canonical)
    if statuses:
        hits += 1

    kinds: list[str] = []
    for canonical, pattern in _KIND_PATTERNS:
        if re.search(pattern, lowered):
            kinds.append(canonical)
    if kinds:
        hits += 1

    assigned_to_me = bool(_ASSIGNED_TO_ME.search(text))
    if assigned_to_me:
        hits += 1

    sprint: str | None = None
    if _CURRENT_SPRINT.search(text):
        sprint = CURRENT_SPRINT
    else:
        named = _NAMED_SPRINT.search(text)
        if named:
            sprint = named.group(1)
    if sprint is not None:
        hits += 1

    phrases = tuple(_DOUBLE_QUOTED.findall(text)) + tuple(_SINGLE_QUOTED.findall(text))
    if phrases:
        hits += 1

    window: str | None = None
    for canonical, pattern in _WINDOW_PATTERNS:
        if re.search(pattern, lowered):
            window = canonical
            break
    if window is not None:
        hits += 1

    project: str | None = None
    mention = _PROJECT_MENTION.search(text)
    hinted = (context.hints.get("project") if context else None) or None
    if hinted:
        project = hinted
    elif mention:
        project = mention.group(1)
    else:
        project = default_project
    if mention or hinted:
        hits += 1

    fallback = text if hits == 0 and text else None
    return WorkQuerySpec(
        project=project,
        statuses=tuple(statuses),
        kinds=tuple(kinds),
        assigned_to_me=assigned_to_me,
        sprint=sprint,
        phrases=phrases,
        created_window=window,
        fallback_text=fallback,
        rule_hits=hits,
    )


def confidence_for(spec: WorkQuerySpec) -> float:
    """0.3 floor, +0.15 per matched rule category, 0.9 ceiling."""
    raw = MIN_CONFIDENCE + _CONFIDENCE_STEP * spec.rule_hits
    return round(min(MAX_CONFIDENCE, max(MIN_CONFIDENCE, raw)), 2)


# ── router dependency providers (override these in tests) ────────────────────


def get_work_tracking_resolver() -> ConnectionResolver:
    """Connection resolver indirection so router tests can inject a fake."""
    return get_connection_resolver()


def get_saved_queries_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SavedQueriesRepository:
    return SavedQueriesRepository(session)
