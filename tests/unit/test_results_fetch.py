"""SSRF guard for the agent's fetch_results tool (apex.services.results_fetch).

Deny-by-default: a URL is only fetchable when its host is allow-listed and not
private. These assert the guard alone (no network) — the happy host check uses
allow_private=True so it never performs DNS resolution.
"""

import pytest

from apex.services.results_fetch import FetchError, validate_fetch_url


def test_allow_listed_host_passes() -> None:
    host = validate_fetch_url(
        "https://results.example.com/run/42",
        allowed_hosts=["results.example.com"],
        allow_private=True,
    )
    assert host == "results.example.com"


def test_host_not_in_allow_list_is_rejected() -> None:
    # Rejected before any DNS resolution — the allow-list check comes first.
    with pytest.raises(FetchError, match="allow-list"):
        validate_fetch_url(
            "https://evil.example.com/x",
            allowed_hosts=["results.example.com"],
        )


def test_empty_allow_list_disables_the_tool() -> None:
    with pytest.raises(FetchError, match="disabled"):
        validate_fetch_url("https://results.example.com/x", allowed_hosts=[])


def test_private_or_loopback_ip_literal_is_rejected() -> None:
    # Even an allow-listed loopback literal is blocked unless allow_private is set.
    with pytest.raises(FetchError, match="private/loopback"):
        validate_fetch_url(
            "http://127.0.0.1/metadata",
            allowed_hosts=["127.0.0.1"],
            allow_private=False,
        )


def test_link_local_metadata_ip_is_rejected() -> None:
    with pytest.raises(FetchError, match="private/loopback"):
        validate_fetch_url(
            "http://169.254.169.254/latest/meta-data",
            allowed_hosts=["169.254.169.254"],
            allow_private=False,
        )


def test_non_http_scheme_is_rejected() -> None:
    with pytest.raises(FetchError, match="scheme"):
        validate_fetch_url("file:///etc/passwd", allowed_hosts=["results.example.com"])
