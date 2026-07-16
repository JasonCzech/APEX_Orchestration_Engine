"""Credential-safe normalization for work-tracking provider responses."""

from __future__ import annotations

import re
from typing import Any, cast
from urllib.parse import urlsplit

from apex.domain.diagnostics import bounded_diagnostic, contains_credential_material
from apex.domain.durable_evidence import sanitize_durable_text
from apex.domain.input_limits import MAX_DESCRIPTION_CHARS
from apex.domain.integrations import Page, TranslatedQuery, WorkItem, WorkItemPage

_WORK_ITEM_KEY = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}\Z")
_INVALID_WORK_ITEM = "work-tracking adapter returned an invalid work item"
_INVALID_WORK_ITEM_PAGE = "work-tracking adapter returned an invalid work-item page"


def validated_provider_work_item(value: Any) -> WorkItem:
    """Return a bounded provider item safe for APIs, checkpoints, and JSONB.

    Provider keys and URLs remain executable identities, so credential-shaped or
    path-delimiting values fail closed. Human-facing fields are redacted before
    they can be displayed or persisted.
    """

    item: WorkItem | None = None
    try:
        item = _validated_provider_work_item(value)
    except Exception:
        pass
    if item is None:
        # Raise after leaving the handler. ``from None`` hides a cause when
        # rendered, but still retains the provider payload in ``__context__``.
        raise RuntimeError(_INVALID_WORK_ITEM)
    return item


def _validated_provider_work_item(value: Any) -> WorkItem:
    if type(value) is WorkItem:
        if value.__pydantic_extra__:
            raise ValueError("unexpected work-item fields")
        source: object = value.__dict__
    else:
        source = value
    if type(source) is not dict:
        raise ValueError("work item is not an object")
    raw = _bounded_mapping(
        source,
        allowed={"description", "key", "kind", "status", "title", "url"},
        required={"description", "key", "kind", "status", "title", "url"},
    )
    key = raw["key"]
    title = raw["title"]
    kind = raw["kind"]
    status = raw["status"]
    description = raw["description"]
    url = raw["url"]
    if (
        type(key) is not str
        or len(key) > 255
        or type(title) is not str
        or len(title) > 500
        or type(kind) is not str
        or len(kind) > 64
        or type(status) is not str
        or len(status) > 255
        or type(description) is not str
        or len(description) > MAX_DESCRIPTION_CHARS
        or (url is not None and (type(url) is not str or len(url) > 4_096))
    ):
        raise ValueError("unbounded work-item fields")
    # Descriptive fields can be safely redacted/repaired before strict model
    # validation. Identity, category, and URL fields remain fail-closed.
    safe_title = sanitize_durable_text(title, 500)
    safe_description = sanitize_durable_text(description, MAX_DESCRIPTION_CHARS)
    if safe_title is None or safe_description is None:
        raise ValueError("missing work-item text")
    item = WorkItem(
        key=key,
        title=safe_title,
        kind=kind,
        status=status,
        description=safe_description,
        url=url,
    )
    if _WORK_ITEM_KEY.fullmatch(item.key) is None or not _credential_free(item.key):
        raise ValueError("unsafe work-item key")
    if not _credential_free(item.kind) or not _credential_free(item.status):
        raise ValueError("unsafe work-item category")
    if item.url is not None:
        _validate_work_item_url(item.url)
    return item


def validated_provider_work_item_page(
    value: Any,
    *,
    requested_page: Page,
) -> WorkItemPage:
    """Revalidate every item in a provider page, including model_construct values."""

    page: WorkItemPage | None = None
    try:
        page = _validated_provider_work_item_page(value, requested_page=requested_page)
    except Exception:
        pass
    if page is None:
        raise RuntimeError(_INVALID_WORK_ITEM_PAGE)
    return page


def _validated_provider_work_item_page(value: Any, *, requested_page: Page) -> WorkItemPage:
    if type(value) is not WorkItemPage or value.__pydantic_extra__:
        raise ValueError("unexpected work-item page type")
    raw = _bounded_mapping(
        cast(dict[Any, Any], value.__dict__),
        allowed={"items", "page", "total"},
        required={"items", "page", "total"},
    )
    raw_items = raw["items"]
    raw_page = raw["page"]
    total = raw["total"]
    if (
        type(raw_items) is not list
        or len(raw_items) > requested_page.limit
        or type(total) is not int
        or not 0 <= total <= 9_223_372_036_854_775_807
        or type(raw_page) is not Page
        or raw_page.__pydantic_extra__
    ):
        raise ValueError("provider page exceeded the requested window")
    provider_page = _bounded_mapping(
        cast(dict[Any, Any], raw_page.__dict__),
        allowed={"limit", "offset"},
        required={"limit", "offset"},
    )
    if (
        type(provider_page["offset"]) is not int
        or type(provider_page["limit"]) is not int
        or provider_page["offset"] != requested_page.offset
        or provider_page["limit"] != requested_page.limit
    ):
        raise ValueError("provider returned different page metadata")
    items = [validated_provider_work_item(item) for item in raw_items]
    return WorkItemPage(
        items=items,
        total=total,
        page=requested_page.model_copy(deep=True),
    )


def validated_provider_query(value: Any, *, expected_provider: str) -> TranslatedQuery:
    """Validate executable provider query output before response or later replay."""

    query: TranslatedQuery | None = None
    try:
        query = _validated_provider_query(value, expected_provider=expected_provider)
    except Exception:
        pass
    if query is None:
        raise RuntimeError("work-tracking adapter returned an invalid query")
    return query


def _validated_provider_query(value: Any, *, expected_provider: str) -> TranslatedQuery:
    if type(value) is not TranslatedQuery or value.__pydantic_extra__:
        raise ValueError("wrong provider query type")
    raw = _bounded_mapping(
        cast(dict[Any, Any], value.__dict__),
        allowed={"confidence", "provider", "query"},
        required={"confidence", "provider", "query"},
    )
    if (
        type(raw["provider"]) is not str
        or not 1 <= len(raw["provider"]) <= 64
        or type(raw["query"]) is not str
        or not 1 <= len(raw["query"]) <= 20_000
        or type(raw["confidence"]) not in {float, int}
    ):
        raise ValueError("unbounded provider query")
    query = TranslatedQuery.model_validate(raw)
    if query.provider.casefold() != expected_provider.casefold():
        raise ValueError("inconsistent provider")
    if contains_credential_material(query.model_dump(mode="json")):
        raise ValueError("credential-bearing provider query")
    return query.model_copy(deep=True)


def _credential_free(value: str) -> bool:
    return bounded_diagnostic(value, max_chars=max(1, len(value))) == value


def _bounded_mapping(
    value: dict[Any, Any],
    *,
    allowed: set[str],
    required: set[str],
) -> dict[str, object]:
    """Extract a tiny schema while reading at most one key beyond its bound."""

    keys: list[str] = []
    iterator = iter(value)
    for _ in range(len(allowed) + 1):
        try:
            key = next(iterator)
        except StopIteration:
            break
        if type(key) is not str:
            raise ValueError("provider field names must be strings")
        keys.append(key)
    key_set = set(keys)
    if len(keys) > len(allowed) or not required <= key_set or not key_set <= allowed:
        raise ValueError("provider fields do not match the expected schema")
    return {key: value[key] for key in keys}


def _validate_work_item_url(value: str) -> None:
    if value != value.strip() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise ValueError("unsafe work-item URL")
    parsed = urlsplit(value)
    invalid_port = False
    try:
        port = parsed.port
    except ValueError:
        invalid_port = True
        port = None
    if invalid_port:
        raise ValueError("unsafe work-item URL")
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
        or not _credential_free(value)
    ):
        raise ValueError("unsafe work-item URL")
