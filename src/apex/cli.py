"""Operator CLI over the same authenticated cycle API as the dashboard."""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="apex", description="APEX cycle operator CLI")
    result.add_argument("--json", action="store_true", help="Stable JSON output")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("cycles", help="List accessible test cycles")
    create = commands.add_parser("create", help="Create a test cycle")
    for name in ("name", "project-id", "app-id", "environment-id"):
        create.add_argument(f"--{name}", required=True)
    create.add_argument("--mode", choices=("live", "simulation"), default="live")
    for name in ("status", "export", "executions", "scripts", "results"):
        command = commands.add_parser(name)
        command.add_argument("cycle_id")
        if name == "results":
            command.add_argument("--baseline")
    imp = commands.add_parser("import", help="Import a VersionCreate JSON document as a new draft")
    imp.add_argument("cycle_id")
    imp.add_argument("file", type=Path)
    preparation = commands.add_parser(
        "prepare-execution", help="Save execution bindings without launching"
    )
    preparation.add_argument("cycle_id")
    preparation.add_argument("file", type=Path)
    link = commands.add_parser(
        "link-run", help="Link an existing engine attempt; bindings remain unverified"
    )
    link.add_argument("cycle_id")
    link.add_argument("engine_run_id")
    link.add_argument("--revision", required=True, type=int)
    store = commands.add_parser("store-script", help="Store and verify an approved script source")
    store.add_argument("cycle_id")
    store.add_argument("version_id")
    store.add_argument("--connection", required=True)
    store.add_argument("--revision", required=True, type=int)
    validation = commands.add_parser(
        "validate-script", help="Validate a prepared source without launching"
    )
    validation.add_argument("cycle_id")
    validation.add_argument("preparation_id")
    validation.add_argument("--revision", required=True, type=int)
    validation.add_argument("--idempotency-key", required=True)
    history = commands.add_parser(
        "script-validations", help="Inspect provider source validation history"
    )
    history.add_argument("cycle_id")
    history.add_argument("preparation_id")
    provider = commands.add_parser(
        "provider-scripts", help="Browse or preview a provider script export"
    )
    provider.add_argument("cycle_id")
    provider.add_argument("--connection")
    provider.add_argument("--script")
    provider.add_argument("--offset", type=int, default=0)
    source_import = commands.add_parser(
        "import-provider-script", help="Import a previewed export as a draft"
    )
    source_import.add_argument("cycle_id")
    source_import.add_argument("file", type=Path)
    freeze = commands.add_parser(
        "freeze-report-evidence", help="Capture previewed summaries as a new report draft"
    )
    freeze.add_argument("cycle_id")
    freeze.add_argument("version_id")
    freeze.add_argument("file", type=Path)
    for operation in ("followup", "next-cycle"):
        handoff = commands.add_parser(operation)
        handoff.add_argument("cycle_id")
        handoff.add_argument("file", type=Path)
    source = commands.add_parser("sources", help="Browse revisioned Confluence sources")
    source.add_argument("cycle_id")
    source.add_argument("--connection")
    source.add_argument("--title", default="")
    source.add_argument("--page")
    source.add_argument("--revision")
    capture = commands.add_parser(
        "capture-source", help="Capture a previewed page as a scope draft"
    )
    capture.add_argument("cycle_id")
    capture.add_argument("version_id")
    capture.add_argument("file", type=Path)
    action = commands.add_parser("review", help="Review an exact deliverable version")
    action.add_argument("cycle_id")
    action.add_argument("--version", required=True)
    action.add_argument("--revision", required=True, type=int)
    action.add_argument(
        "--action",
        required=True,
        choices=("submit", "approve", "request_revision", "archive", "restore"),
    )
    action.add_argument("--note", default="")
    return result


def main() -> int:
    args = parser().parse_args()
    base = os.environ.get("APEX_URL", "http://127.0.0.1:2024").rstrip("/")
    key = os.environ.get("APEX_API_KEY")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
    ):
        print("APEX_URL must be HTTPS (HTTP is allowed only on loopback).", file=sys.stderr)
        return 2
    if not key:
        print("Set APEX_API_KEY for an authenticated operator identity.", file=sys.stderr)
        return 3
    path = "/v1/cycles"
    method = "GET"
    body = None
    query = None
    if args.command == "create":
        method = "POST"
        body = {
            name: getattr(args, name)
            for name in ("name", "project_id", "app_id", "environment_id", "mode")
        }
    elif args.command != "cycles":
        path += "/" + quote(args.cycle_id, safe="")
        if args.command in {
            "import",
            "prepare-execution",
            "import-provider-script",
            "freeze-report-evidence",
            "followup",
            "next-cycle",
            "capture-source",
        }:
            from apex.domain.cycle_executions import PrepareExecution
            from apex.domain.cycle_handoff import FollowupWrite, NextCycleRequest
            from apex.domain.cycle_sources import CaptureSource
            from apex.domain.cycles import VersionCreate
            from apex.domain.provider_scripts import ImportProviderScript
            from apex.domain.report_evidence import FreezeReportEvidence

            model = {
                "import": VersionCreate,
                "capture-source": CaptureSource,
                "followup": FollowupWrite,
                "next-cycle": NextCycleRequest,
                "prepare-execution": PrepareExecution,
                "import-provider-script": ImportProviderScript,
                "freeze-report-evidence": FreezeReportEvidence,
            }[args.command]

            try:
                if args.file.stat().st_size > 200_000:
                    raise ValueError("document too large")
                body = model.model_validate_json(args.file.read_text()).model_dump(mode="json")
            except (OSError, ValueError):
                print(
                    f"Import failed: use a valid {model.__name__} JSON document under 200 KB.",
                    file=sys.stderr,
                )
                return 2
            path += {
                "import": "/versions",
                "capture-source": (
                    f"/scopes/{quote(getattr(args, 'version_id', ''), safe='')}/sources"
                ),
                "followup": "/followups",
                "next-cycle": "/next-cycle",
                "prepare-execution": "/executions",
                "import-provider-script": "/provider-scripts/import",
                "freeze-report-evidence": (
                    f"/reports/{quote(getattr(args, 'version_id', ''), safe='')}/evidence"
                ),
            }[args.command]
            method = "POST"
        elif args.command == "sources":
            path += "/sources"
            query = {
                key: value
                for key, value in {
                    "connection_id": args.connection,
                    "title": args.title,
                    "page_id": args.page,
                    "revision": args.revision,
                }.items()
                if value is not None
            }
        elif args.command == "provider-scripts":
            path += "/provider-scripts"
            query = {
                key: value
                for key, value in {
                    "connection_id": args.connection,
                    "remote_script_id": args.script,
                    "offset": args.offset,
                }.items()
                if value is not None
            }
        elif args.command in {"executions", "scripts", "results"}:
            if args.command == "results" and args.baseline:
                query = {"baseline_id": args.baseline}
            path += "/" + args.command
        elif args.command in {"validate-script", "script-validations"}:
            path += "/executions/" + quote(args.preparation_id, safe="") + "/script-validations"
            if args.command == "validate-script":
                method = "POST"
                body = {"expected_revision": args.revision, "idempotency_key": args.idempotency_key}
        elif args.command == "store-script":
            path += "/scripts/" + quote(args.version_id, safe="") + "/artifact"
            method = "POST"
            body = {"expected_revision": args.revision, "connection_id": args.connection}
        elif args.command == "link-run":
            path += "/runs"
            method = "POST"
            body = {"engine_run_id": args.engine_run_id, "expected_revision": args.revision}
        elif args.command == "review":
            path += "/actions"
            method = "POST"
            body = {
                "version_id": args.version,
                "expected_revision": args.revision,
                "action": args.action,
                "note": args.note,
            }
    try:
        # Never forward credentials through a redirect; no automatic mutation retries.
        with httpx.Client(
            base_url=base, headers={"x-api-key": key}, follow_redirects=False, timeout=30
        ) as client:
            response = client.request(method, path, json=body, params=query)
        if not response.is_success:
            print(
                f"APEX request failed (HTTP {response.status_code}). "
                "Refresh status before retrying a mutation.",
                file=sys.stderr,
            )
            return (
                3 if response.status_code in {401, 403} else 4 if response.status_code == 409 else 5
            )
        data = response.json()
    except (httpx.HTTPError, ValueError):
        print(
            "APEX request failed. Check connectivity and query cycle status before retrying.",
            file=sys.stderr,
        )
        return 5
    if args.json or args.command == "export":
        print(json.dumps(data, sort_keys=True, indent=2))
    elif args.command == "sources":
        print(f"Cycle revision {data['revision']}")
        for connection in data["connections"]:
            print(f"  {connection['name']} [{connection['id']}]")
        for hit in data["hits"]:
            print(f"  {hit['title']} · page {hit['ref']['id']} · revision {hit['ref']['revision']}")
        if data.get("document"):
            print(data["document"]["text"])
            print(f"Content fingerprint: {data['checksum']}")
        for blocker in data["blockers"]:
            print(f"  Blocked: {blocker}")
    elif args.command == "results":
        print("Linked history summaries; cycle execution bindings remain unverified")
        for result in data["results"]:
            print(
                f"{result['engine']} attempt {result['attempt']} [{result['id']}] · "
                f"execution {result['execution_status']} · "
                f"test outcome {result['test_outcome'] or 'Not available'}"
            )
            for metric in result["metrics"]:
                value = metric["value"] if metric["value"] is not None else "Not available"
                print(f"  {metric['label']}: {value} {metric['unit']}")
                if data["baseline_id"] and result["id"] != data["baseline_id"]:
                    difference = metric["difference"]
                    percentage = metric["percent_difference"]
                    delta = difference if difference is not None else "Not available"
                    percent = percentage if percentage is not None else "Not available"
                    print(f"    Difference: {delta} · percentage: {percent}")
    elif args.command == "import-provider-script":
        print(f"Imported draft {data['version_id']} · provider executable content unverified")
    elif args.command == "provider-scripts":
        print(f"Cycle revision {data['revision']} · provider exports; execution content unverified")
        for connection in data["connections"]:
            print(f"  Connection {connection['name']} [{connection['id']}]")
        for script in data["scripts"]:
            print(
                f"  {script['name']} [{script['id']}] · {script['protocol']} "
                f"· version {script['version']}"
            )
        if data.get("preview"):
            print(f"Export SHA-256: {data['preview']['source_checksum']}")
            print(data["preview"]["source"])
        for blocker in data["blockers"]:
            print(f"  Blocked: {blocker}")
    elif args.command in {"prepare-execution", "link-run"}:
        print(f"Saved {data['id']} · cycle revision {data['revision']} · no test launched")
    elif args.command in {"validate-script", "script-validations"}:
        for record in (
            data.get("validations", []) if args.command == "script-validations" else [data]
        ):
            print(
                f"{record['id']} · source validation {record['status']} · "
                f"current: {record['current']}"
            )
            for issue in record["issues"]:
                print(f"  {issue}")
        if args.command == "script-validations":
            for blocker in data["blockers"]:
                print(f"  Blocked: {blocker}")
    elif args.command == "store-script":
        print(
            f"Stored source {data['source_checksum']} · "
            f"{data['size_bytes']} bytes · no test launched"
        )
    elif args.command == "scripts":
        print(f"Cycle revision {data['revision']}")
        for artifact in data["artifacts"]:
            state = "verified" if artifact["stored_at"] else "pending; retry required"
            print(f"  {artifact['version_id']} · {artifact['source_checksum']} · {state}")
    elif args.command == "executions":
        print(f"Cycle revision {data['revision']} · launch unavailable")
        for preparation in data["preparations"]:
            print(f"  {preparation['id']} · {preparation['snapshot']['script_title']}")
            for check in preparation["checks"]:
                print(f"    {check['status']}: {check['label']} — {check['detail']}")
        for run in data["linked_runs"]:
            print(f"  Run {run['id']} · {run['engine']} · {run['status']} · bindings unverified")
    else:
        for cycle in data if isinstance(data, list) else [data]:
            print(
                f"{cycle['name']} [{cycle['id']}] · revision {cycle['revision']} · {cycle['mode']}"
            )
            for blocker in cycle.get("blockers", []):
                print(f"  Next: {blocker}")
    if args.command == "validate-script":
        if not isinstance(data, dict):
            return 7
        if data.get("status") == "accepted":
            return 0 if data.get("current") is True else 7
        return 6 if data.get("status") == "rejected" else 7
    return 0


if __name__ == "__main__":
    sys.exit(main())
