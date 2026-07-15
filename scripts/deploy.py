"""Turnkey deploy/operate tasks for APEX (stdlib only; mirrors scripts/dev.py).

Wraps the image build, Compose stacks, Helm install, and the AKS bring-up so each
target is one command. Shells out to docker / helm / terraform / az / kubectl —
those must be on PATH. Run with no args to list tasks.

  uv run python scripts/deploy.py image-build [tag]
  uv run python scripts/deploy.py compose-up
  uv run python scripts/deploy.py aks-up            # needs APEX_ENV + az login (see README)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART = "deploy/helm/apex-orchestration-engine"
TF = "deploy/terraform"
DEFAULT_TAG = "local"

Task = Callable[[list[str]], None]
TASKS: dict[str, tuple[str, Task]] = {}


def task(name: str, help_text: str) -> Callable[[Task], Task]:
    def register(func: Task) -> Task:
        TASKS[name] = (help_text, func)
        return func

    return register


def run(command: list[str]) -> None:
    print("+ " + " ".join(command), flush=True)
    try:
        subprocess.run(command, cwd=REPO_ROOT, check=True)
    except FileNotFoundError:
        raise SystemExit(
            f"Required executable not found: {command[0]}. Install it / add to PATH."
        ) from None
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from None


def capture(command: list[str]) -> str:
    result = subprocess.run(command, cwd=REPO_ROOT, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"${name} is required for this task.")
    return value


def _arg(args: list[str], index: int, default: str) -> str:
    return args[index] if len(args) > index else default


# ── images ────────────────────────────────────────────────────────────────────


@task("image-build", "Build the server (langgraph) + dashboard images. Args: [tag]")
def image_build(args: list[str]) -> None:
    tag = _arg(args, 0, DEFAULT_TAG)
    run(["uv", "run", "langgraph", "build", "-t", f"apex-orchestration-engine:{tag}"])
    run(["docker", "build", "-f", "apps/dashboard/Dockerfile", "-t", f"apex-dashboard:{tag}", "."])


@task("image-push", "Tag + push both images to a registry. Args: <registry> [tag]")
def image_push(args: list[str]) -> None:
    if not args:
        raise SystemExit("usage: image-push <registry> [tag]")
    registry, tag = args[0].rstrip("/"), _arg(args, 1, DEFAULT_TAG)
    for name in ("apex-orchestration-engine", "apex-dashboard"):
        run(["docker", "tag", f"{name}:{tag}", f"{registry}/{name}:{tag}"])
        run(["docker", "push", f"{registry}/{name}:{tag}"])


# ── compose ─────────────────────────────────────────────────────────────────


@task("compose-up", "Build + start the full local stack (infra + server + dashboard).")
def compose_up(_: list[str]) -> None:
    run(["docker", "compose", "-f", "docker-compose.yaml", "up", "-d", "--build", "--wait"])


@task("compose-down", "Stop the full local stack.")
def compose_down(_: list[str]) -> None:
    run(["docker", "compose", "-f", "docker-compose.yaml", "down"])


@task("compose-ha-up", "Start the HA soak rig (2 replicas + nginx). Needs the license env vars.")
def compose_ha_up(_: list[str]) -> None:
    run(
        [
            "docker",
            "compose",
            "-f",
            "deploy/compose-ha/docker-compose.ha.yaml",
            "up",
            "-d",
            "--wait",
        ]
    )


# ── kubernetes / helm ─────────────────────────────────────────────────────────


@task("helm-install", "helm upgrade --install. Args: [release] [namespace] [extra helm args...]")
def helm_install(args: list[str]) -> None:
    release = _arg(args, 0, "apex")
    namespace = _arg(args, 1, "apex")
    extra = args[2:]
    run(
        [
            "helm",
            "upgrade",
            "--install",
            release,
            CHART,
            "-n",
            namespace,
            "--create-namespace",
            *extra,
        ]
    )


# ── azure aks (turnkey) ───────────────────────────────────────────────────────


def _tf_output(name: str) -> str:
    return capture(["terraform", f"-chdir={TF}", "output", "-raw", name])


def _helm_fullname(release: str, chart: str = "apex-orchestration-engine") -> str:
    """Mirror the chart's fullname helper, including Kubernetes truncation."""
    value = release if chart in release else f"{release}-{chart}"
    return value[:63].rstrip("-")


@task("aks-up", "Provision Azure + deploy. Needs APEX_ENV (dev|staging|prod) and `az login`.")
def aks_up(_: list[str]) -> None:
    env = require_env("APEX_ENV")
    hostname = require_env("APEX_HOSTNAME")
    tls_secret = require_env("APEX_TLS_SECRET")
    tag = os.environ.get("APEX_TAG") or f"sha-{capture(['git', 'rev-parse', '--short=12', 'HEAD'])}"
    namespace = os.environ.get("APEX_NAMESPACE", "apex")
    release = os.environ.get("APEX_RELEASE", "apex")

    # 1) Provision (the backend must already be initialized — see deploy/terraform/README.md).
    fullname = _helm_fullname(release)
    backend_deployment = f"deployment/{fullname}"
    dashboard_deployment = f"deployment/{(fullname + '-dashboard')[:63].rstrip('-')}"
    run(
        [
            "terraform",
            f"-chdir={TF}",
            "apply",
            "-auto-approve",
            "-input=false",
            f"-var-file=env/{env}.tfvars",
            f"-var=workload_namespace={namespace}",
        ]
    )
    acr = _tf_output("acr_login_server")
    aks = _tf_output("aks_cluster_name")
    rg = _tf_output("resource_group")
    kv = _tf_output("key_vault_name")
    tenant = _tf_output("tenant_id")
    client_id = _tf_output("workload_identity_client_id")
    service_account = _tf_output("workload_service_account")
    hook_service_account = _tf_output("workload_hook_service_account")

    # 2) Build + push images to ACR (langgraph build has no Dockerfile -> can't use `az acr build`).
    image_build([tag])
    run(["az", "acr", "login", "--name", acr.split(".")[0]])
    image_push([acr, tag])

    # 3) Cluster credentials.
    run(
        [
            "az",
            "aks",
            "get-credentials",
            "--resource-group",
            rg,
            "--name",
            aks,
            "--overwrite-existing",
        ]
    )

    # 4) Prepare a fresh namespace and wait for the Terraform-enabled CSI API.
    # The chart's ordered pre-install hooks mount Key Vault and synthesize Secrets
    # before migration/bootstrap consume them.
    namespace_manifest = capture(
        ["kubectl", "create", "namespace", namespace, "--dry-run=client", "-o", "json"]
    )
    print("+ kubectl apply -f - <namespace-manifest>", flush=True)
    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        cwd=REPO_ROOT,
        check=True,
        input=namespace_manifest,
        text=True,
    )
    run(
        [
            "kubectl",
            "wait",
            "--for=condition=Established",
            "crd/secretproviderclasses.secrets-store.csi.x-k8s.io",
            "--timeout=5m",
        ]
    )
    run(["kubectl", "-n", namespace, "get", "secret", tls_secret])

    # 5) MinIO may remain Pending until the chart's CSI sync hook creates its Secret.
    run(["kubectl", "apply", "-n", namespace, "-f", "deploy/azure/k8s/minio/minio.yaml"])

    # 6) Deploy. Migration + bootstrap run as pre-upgrade hooks (migrate-then-roll).
    run(
        [
            "helm",
            "upgrade",
            "--install",
            release,
            CHART,
            "-n",
            namespace,
            "--create-namespace",
            "-f",
            "deploy/azure/helm/values-azure.yaml",
            "--set",
            f"image.repository={acr}/apex-orchestration-engine",
            "--set",
            f"image.tag={tag}",
            "--set",
            f"dashboard.image.repository={acr}/apex-dashboard",
            "--set",
            f"dashboard.image.tag={tag}",
            "--set-string",
            f"dashboard.backendUpstream=http://{fullname}:80",
            "--set-string",
            "bootstrap.document.connections[0].options.endpoint=apex-minio:9000",
            "--set-string",
            f"ingress.hosts[0].host={hostname}",
            "--set-string",
            f"ingress.tls[0].hosts[0]={hostname}",
            "--set-string",
            f"ingress.tls[0].secretName={tls_secret}",
            "--set-string",
            f'apexSettings.APEX_CORS_ORIGINS=["https://{hostname}"]',
            "--set",
            f"secretBackend.csi.keyvaultName={kv}",
            "--set",
            f"secretBackend.csi.tenantId={tenant}",
            "--set",
            f"workloadIdentity.clientId={client_id}",
            "--set",
            f"serviceAccount.name={service_account}",
            "--set",
            f"hookServiceAccountName={hook_service_account}",
            "--wait",
            "--timeout",
            "15m",
        ]
    )
    # A reused image tag or rotated Secret does not change the pod template.
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "rollout",
            "restart",
            backend_deployment,
            dashboard_deployment,
        ]
    )
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "rollout",
            "status",
            backend_deployment,
        ]
    )
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "rollout",
            "status",
            dashboard_deployment,
        ]
    )
    run(["kubectl", "-n", namespace, "rollout", "restart", "deployment/apex-minio"])
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "rollout",
            "status",
            "deployment/apex-minio",
            "--timeout=5m",
        ]
    )
    run(
        [
            "kubectl",
            "apply",
            "-n",
            namespace,
            "-f",
            "deploy/azure/k8s/minio/backup-cronjob.yaml",
        ]
    )
    print("aks-up complete. Smoke /ok — see docs/runbooks/aks-deployment.md.")


@task("aks-down", "Destroy the Azure stack for APEX_ENV. Irreversible.")
def aks_down(_: list[str]) -> None:
    env = require_env("APEX_ENV")
    run(
        [
            "terraform",
            f"-chdir={TF}",
            "destroy",
            "-auto-approve",
            "-input=false",
            f"-var-file=env/{env}.tfvars",
        ]
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="APEX deploy task runner")
    parser.add_argument("task", nargs="?", choices=sorted(TASKS))
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def print_tasks() -> None:
    print("Available tasks:")
    for name in sorted(TASKS):
        help_text, _ = TASKS[name]
        print(f"  {name:<14} {help_text}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.task is None:
        print_tasks()
        return 0
    extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
    TASKS[args.task][1](extra)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
