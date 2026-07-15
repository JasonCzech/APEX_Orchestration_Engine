"""Bounds and secrecy guarantees for public request-validation errors."""

import json
from types import SimpleNamespace

from fastapi.exceptions import RequestValidationError
from starlette.requests import Request

from apex.app.errors import (
    MAX_PROBLEM_DETAIL_CHARS,
    MAX_PROBLEM_TITLE_CHARS,
    MAX_VALIDATION_ERRORS,
    MAX_VALIDATION_ERRORS_JSON_CHARS,
    MAX_VALIDATION_LOCATION_CHARS,
    MAX_VALIDATION_LOCATION_COMPONENTS,
    MAX_VALIDATION_MESSAGE_CHARS,
    MAX_VALIDATION_TYPE_CHARS,
    _safe_route_template,
    _sanitized_validation_errors,
    problem,
)


def test_unexpected_error_route_logging_never_uses_caller_path_segments() -> None:
    canary = "Bearer path-secret-canary"
    matched = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/v1/documents/{canary}",
            "headers": [],
            "query_string": b"",
            "route": SimpleNamespace(path="/v1/documents/{document_id}"),
        }
    )
    unmatched = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/v1/{canary}",
            "headers": [],
            "query_string": b"",
        }
    )

    assert _safe_route_template(matched) == "/v1/documents/{document_id}"
    assert _safe_route_template(unmatched) == "<unmatched-route>"
    assert canary not in _safe_route_template(matched)
    assert canary not in _safe_route_template(unmatched)


def test_validation_error_does_not_reflect_oversized_unknown_key_or_input() -> None:
    secret_key = "unknown-secret-" + "k" * (512 * 1024)
    rejected_secret = "rejected-body-secret"
    exc = RequestValidationError(
        [
            {
                "type": "extra_forbidden",
                "loc": ("body", secret_key),
                "msg": "Extra inputs are not permitted",
                "input": rejected_secret,
                "ctx": {"password": rejected_secret},
            }
        ]
    )

    errors = _sanitized_validation_errors(exc)
    rendered = json.dumps(errors)

    assert errors == [
        {
            "type": "extra_forbidden",
            "loc": ["body", "<unknown-field>"],
            "msg": "Extra inputs are not permitted",
        }
    ]
    assert "unknown-secret" not in rendered
    assert rejected_secret not in rendered


def test_validation_error_does_not_reflect_mapping_key_or_custom_message() -> None:
    canary = "CALLER_CONTROLLED_VALIDATION_CANARY"
    exc = RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body", "filters", canary),
                "msg": f"Value error, rejected mapping key {canary}",
                "input": "rejected-input-canary",
                "ctx": {"error": ValueError(canary)},
            }
        ]
    )

    errors = _sanitized_validation_errors(exc)
    rendered = json.dumps(errors)

    assert errors == [
        {
            "type": "value_error",
            "loc": ["body", "<field>", "<field>"],
            "msg": "Invalid request value",
        }
    ]
    assert canary not in rendered
    assert "rejected-input-canary" not in rendered


def test_validation_error_count_components_and_total_output_are_bounded() -> None:
    errors = [
        {
            "type": "type-" + "t" * 1_000,
            "loc": tuple(["body", *("l" * 1_000 for _ in range(50))]),
            "msg": "bad\x00 password=do-not-leak; " + "m" * 10_000,
            "input": {"token": "do-not-leak"},
        }
    ]
    errors.extend(
        {
            "type": "invalid",
            "loc": ("body", "field"),
            "msg": "Invalid request value",
            "input": "not-reflected",
        }
        for _ in range(19_999)
    )

    sanitized = _sanitized_validation_errors(RequestValidationError(errors))
    rendered = json.dumps(sanitized, ensure_ascii=True, separators=(",", ":"))

    assert len(sanitized) <= MAX_VALIDATION_ERRORS
    assert len(rendered) <= MAX_VALIDATION_ERRORS_JSON_CHARS
    assert all(len(error["type"]) <= MAX_VALIDATION_TYPE_CHARS for error in sanitized)
    assert all(len(error["msg"]) <= MAX_VALIDATION_MESSAGE_CHARS for error in sanitized)
    assert all(len(error["loc"]) <= MAX_VALIDATION_LOCATION_COMPONENTS for error in sanitized)
    assert all(
        len(component) <= MAX_VALIDATION_LOCATION_CHARS
        for error in sanitized
        for component in error["loc"]
        if isinstance(component, str)
    )
    assert "do-not-leak" not in rendered
    assert "\\u0000" not in rendered


def test_problem_title_and_detail_are_credential_redacted_and_bounded() -> None:
    response = problem(
        422,
        "password=title-secret " + ("x" * 100_000),
        detail='{"client_secret":"detail-secret"}' + ("y" * 100_000),
    )
    body = json.loads(bytes(response.body))

    assert "title-secret" not in body["title"]
    assert "detail-secret" not in body["detail"]
    assert "[REDACTED]" in body["title"]
    assert "[REDACTED]" in body["detail"]
    assert len(body["title"]) <= MAX_PROBLEM_TITLE_CHARS
    assert len(body["detail"]) <= MAX_PROBLEM_DETAIL_CHARS
