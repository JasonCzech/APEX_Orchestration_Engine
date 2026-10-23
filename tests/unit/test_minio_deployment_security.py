"""Fail-closed deployment regressions for the archived MinIO OSS server."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MINIO_MANIFEST = REPO_ROOT / "deploy/azure/k8s/minio/minio.yaml"
MINIO_NETWORK_POLICY = REPO_ROOT / "deploy/azure/k8s/minio/networkpolicy.yaml"
GATEWAY_CONFIG = REPO_ROOT / "deploy/minio-gateway/nginx.conf"
GATEWAY_IMAGE = (
    "nginxinc/nginx-unprivileged:1.31.2-alpine3.23"
    "@sha256:6320020c7da8714feab524e02c08c5a1958675c4e68700e93a2fd8970b065786"
)
RAW_MINIO_IMAGE = (
    "minio/minio:RELEASE.2024-07-16T23-46-41Z"
    "@sha256:77ff9f7e12549d269990b167fa21da010fa8b0beb40d6064569b8887e37c456b"
)
DEV_MINIO_IMAGE = (
    "bitnamilegacy/minio:2025.7.23-debian-12-r5"
    "@sha256:6dabb4a2088c9a79908de3bc05f4586c23ad2182c8908e7e3acbf61c1467fb20"
)


def _documents() -> dict[tuple[str, str], dict[str, Any]]:
    manifests = (MINIO_MANIFEST, MINIO_NETWORK_POLICY)
    return {
        (document["kind"], document["metadata"]["name"]): document
        for manifest in manifests
        for document in yaml.safe_load_all(manifest.read_text())
    }


def test_minio_is_loopback_only_behind_the_advisory_gateway() -> None:
    documents = _documents()
    deployment = documents[("Deployment", "apex-minio")]
    containers = {
        container["name"]: container
        for container in deployment["spec"]["template"]["spec"]["containers"]
    }
    minio = containers["minio"]
    gateway = containers["s3-security-gateway"]

    assert "127.0.0.1:9100" in minio["args"]
    assert "127.0.0.1:9101" in minio["args"]
    assert minio["image"] == RAW_MINIO_IMAGE
    assert gateway["image"] == GATEWAY_IMAGE
    assert gateway["securityContext"]["readOnlyRootFilesystem"] is True

    service_ports = documents[("Service", "apex-minio")]["spec"]["ports"]
    assert service_ports == [{"name": "s3", "port": 9000, "targetPort": "s3"}]


def test_minio_gateway_blocks_both_primary_advisory_request_shapes() -> None:
    shared_config = GATEWAY_CONFIG.read_text()
    manifest_config = _documents()[("ConfigMap", "apex-minio-gateway")]["data"]["nginx.conf"]
    required_fragments = (
        "STREAMING-UNSIGNED-PAYLOAD-TRAILER",
        "~*(^|&)select(?:=|&|$) 1;",
        '"POST:1" 1;',
        "~*^(uploads=?|delete=?|uploadId=[^&]+)$ 1;",
        '"POST:0" 1;',
        "proxy_pass http://127.0.0.1:9100;",
        "proxy_set_header Host $http_host;",
        "log_format apex_safe escape=json",
        '"method":"$request_method","uri":"$uri"',
        "access_log /dev/stdout apex_safe;",
        "error_log /dev/null;",
        "proxy_intercept_errors on;",
        "error_page 500 502 503 504 = @sanitized_upstream_error;",
        'return 502 "S3 gateway upstream unavailable\\n";',
    )

    for fragment in required_fragments:
        assert fragment in shared_config
        assert fragment in manifest_config

    for config in (shared_config, manifest_config):
        assert "access_log /dev/stdout;" not in config
        assert "error_log /dev/stderr" not in config
        assert "$request " not in config
        assert "$request_uri" not in config
        # `$args` is needed only by the request-shape deny rules. It must never
        # appear in either log format or the sanitized upstream error location.
        log_section = config[config.index("log_format apex_safe") : config.index("map $http")]
        assert "$args" not in log_section
        assert "authorization" not in log_section.lower()
        error_location = config[config.index("location @sanitized_upstream_error") :]
        assert "$args" not in error_location
        assert "$request" not in error_location


def test_minio_network_policy_allows_only_server_and_backup_gateway_ingress() -> None:
    policy = _documents()[("NetworkPolicy", "apex-minio")]["spec"]
    ingress = policy["ingress"]

    assert policy["policyTypes"] == ["Ingress", "Egress"]
    assert policy["egress"] == []
    assert ingress[0]["ports"] == [{"port": 9000, "protocol": "TCP"}]
    selectors = [source["podSelector"]["matchLabels"] for source in ingress[0]["from"]]
    assert selectors == [
        {"apex.io/minio-client": "true"},
        {"app.kubernetes.io/name": "apex-minio-backup"},
    ]


def test_compose_minio_has_no_network_visible_unfiltered_listener() -> None:
    compose_paths = (
        REPO_ROOT / "docker-compose.yaml",
        REPO_ROOT / "docker-compose.dev.yaml",
        REPO_ROOT / "deploy/compose-ha/docker-compose.ha.yaml",
    )
    for path in compose_paths:
        services = yaml.safe_load(path.read_text())["services"]
        gateway = services["minio"]
        storage = services["minio-storage"]

        assert gateway["image"] == GATEWAY_IMAGE
        # minio/minio is no longer pullable from any registry.
        assert storage["image"] == DEV_MINIO_IMAGE
        # Existing volumes were created by the prior root-running image.
        assert storage["user"] == "0:0"
        assert storage["entrypoint"] == ["minio"]
        if path.name == "docker-compose.dev.yaml":
            assert "miniodata:/bitnami/minio/data" in storage["volumes"]
            assert "/bitnami/minio/data" in storage["command"]
        else:
            assert storage["command"].startswith("server /data ")
            assert any(volume.endswith(":/data") for volume in storage["volumes"])
        assert storage["network_mode"] == "service:minio"
        assert "127.0.0.1:9100" in storage["command"]
        assert "ports" not in storage


def test_ci_minio_uses_available_image_behind_loopback_gateway() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yaml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["integration"]["steps"]
        if step.get("name") == "Start MinIO"
    )
    commands = [
        shlex.split(line)
        for line in step["run"].replace("\\\n", "").splitlines()
        if line.lstrip().startswith("docker run ")
    ]
    gateway, storage = commands

    assert gateway[-1] == GATEWAY_IMAGE
    assert storage[storage.index("--network") + 1] == "container:apex-minio-gateway"
    assert storage[storage.index("--user") + 1] == "0:0"
    assert storage[storage.index("--entrypoint") + 1] == "minio"
    assert storage[-7] == DEV_MINIO_IMAGE
    assert storage[-6:] == [
        "server",
        "/bitnami/minio/data",
        "--address",
        "127.0.0.1:9100",
        "--console-address",
        "127.0.0.1:9101",
    ]
    assert "-p" not in storage
    assert "--publish" not in storage
