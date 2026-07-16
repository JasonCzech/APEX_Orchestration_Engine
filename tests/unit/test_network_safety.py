"""Connect-time DNS pinning shared by adapters and result fetching."""

import asyncio
import socket
import ssl
import subprocess
import threading
import warnings
from typing import Any

import certifi
import httpx
import pytest

import apex.adapters.network_safety as network_safety
from apex.adapters.network_safety import (
    UnsafeDestinationError,
    resolve_destination,
    safe_async_http_client,
)
from apex.services.connections import validate_adapter_base_url


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/path?redirect=https://internal.example",
        "https://api.example.com/path#fragment",
        "https://api.example.com\\@internal.example",
        " https://api.example.com",
        "https://api.example.com\n.evil.example",
        "https://api.example.com:0",
        "https://api.example.com:65536",
        {"host": "api.example.com"},
    ],
)
def test_adapter_base_url_rejects_ambiguous_or_malformed_targets(url: object) -> None:
    with pytest.raises(ValueError, match="adapter URL"):
        validate_adapter_base_url(url)


def test_adapter_base_url_invalid_port_does_not_retain_caller_text() -> None:
    canary = "bare-invalid-port-canary"

    with pytest.raises(ValueError, match="invalid port") as excinfo:
        validate_adapter_base_url(f"https://api.example.com:{canary}")

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in str(excinfo.value)


@pytest.mark.parametrize("timeout_s", [0.0, -1.0, float("inf"), float("nan")])
def test_sync_transport_rejects_invalid_total_timeout(timeout_s: float) -> None:
    with pytest.raises(ValueError, match="finite and greater than zero"):
        network_safety.SafeHTTPTransport(total_timeout_s=timeout_s)


@pytest.mark.parametrize(
    "transport_type",
    [network_safety.SafeHTTPTransport, network_safety.SafeAsyncHTTPTransport],
)
def test_httpx_transports_normalize_ca_files_to_verified_ssl_contexts(
    transport_type: Any,
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        transport = transport_type(verify=certifi.where())

    context = transport._pool._ssl_context  # pyright: ignore[reportPrivateUsage]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert not any("verify=<str>" in str(warning.message) for warning in caught)


def test_ca_file_normalization_fails_closed_for_an_invalid_bundle(tmp_path: Any) -> None:
    invalid_ca = tmp_path / "invalid-ca.pem"
    invalid_ca.write_text("not a certificate")

    with pytest.raises(ssl.SSLError):
        network_safety.SafeHTTPTransport(verify=str(invalid_ca))


def _answer(address: str, port: int | None) -> tuple[object, ...]:
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port or 80))


def test_mixed_public_private_dns_answer_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        network_safety,
        "_resolve_hostname_sync",
        lambda *_args: ["93.184.216.34", "10.0.0.8"],
    )

    with pytest.raises(UnsafeDestinationError, match="private adapter hosts"):
        resolve_destination("mixed.example", 443)


def test_destination_validation_bounds_host_and_port_before_normalization_or_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class HostileHost(str):
        def strip(self, *_args: object, **_kwargs: object) -> str:
            calls.append("strip")
            raise AssertionError("host normalization hook ran")

        def rstrip(self, *_args: object, **_kwargs: object) -> str:
            calls.append("rstrip")
            raise AssertionError("host normalization hook ran")

    monkeypatch.setattr(
        network_safety,
        "_resolve_hostname_sync",
        lambda *_args, **_kwargs: calls.append("dns") or ["8.8.8.8"],
    )

    for host, port in (
        (HostileHost("example.com"), 443),
        ("x" * 254, 443),
        (" example.com", 443),
        ("example.com", True),
        ("example.com", 0),
        ("example.com", 65_536),
    ):
        with pytest.raises(UnsafeDestinationError, match="destination"):
            resolve_destination(host, port)

    assert calls == []


@pytest.mark.parametrize(
    "host",
    [
        "metadata",
        "metadata.google.internal",
        "alias.metadata.google.internal",
        "api.metadata.cloud.ibm.com",
        "instance-data.ec2.internal",
        "metadata.tencentyun.com",
    ],
)
def test_metadata_hosts_remain_forbidden_with_private_approval(host: str) -> None:
    with pytest.raises(UnsafeDestinationError, match="unconditionally forbidden"):
        resolve_destination(host, 80, allow_private_hosts=True)
    with pytest.raises(ValueError, match="host is forbidden"):
        validate_adapter_base_url(f"http://{host}/latest", allow_private_hosts=True)


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "::ffff:169.254.169.254",
        "fd00:ec2::254",
        "fd20:ce::254",
        "168.63.129.16",
        "::ffff:168.63.129.16",
        "100.100.100.200",
        "192.0.0.192",
        "::ffff:192.0.0.192",
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "ff02::1",
        "100.64.0.1",
        "198.18.0.1",
        "192.0.2.1",
        "255.255.255.255",
    ],
)
def test_private_approval_rejects_non_application_special_ranges(address: str) -> None:
    with pytest.raises(UnsafeDestinationError, match="forbidden address"):
        resolve_destination(address, 443, allow_private_hosts=True)
    url_host = f"[{address}]" if ":" in address else address
    with pytest.raises(ValueError, match="host is forbidden"):
        validate_adapter_base_url(
            f"https://{url_host}/resource",
            allow_private_hosts=True,
        )


@pytest.mark.parametrize(
    "address",
    ["10.2.3.4", "172.20.1.2", "192.168.10.4", "127.0.0.1", "fd00::10", "::1"],
)
def test_private_approval_is_limited_to_rfc1918_loopback_and_ula(address: str) -> None:
    assert resolve_destination(address, 443, allow_private_hosts=True)


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "fd00:ec2::254",
        "fd20:ce::254",
        "168.63.129.16",
        "100.100.100.200",
        "192.0.0.192",
        "::ffff:192.0.0.192",
    ],
)
@pytest.mark.parametrize("allow_private_hosts", [False, True])
def test_cloud_metadata_addresses_are_unconditionally_forbidden(
    address: str,
    allow_private_hosts: bool,
) -> None:
    with pytest.raises(UnsafeDestinationError):
        resolve_destination(address, 80, allow_private_hosts=allow_private_hosts)


@pytest.mark.parametrize("metadata_address", ["fd00:ec2::254", "fd20:ce::254", "192.0.0.192"])
def test_private_approved_dns_alias_cannot_resolve_to_instance_metadata(
    monkeypatch: pytest.MonkeyPatch,
    metadata_address: str,
) -> None:
    monkeypatch.setattr(
        network_safety,
        "_resolve_hostname_sync",
        lambda *_args, **_kwargs: [metadata_address],
    )

    with pytest.raises(UnsafeDestinationError, match="forbidden address"):
        resolve_destination("approved.internal", 80, allow_private_hosts=True)


@pytest.mark.parametrize("metadata_address", ["fd20:ce::254", "192.0.0.192"])
async def test_private_approved_async_alias_cannot_resolve_to_instance_metadata(
    monkeypatch: pytest.MonkeyPatch,
    metadata_address: str,
) -> None:
    async def resolve(*_args: object, **_kwargs: object) -> list[str]:
        return [metadata_address]

    monkeypatch.setattr(network_safety, "_resolve_hostname_async", resolve)
    backend = network_safety._PinnedAsyncBackend(allow_private_hosts=True)

    with pytest.raises(network_safety.httpcore.ConnectError, match="forbidden address"):
        await backend.connect_tcp("approved.internal", 80)


@pytest.mark.parametrize("metadata_address", ["fd00:ec2::254", "192.0.0.192"])
def test_private_approved_urllib_alias_cannot_resolve_to_instance_metadata(
    monkeypatch: pytest.MonkeyPatch,
    metadata_address: str,
) -> None:
    monkeypatch.setattr(
        network_safety,
        "_resolve_hostname_sync",
        lambda *_args, **_kwargs: [metadata_address],
    )
    connection = network_safety._PrivatePinnedHTTPConnection("approved.internal", port=80)

    with pytest.raises(network_safety.NewConnectionError, match="forbidden address"):
        connection._new_conn()


async def test_adapter_transport_revalidates_dns_at_connect_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_resolve(host: str, port: int, **_kwargs: object) -> list[str]:
        del host, port
        nonlocal calls
        calls += 1
        return ["127.0.0.1"]

    monkeypatch.setattr(network_safety, "_resolve_hostname_async", fake_resolve)

    validate_adapter_base_url("http://rebind.example")
    async with safe_async_http_client(timeout=1.0) as client:
        with pytest.raises(httpx.ConnectError, match="private adapter hosts are disabled"):
            await client.get("http://rebind.example/resource")

    assert calls == 1  # save/build is syntax-only; connect-time DNS is authoritative


def test_sync_dns_timeout_is_reported_as_unsafe_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timed_out(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(["python"], 0.01)

    monkeypatch.setattr(network_safety.subprocess, "run", timed_out)

    with pytest.raises(UnsafeDestinationError, match="DNS resolution timed out"):
        resolve_destination("slow.example", 443)


def test_sync_dns_worker_does_not_inherit_server_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_names = {
        "APEX_AUTH__DEV_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URI",
        "JIRA_TOKEN",
        "REDIS_URI",
    }
    for name in sensitive_names:
        monkeypatch.setenv(name, "sentinel-secret-must-not-reach-worker")

    captured_environment: dict[str, str] | None = None

    def run(
        command: tuple[str, ...],
        *,
        check: bool,
        capture_output: bool,
        timeout: float,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal captured_environment
        assert command[1:3] == ("-I", "-c")
        assert check is False
        assert capture_output is True
        assert timeout > 0
        captured_environment = dict(env)
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=b'["93.184.216.34"]',
            stderr=b"",
        )

    monkeypatch.setattr(network_safety.subprocess, "run", run)

    assert network_safety._resolve_hostname_sync("public.example", 443) == ["93.184.216.34"]
    assert captured_environment == network_safety._DNS_WORKER_ENV
    assert captured_environment is not None
    assert sensitive_names.isdisjoint(captured_environment)


async def test_async_dns_deadline_kills_the_resolver_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed = False

    class HungProcess:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            return b"[]", b""

        def kill(self) -> None:
            nonlocal killed
            killed = True
            self.returncode = -9

        async def wait(self) -> int:
            return -9

    async def create_process(*_args: object, **_kwargs: object) -> HungProcess:
        return HungProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(network_safety, "DNS_RESOLUTION_TIMEOUT_S", 0.01)
    monkeypatch.setattr(network_safety, "_DNS_ADMISSION", threading.BoundedSemaphore(1))

    with pytest.raises(UnsafeDestinationError, match="DNS resolution timed out"):
        await network_safety._resolve_destination_async(
            "slow.example",
            443,
            allow_private_hosts=False,
        )

    assert killed is True


async def test_cancel_during_dns_spawn_cannot_orphan_just_created_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = asyncio.Event()
    allow_spawn_return = asyncio.Event()
    killed = False
    reaped = False

    class JustSpawnedProcess:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            raise AssertionError("cancelled spawn must not start DNS communication")

        def kill(self) -> None:
            nonlocal killed
            killed = True
            self.returncode = -9

        async def wait(self) -> int:
            nonlocal reaped
            reaped = True
            return -9

    process = JustSpawnedProcess()

    async def create_process(*_args: object, **_kwargs: object) -> JustSpawnedProcess:
        # Model the ambiguity window: the OS child exists, but the await has not
        # returned the Process object to the caller for assignment yet.
        spawned.set()
        await allow_spawn_return.wait()
        return process

    admission = threading.BoundedSemaphore(1)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(network_safety, "_DNS_ADMISSION", admission)

    task = asyncio.create_task(network_safety._resolve_hostname_async("spawn.example", 443))
    await spawned.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False

    allow_spawn_return.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert killed is True
    assert reaped is True
    assert admission.acquire(blocking=False) is True
    admission.release()


async def test_cancelled_dns_is_worker_bounded_and_does_not_starve_to_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_lock = threading.Lock()
    state = {"active": 0, "max_active": 0, "calls": 0}

    class BlockingProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.release = asyncio.Event()

        async def communicate(self) -> tuple[bytes, bytes]:
            with state_lock:
                state["active"] += 1
                state["calls"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                await self.release.wait()
                return b"[]", b""
            finally:
                with state_lock:
                    state["active"] -= 1

        def kill(self) -> None:
            self.returncode = -9
            self.release.set()

        async def wait(self) -> int:
            await self.release.wait()
            return self.returncode or 0

    async def create_process(*_args: object, **_kwargs: object) -> BlockingProcess:
        return BlockingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        network_safety,
        "_DNS_ADMISSION",
        threading.BoundedSemaphore(network_safety.DNS_WORKER_LIMIT),
    )
    backend = network_safety._PinnedAsyncBackend(allow_private_hosts=False)
    tasks = [
        asyncio.create_task(backend.connect_tcp(f"host-{index}.example", 443))
        for index in range(network_safety.DNS_WORKER_LIMIT * 3)
    ]
    results: list[Any] = []
    try:
        for _ in range(200):
            with state_lock:
                saturated = state["active"] == network_safety.DNS_WORKER_LIMIT
            if saturated:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("DNS worker pool did not saturate")

        # Repeated cancellation must not release admission while getaddrinfo still
        # owns a worker; queued calls should be cancelled before they are submitted.
        for _ in range(3):
            for task in tasks:
                task.cancel()
            await asyncio.sleep(0)

        probe = await asyncio.wait_for(
            asyncio.to_thread(lambda: "available"),
            timeout=0.5,
        )
        assert probe == "available"
        with state_lock:
            assert state["max_active"] <= network_safety.DNS_WORKER_LIMIT
    finally:
        for task in tasks:
            task.cancel()
        results = list(await asyncio.gather(*tasks, return_exceptions=True))

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert state["calls"] == network_safety.DNS_WORKER_LIMIT


def test_resolved_connect_candidates_are_capped_after_full_policy_validation() -> None:
    addresses = [f"8.8.{index // 255}.{index % 255 + 1}" for index in range(20)]

    resolved = network_safety._validate_addresses(
        addresses,
        allow_private_hosts=False,
    )

    assert len(resolved) == network_safety.MAX_CONNECT_CANDIDATES


def test_sync_backend_detaches_destination_policy_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "sync-network-policy-context-secret-canary"

    def fail_resolution(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        error = UnsafeDestinationError("private adapter hosts are disabled")
        error.__context__ = RuntimeError(canary)
        raise error

    monkeypatch.setattr(network_safety, "resolve_destination", fail_resolution)
    backend = network_safety._PinnedSyncBackend(allow_private_hosts=False)

    with pytest.raises(network_safety.httpcore.ConnectError) as excinfo:
        backend.connect_tcp("private.example", 443)

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in repr(excinfo.value)


def test_sync_backend_never_invokes_or_reflects_hostile_policy_error_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class HostilePolicyError(UnsafeDestinationError):
        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("policy exception hook ran")

    def fail_resolution(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        raise HostilePolicyError("provider-policy-secret")

    monkeypatch.setattr(network_safety, "resolve_destination", fail_resolution)
    backend = network_safety._PinnedSyncBackend(allow_private_hosts=True)

    with pytest.raises(
        network_safety.httpcore.ConnectError,
        match="could not be resolved",
    ) as error:
        backend.connect_tcp("private.example", 443)

    assert calls == []
    assert "provider-policy-secret" not in repr(error.value)


async def test_async_backend_detaches_destination_policy_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "async-network-policy-context-secret-canary"

    async def fail_resolution(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        error = UnsafeDestinationError("private adapter hosts are disabled")
        error.__context__ = RuntimeError(canary)
        raise error

    monkeypatch.setattr(network_safety, "_resolve_destination_async", fail_resolution)
    backend = network_safety._PinnedAsyncBackend(allow_private_hosts=False)

    with pytest.raises(network_safety.httpcore.ConnectError) as excinfo:
        await backend.connect_tcp("private.example", 443)

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in repr(excinfo.value)


def test_urllib3_backend_detaches_destination_policy_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "urllib-network-policy-context-secret-canary"

    def fail_resolution(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        error = UnsafeDestinationError("private adapter hosts are disabled")
        error.__context__ = RuntimeError(canary)
        raise error

    monkeypatch.setattr(network_safety, "resolve_destination", fail_resolution)
    connection = network_safety._PinnedHTTPConnection("private.example", port=80)

    with pytest.raises(network_safety.NewConnectionError) as excinfo:
        connection._new_conn()

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in repr(excinfo.value)


def test_sync_backend_shares_one_connect_timeout_across_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    attempted_timeouts: list[float | None] = []

    def resolve(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return tuple(f"8.8.8.{index}" for index in range(1, 11))

    def connect(
        _backend: object,
        _host: str,
        _port: int,
        timeout: float | None = None,
        **_kwargs: object,
    ) -> object:
        nonlocal now
        attempted_timeouts.append(timeout)
        now += 0.4
        raise network_safety.httpcore.ConnectTimeout("black holed")

    monkeypatch.setattr(network_safety, "resolve_destination", resolve)
    monkeypatch.setattr(network_safety.time, "monotonic", lambda: now)
    monkeypatch.setattr(network_safety.SyncBackend, "connect_tcp", connect)

    backend = network_safety._PinnedSyncBackend(allow_private_hosts=False)
    with pytest.raises(network_safety.httpcore.ConnectTimeout, match="total connect"):
        backend.connect_tcp("many.example", 443, timeout=1.0)

    assert attempted_timeouts == pytest.approx([1.0, 0.6, 0.2])


async def test_async_backend_shares_one_connect_timeout_across_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    attempted_timeouts: list[float | None] = []

    async def resolve(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return tuple(f"8.8.8.{index}" for index in range(1, 11))

    async def connect(
        _backend: object,
        _host: str,
        _port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors httpcore's backend API
        **_kwargs: object,
    ) -> object:
        nonlocal now
        attempted_timeouts.append(timeout)
        now += 0.4
        raise network_safety.httpcore.ConnectTimeout("black holed")

    monkeypatch.setattr(network_safety, "_resolve_destination_async", resolve)
    monkeypatch.setattr(network_safety.time, "monotonic", lambda: now)
    monkeypatch.setattr(network_safety.AutoBackend, "connect_tcp", connect)

    backend = network_safety._PinnedAsyncBackend(allow_private_hosts=False)
    with pytest.raises(network_safety.httpcore.ConnectTimeout, match="total connect"):
        await backend.connect_tcp("many.example", 443, timeout=1.0)

    assert attempted_timeouts == pytest.approx([1.0, 0.6, 0.2])
