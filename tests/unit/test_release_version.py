from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from scripts.release_version import main, normalize_release_tag

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = REPO_ROOT / ".github/workflows/release.yaml"
LANGGRAPH_CONFIG = REPO_ROOT / "langgraph.json"
TRIVY_ACTION = "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25"


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v0.0.0", "0.0.0"),
        ("v1.2.3", "1.2.3"),
        ("v1.2.3-alpha", "1.2.3-alpha"),
        ("v1.2.3-alpha.1-x", "1.2.3-alpha.1-x"),
        ("v1.2.3-0", "1.2.3-0"),
    ],
)
def test_normalize_release_tag_accepts_canonical_semver(tag: str, expected: str) -> None:
    assert normalize_release_tag(tag) == expected


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "v",
        "v01.2.3",
        "v1.02.3",
        "v1.2.03",
        "v1.2.3-",
        "v1.2.3-alpha..1",
        "v1.2.3-01",
        "v1.2.3+build",
        "v1.2.3-alpha_beta",
        "v1.2",
        "v1.2.3.4",
        "v1.2.3-" + "a" * 123,
    ],
)
def test_normalize_release_tag_rejects_noncanonical_or_overlong_tags(tag: str) -> None:
    with pytest.raises(ValueError):
        normalize_release_tag(tag)


def test_release_version_cli_is_opaque_on_invalid_tag(capsys: pytest.CaptureFixture[str]) -> None:
    canary = "v1.2.3+do-not-reflect"

    assert main([canary]) == 2

    captured = capsys.readouterr()
    assert canary not in captured.err
    assert captured.out == ""


def test_release_workflow_stamps_sdk_with_the_validated_tag() -> None:
    workflow = yaml.load(RELEASE_WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]

    version_runs = [step.get("run", "") for step in jobs["version"]["steps"]]
    assert any(
        'python3 scripts/release_version.py "$GITHUB_REF_NAME"' in run for run in version_runs
    )

    sdk = jobs["sdk"]
    assert set(sdk["needs"]) == {"gates", "dashboard-gates", "version"}
    stamp = next(
        step
        for step in sdk["steps"]
        if step.get("name") == "Stamp package with the validated release version"
    )
    assert stamp["env"]["RELEASE_VERSION"] == "${{ needs.version.outputs.value }}"
    assert "npm version --workspace @apex/api-client" in stamp["run"]


def test_release_workflow_only_accepts_tags_reachable_from_main() -> None:
    workflow = yaml.load(RELEASE_WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["version"]["steps"]
    checkout = next(
        step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    provenance = next(
        step for step in steps if step.get("name") == "Enforce main-only release provenance"
    )

    assert checkout["with"]["fetch-depth"] == "0"
    fetch = "git fetch --no-tags --prune origin +refs/heads/main:refs/remotes/origin/main"
    ancestry = 'git merge-base --is-ancestor "$GITHUB_SHA" refs/remotes/origin/main'
    normalized_run = " ".join(provenance["run"].split())
    assert fetch in normalized_run
    assert ancestry in normalized_run
    assert normalized_run.index(fetch) < normalized_run.index(ancestry)


def test_server_image_build_uses_the_frozen_uv_lock() -> None:
    config = yaml.safe_load(LANGGRAPH_CONFIG.read_text())

    assert config["source"] == {"kind": "uv", "root": "."}
    assert "dependencies" not in config
    assert config["dockerfile_lines"] == [
        "ARG APEX_BUILD_VERSION=0.0.0+local",
        "ENV APEX_VERSION=${APEX_BUILD_VERSION}",
        "LABEL org.opencontainers.image.version=${APEX_BUILD_VERSION}",
    ]

    workflows = (
        (REPO_ROOT / ".github/workflows/ci.yaml", "checks"),
        (RELEASE_WORKFLOW, "gates"),
        (REPO_ROOT / ".github/workflows/deploy-aks.yaml", "verify"),
    )
    for path, job_name in workflows:
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        recipe_gate = next(
            step
            for step in workflow["jobs"][job_name]["steps"]
            if step.get("name") == "Validate pinned LangGraph image recipe"
        )["run"]
        assert "uv run langgraph validate" in recipe_gate
        assert "uv run langgraph dockerfile /tmp/apex-langgraph.Dockerfile" in recipe_gate
        assert (
            "uv run python scripts/validate_langgraph_recipe.py /tmp/apex-langgraph.Dockerfile"
        ) in recipe_gate


def test_release_and_aks_builds_embed_the_validated_image_identity() -> None:
    release = RELEASE_WORKFLOW.read_text()
    deploy = (REPO_ROOT / ".github/workflows/deploy-aks.yaml").read_text()

    assert '--build-arg "APEX_BUILD_VERSION=$RELEASE_VERSION"' in release
    assert "Verify server image release identity" in release
    assert "print(ApexSettings().version)" in release
    assert '--build-arg "APEX_BUILD_VERSION=$TAG"' in deploy


def test_release_dashboard_image_requires_full_frozen_dashboard_gates() -> None:
    workflow = yaml.load(RELEASE_WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]
    dashboard_gates = jobs["dashboard-gates"]

    runs = [step.get("run", "") for step in dashboard_gates["steps"]]
    expected_commands = {
        "npm ci",
        "npm audit --audit-level=high",
        "npm run -w @apex/pipeline-events typecheck",
        "npm run -w @apex/pipeline-events lint",
        "npm run -w @apex/pipeline-events test",
        "npm run -w @apex/api-client typecheck",
        "npm run -w @apex/dashboard typecheck",
        "npm run -w @apex/dashboard lint",
        "npm run -w @apex/dashboard test",
        "npm run -w @apex/dashboard test:coverage",
        "npm run -w @apex/dashboard build",
    }

    assert expected_commands <= set(runs)
    assert any(
        "npm run generate:sdks" in run
        and "git diff --exit-code packages/api-client/src/schema.d.ts" in run
        for run in runs
    )
    assert set(jobs["dashboard-image"]["needs"]) == {
        "dashboard-gates",
        "gates",
        "version",
    }


def test_release_images_are_scanned_before_they_become_artifacts() -> None:
    workflow = yaml.load(RELEASE_WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    expected_images = {
        "server-image": "apex-orchestration-engine:${{ needs.version.outputs.value }}",
        "dashboard-image": "apex-dashboard:${{ needs.version.outputs.value }}",
    }

    for job_name, image_ref in expected_images.items():
        steps = workflow["jobs"][job_name]["steps"]
        scan_index, scan = next(
            (index, step)
            for index, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("aquasecurity/trivy-action@")
        )
        save_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Save image as artifact"
        )

        assert scan_index < save_index
        assert scan["uses"] == TRIVY_ACTION
        assert scan["with"] == {
            "scan-type": "image",
            "image-ref": image_ref,
            "version": "v0.70.0",
            "scanners": "vuln",
            "vuln-type": "os,library",
            "severity": "HIGH,CRITICAL",
            "ignore-unfixed": "false",
            "exit-code": "1",
            "format": "table",
        }


def test_npm_publish_uses_the_signed_and_attested_tarball() -> None:
    workflow = yaml.load(RELEASE_WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["sdk"]["steps"]
    package = next(step for step in steps if step.get("id") == "sdk-package")
    publish = next(step for step in steps if step.get("name") == "Publish to npm")

    assert "tarball=%s" in package["run"]
    assert publish["env"]["SDK_TARBALL"] == "${{ steps.sdk-package.outputs.tarball }}"
    assert 'npm publish "$SDK_TARBALL"' in publish["run"]
    assert "working-directory" not in publish
