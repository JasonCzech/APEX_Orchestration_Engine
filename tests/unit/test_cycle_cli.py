import json
import sys

import httpx
import respx

from apex.cli import main


@respx.mock
def test_dispatch_preserves_operation_identity_and_reports_uncertainty(monkeypatch, capsys):
    monkeypatch.setenv("APEX_API_KEY", "cli-test-key")
    monkeypatch.setenv("APEX_URL", "https://apex.example")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apex",
            "dispatch-execution",
            "cycle",
            "prep",
            "--revision",
            "7",
            "--idempotency-key",
            "original-key",
        ],
    )
    route = respx.post("https://apex.example/v1/cycles/cycle/executions/prep/dispatch").mock(
        return_value=httpx.Response(
            200,
            json={
                "revision": 8,
                "checks": [],
                "attempt": {
                    "id": "dispatch",
                    "status": "uncertain",
                    "request_key": "original-key",
                    "detail": "Launch receipt unconfirmed",
                },
            },
        )
    )
    assert main() == 0
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == {
        "expected_revision": 7,
        "idempotency_key": "original-key",
    }
    output = capsys.readouterr().out
    assert "uncertain" in output and "original-key" in output
    assert "no test launched" not in output


@respx.mock
def test_dispatch_inspection_is_read_only_and_keeps_blockers(monkeypatch, capsys):
    monkeypatch.setenv("APEX_API_KEY", "cli-test-key")
    monkeypatch.setenv("APEX_URL", "https://apex.example")
    monkeypatch.setattr(sys, "argv", ["apex", "execution-dispatch", "cycle", "prep"])
    route = respx.get("https://apex.example/v1/cycles/cycle/executions/prep/dispatch").mock(
        return_value=httpx.Response(
            200,
            json={
                "revision": 7,
                "checks": [
                    {"status": "blocked", "label": "Source", "detail": "Review the changed source"}
                ],
                "attempt": None,
            },
        )
    )
    assert main() == 0
    assert route.call_count == 1
    output = capsys.readouterr().out
    assert "No execution dispatched" in output and "Review the changed source" in output


@respx.mock
def test_review_sends_exact_version_and_revision(monkeypatch, capsys):
    monkeypatch.setenv("APEX_API_KEY", "cli-test-key")
    monkeypatch.setenv("APEX_URL", "https://apex.example")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apex",
            "--json",
            "review",
            "cycle1",
            "--version",
            "version1",
            "--revision",
            "7",
            "--action",
            "approve",
        ],
    )
    route = respx.post("https://apex.example/v1/cycles/cycle1/actions").mock(
        return_value=httpx.Response(200, json={"id": "cycle1", "revision": 8})
    )
    assert main() == 0
    assert json.loads(route.calls.last.request.content) == {
        "version_id": "version1",
        "expected_revision": 7,
        "action": "approve",
        "note": "",
    }
    assert json.loads(capsys.readouterr().out)["revision"] == 8


@respx.mock
def test_conflicts_are_not_retried_and_do_not_reflect_provider_body(monkeypatch, capsys):
    monkeypatch.setenv("APEX_API_KEY", "secret-key")
    monkeypatch.setenv("APEX_URL", "https://apex.example")
    monkeypatch.setattr(sys, "argv", ["apex", "status", "cycle1"])
    route = respx.get("https://apex.example/v1/cycles/cycle1").mock(
        return_value=httpx.Response(409, text="secret-key")
    )
    assert main() == 4
    assert route.call_count == 1
    assert "secret-key" not in capsys.readouterr().err


def test_remote_http_rejected_before_sending_identity(monkeypatch, capsys):
    monkeypatch.setenv("APEX_URL", "http://apex.example")
    monkeypatch.setenv("APEX_API_KEY", "secret-key")
    monkeypatch.setattr(sys, "argv", ["apex", "cycles"])
    assert main() == 2
    assert "HTTPS" in capsys.readouterr().err


@respx.mock
def test_preparation_uses_operator_api_and_preserves_idempotency(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("APEX_API_KEY", "cli-test-key")
    monkeypatch.setenv("APEX_URL", "https://apex.example")
    body = {
        "expected_revision": 3,
        "idempotency_key": "stable-request",
        "plan_version_id": "plan",
        "script_version_id": "script",
    }
    document = tmp_path / "preparation.json"
    document.write_text(json.dumps(body))
    monkeypatch.setattr(sys, "argv", ["apex", "prepare-execution", "cycle1", str(document)])
    route = respx.post("https://apex.example/v1/cycles/cycle1/executions").mock(
        return_value=httpx.Response(200, json={"id": "preparation", "revision": 4})
    )
    assert main() == 0
    assert json.loads(route.calls.last.request.content)["idempotency_key"] == "stable-request"
    assert "no test launched" in capsys.readouterr().out


@respx.mock
def test_store_script_binds_version_and_connection(monkeypatch, capsys):
    monkeypatch.setenv("APEX_API_KEY", "cli-test-key")
    monkeypatch.setenv("APEX_URL", "https://apex.example")
    monkeypatch.setattr(
        sys,
        "argv",
        ["apex", "store-script", "cycle", "version", "--connection", "store", "--revision", "4"],
    )
    route = respx.post("https://apex.example/v1/cycles/cycle/scripts/version/artifact").mock(
        return_value=httpx.Response(200, json={"source_checksum": "checksum", "size_bytes": 12})
    )
    assert main() == 0
    assert json.loads(route.calls.last.request.content) == {
        "expected_revision": 4,
        "connection_id": "store",
    }
    assert "no test launched" in capsys.readouterr().out


@respx.mock
def test_provider_validation_cli_preserves_operation_key(monkeypatch, capsys):
    monkeypatch.setenv("APEX_API_KEY", "cli-test-key")
    monkeypatch.setenv("APEX_URL", "https://apex.example")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apex",
            "--json",
            "validate-script",
            "cycle",
            "prep",
            "--revision",
            "4",
            "--idempotency-key",
            "stable",
        ],
    )
    route = respx.post(
        "https://apex.example/v1/cycles/cycle/executions/prep/script-validations"
    ).mock(
        return_value=httpx.Response(
            200, json={"id": "operation", "status": "rejected", "current": False, "issues": []}
        )
    )
    assert main() == 6
    assert json.loads(route.calls.last.request.content) == {
        "expected_revision": 4,
        "idempotency_key": "stable",
    }
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"


@respx.mock
def test_provider_library_preview_uses_encoded_query(monkeypatch, capsys):
    monkeypatch.setenv("APEX_API_KEY", "cli-test-key")
    monkeypatch.setenv("APEX_URL", "https://apex.example")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apex",
            "--json",
            "provider-scripts",
            "cycle",
            "--connection",
            "connection",
            "--script",
            "checkout & cart",
        ],
    )
    route = respx.get("https://apex.example/v1/cycles/cycle/provider-scripts").mock(
        return_value=httpx.Response(200, json={"preview": None})
    )
    assert main() == 0
    assert route.calls.last.request.url.params["remote_script_id"] == "checkout & cart"
    assert route.calls.last.request.url.params["connection_id"] == "connection"
    assert json.loads(capsys.readouterr().out) == {"preview": None}


@respx.mock
def test_cycle_results_preserve_explicit_baseline(monkeypatch, capsys):
    monkeypatch.setenv("APEX_API_KEY", "cli-test-key")
    monkeypatch.setenv("APEX_URL", "https://apex.example")
    monkeypatch.setattr(
        sys, "argv", ["apex", "--json", "results", "cycle", "--baseline", "baseline"]
    )
    route = respx.get("https://apex.example/v1/cycles/cycle/results").mock(
        return_value=httpx.Response(200, json={"baseline_id": "baseline", "results": []})
    )
    assert main() == 0
    assert route.calls.last.request.url.params["baseline_id"] == "baseline"
    assert json.loads(capsys.readouterr().out)["baseline_id"] == "baseline"


@respx.mock
def test_freeze_report_cli_sends_preview_identity(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("APEX_API_KEY", "cli-test-key")
    monkeypatch.setenv("APEX_URL", "https://apex.example")
    document = tmp_path / "evidence.json"
    body = {
        "expected_revision": 4,
        "idempotency_key": "capture",
        "expected_results": {"run": "a" * 64},
    }
    document.write_text(json.dumps(body))
    monkeypatch.setattr(
        sys, "argv", ["apex", "--json", "freeze-report-evidence", "cycle", "report", str(document)]
    )
    route = respx.post("https://apex.example/v1/cycles/cycle/reports/report/evidence").mock(
        return_value=httpx.Response(200, json={"revision": 5})
    )
    assert main() == 0
    assert json.loads(route.calls.last.request.content) == body
    assert json.loads(capsys.readouterr().out)["revision"] == 5


@respx.mock
def test_next_cycle_cli_keeps_explicit_operation_identity(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("APEX_API_KEY", "cli-test-key")
    monkeypatch.setenv("APEX_URL", "https://apex.example")
    document = tmp_path / "next.json"
    document.write_text(
        json.dumps(
            {"expected_revision": 4, "idempotency_key": "next", "name": "Next", "version_ids": []}
        )
    )
    monkeypatch.setattr(sys, "argv", ["apex", "--json", "next-cycle", "cycle", str(document)])
    route = respx.post("https://apex.example/v1/cycles/cycle/next-cycle").mock(
        return_value=httpx.Response(200, json={"id": "next"})
    )
    assert main() == 0
    assert json.loads(route.calls.last.request.content)["idempotency_key"] == "next"
    assert json.loads(capsys.readouterr().out)["id"] == "next"


@respx.mock
def test_sources_cli_preserves_page_revision(monkeypatch, capsys):
    monkeypatch.setenv("APEX_API_KEY", "cli-test-key")
    monkeypatch.setenv("APEX_URL", "https://apex.example")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apex",
            "--json",
            "sources",
            "cycle",
            "--connection",
            "docs",
            "--page",
            "123",
            "--revision",
            "7",
        ],
    )
    route = respx.get("https://apex.example/v1/cycles/cycle/sources").mock(
        return_value=httpx.Response(200, json={"document": None})
    )
    assert main() == 0
    assert dict(route.calls.last.request.url.params) == {
        "connection_id": "docs",
        "title": "",
        "page_id": "123",
        "revision": "7",
    }
    assert json.loads(capsys.readouterr().out) == {"document": None}


@respx.mock
def test_cancel_dispatch_requires_exact_intent_and_preserves_receipt(monkeypatch, capsys):
    monkeypatch.setenv("APEX_API_KEY", "cli-test-key")
    monkeypatch.setenv("APEX_URL", "https://apex.example")
    monkeypatch.setattr(
        sys,
        "argv",
        ["apex", "cancel-dispatch", "cycle", "prep", "--dispatch-id", "original-intent"],
    )
    route = respx.post("https://apex.example/v1/cycles/cycle/executions/prep/dispatch/cancel").mock(
        return_value=httpx.Response(
            200,
            json={
                "revision": 7,
                "checks": [],
                "attempt": {
                    "id": "original-intent",
                    "status": "aborted",
                    "request_key": "original-key",
                    "detail": "Unsubmitted launch intent canceled; no execution was submitted",
                },
            },
        )
    )
    assert main() == 0
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == {"dispatch_id": "original-intent"}
    output = capsys.readouterr().out
    assert "original-intent" in output and "no execution was submitted" in output
