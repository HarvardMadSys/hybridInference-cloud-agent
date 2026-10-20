"""Shared SSH deployment protects credentials, retained state and repeatability."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shlex
import tarfile
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from dotenv import dotenv_values
from pydantic import SecretStr, ValidationError

from cloud_agent.ports.external.compute_provider import (
    NodeObservation,
    ProviderError,
    SshConnection,
)
from cloud_agent.ports.external.host_bootstrap import BootstrapIntent, DeploymentProfile
from cloud_agent.providers.compute.host_bootstrap import (
    _DEPLOY,
    _INVENTORY,
    _PREFLIGHT,
    _READINESS,
    _REQUIRED_ENV,
    SshHostBootstrap,
)

NODE = NodeObservation(
    provider_node_id="node-1",
    instance_identity="allocation:machine",
    display_name="one",
    machine_identity="verified",
    connection=SshConnection(
        address="node.example.org",
        username="agent",
        credential_ref="ssh-1",
        known_hosts_ref="ssh-1:known_hosts",
    ),
)
INTENT = BootstrapIntent(
    operation_id="operation-1",
    allocation_id="allocation-1",
    node_id="node-1",
    host="managed-host-1",
)
PREFLIGHT = {
    "machine_identity": "verified",
    "operating_system": "ubuntu:22.04",
    "architecture": "x86_64",
    "checks": {
        "supported_os": True,
        "architecture": True,
        "privileged_deploy": True,
        "kvm": True,
        "docker": False,
        "compose": False,
    },
}


def setup(tmp_path, *, unsafe_member=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for filename in [
            "deploy/host/sandbox_host.sh",
            "deploy/host/setup_kata_runtime.sh",
            "deploy/host/docker-compose.sandbox.yml",
            "pyproject.toml",
            ".dockerignore",
        ]:
            member = tarfile.TarInfo(filename)
            data = b".env\n" if filename == ".dockerignore" else b"# reviewed source\n"
            member.size, member.mode = len(data), 0o755
            archive.addfile(member, io.BytesIO(data))
        if unsafe_member:
            archive.addfile(unsafe_member)
    source = tmp_path / "source.tar.gz"
    source.write_bytes(buffer.getvalue())
    env = dict.fromkeys(_REQUIRED_ENV, "value")
    files = {}
    secrets = {}
    for prefix in ("AGENT_TUNNEL", "AGENT_MCP_TUNNEL", "AGENT_CONTROL_PLANE_TUNNEL"):
        for suffix in ("SSH_KEY", "SSH_KNOWN_HOSTS"):
            filename = ".compute-secrets/" + prefix.lower() + "-" + suffix.lower()
            ref = "deployment:" + prefix.lower() + "-" + suffix.lower()
            env[prefix + "_" + suffix] = "{deploy_root}/" + filename
            files[filename] = ref
            secrets[ref] = "DEDICATED TUNNEL KEY" if suffix == "SSH_KEY" else "verified-host-key"
    env["AGENT_SANDBOX_MANAGER_TOKEN"] = "sandbox-manager-secret-$literal"
    secrets["deployment:env"] = "\n".join(f"{key}={value}" for key, value in env.items())
    profile = DeploymentProfile(
        id="pilot",
        label="Pilot",
        source_bundle=str(source),
        source_sha256=hashlib.sha256(buffer.getvalue()).hexdigest(),
        deployment_sha="a" * 40,
        environment_ref="deployment:env",
        files=files,
    )
    resolver = AsyncMock()
    resolver.resolve.side_effect = lambda ref: secrets[ref]
    transport = AsyncMock()
    transport.run.side_effect = [
        json.dumps(PREFLIGHT),
        json.dumps({"ready": True, "blocked_reasons": []}),
    ]
    return SshHostBootstrap(transport=transport, resolver=resolver), profile, transport, secrets


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_names", [False, True])
async def test_vanilla_node_bootstrap_uses_one_recipe_and_secrets_stay_out_of_arguments(
    tmp_path, legacy_names
):
    from cloud_agent_shared.sandbox_manager_compat import LEGACY_ENV_NAMES

    bootstrap, profile, transport, secrets = setup(tmp_path)
    if legacy_names:
        for current, legacy in LEGACY_ENV_NAMES.items():
            secrets["deployment:env"] = secrets["deployment:env"].replace(current, legacy)
    result = await bootstrap.deploy(NODE, profile, INTENT, SecretStr("one-time-enrollment"))
    assert result.ready
    deploy_call = transport.run.call_args
    command = deploy_call.args[1]
    assert "one-time-enrollment" not in command and "sandbox-manager-secret" not in command
    assert "StrictHostKeyChecking=no" not in command
    assert "sandbox_host.sh" in command and "setup_kata_runtime.sh" in command
    assert deploy_call.kwargs["timeout"] == profile.timeout_seconds
    with tarfile.open(fileobj=io.BytesIO(deploy_call.kwargs["stdin"]), mode="r:gz") as archive:
        env = dotenv_values(
            stream=io.StringIO(archive.extractfile(".env").read().decode()), interpolate=False
        )
        assert env["AGENT_ENROLL_TOKEN"] == "one-time-enrollment"
        assert env["AGENT_RUNNER_HOST"] == INTENT.host
        assert env["AGENT_SANDBOX_BACKEND"] == "kata"
        assert env["AGENT_SANDBOX_MANAGER_TOKEN"] == "sandbox-manager-secret-$literal"
        if legacy_names:
            assert env["AGENT_WORKSPACE_BROKER_TOKEN"] == env["AGENT_SANDBOX_MANAGER_TOKEN"]
            assert env["AGENT_BROKER_REVERSE_PORT"] == env["AGENT_SANDBOX_MANAGER_REVERSE_PORT"]
        assert archive.getmember(".env").mode == 0o600
        assert b".compute-secrets" in archive.extractfile(".dockerignore").read()
        assert all(name.startswith(".compute-secrets/") for name in profile.files)
        assert b"account-token" not in deploy_call.kwargs["stdin"]


@pytest.mark.asyncio
async def test_selected_storage_path_is_used_by_bootstrap_and_daemon_attestation(tmp_path):
    bootstrap, profile, transport, secrets = setup(tmp_path)
    profile = profile.model_copy(
        update={"data_mount": "/profile-disk", "sandbox_storage_path": "/sandbox-disk"}
    )
    transport.run.side_effect = [
        json.dumps(
            {
                **PREFLIGHT,
                "checks": {
                    **PREFLIGHT["checks"],
                    "data_mount_available": True,
                    "data_mount_conflict_free": True,
                },
            }
        ),
        json.dumps({"ready": True, "blocked_reasons": []}),
    ]
    secrets["deployment:env"] += "\nAGENT_RETAINED_DOCKER_DATA_ROOT=/profile-docker"
    result = await bootstrap.deploy(NODE, profile, INTENT, SecretStr("token"))
    assert result.ready
    params = json.loads(shlex.split(transport.run.call_args.args[1])[-1])
    assert params["data_mount"] == "/sandbox-disk"
    assert params["identity"]["sandbox_storage_path"] == "/sandbox-disk"
    preflight_params = json.loads(shlex.split(transport.run.await_args_list[0].args[1])[-1])
    assert preflight_params["deployment"] == {
        "root": params["root"],
        "identity": params["identity"],
    }
    with tarfile.open(
        fileobj=io.BytesIO(transport.run.call_args.kwargs["stdin"]), mode="r:gz"
    ) as archive:
        env = dotenv_values(
            stream=io.StringIO(archive.extractfile(".env").read().decode()), interpolate=False
        )
    assert env["AGENT_RETAINED_DOCKER_DATA_ROOT"] == "/var/lib/docker"


def test_bootstrap_keeps_the_legacy_profile_storage_when_no_path_is_selected(tmp_path):
    _, profile, _, _ = setup(tmp_path)
    profile = profile.model_copy(update={"data_mount": "/profile-disk"})
    params = SshHostBootstrap._params(profile, INTENT)
    assert params["data_mount"] == "/profile-disk"
    assert "sandbox_storage_path" not in params["identity"]


@pytest.mark.asyncio
async def test_retry_without_enrollment_uses_persisted_host_credential(tmp_path):
    bootstrap, profile, transport, _ = setup(tmp_path)
    await bootstrap.deploy(NODE, profile, INTENT, None)
    with tarfile.open(
        fileobj=io.BytesIO(transport.run.call_args.kwargs["stdin"]), mode="r:gz"
    ) as archive:
        assert b"AGENT_ENROLL_TOKEN" not in archive.extractfile(".env").read()
    params = json.loads(shlex.split(transport.run.call_args.args[1])[-1])
    assert params["reenroll"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("check", ["supported_os", "architecture", "privileged_deploy", "kvm"])
async def test_preflight_blocks_install_when_prerequisite_cannot_be_installed(tmp_path, check):
    bootstrap, profile, transport, _ = setup(tmp_path)
    transport.run.side_effect = [
        json.dumps({**PREFLIGHT, "checks": {**PREFLIGHT["checks"], check: False}})
    ]
    result = await bootstrap.deploy(NODE, profile, INTENT, SecretStr("token"))
    assert not result.ready and check in result.blocked_reasons
    assert transport.run.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("selected_path", [None, "/selected-disk"])
@pytest.mark.parametrize(
    ("storage_checks", "blocked"),
    [
        ({}, "data_mount_unavailable"),
        (
            {"data_mount_available": False, "data_mount_conflict_free": False},
            "data_mount_unavailable",
        ),
        (
            {"data_mount_available": True, "data_mount_conflict_free": False},
            "data_mount_conflict",
        ),
        ({"data_mount_available": True, "data_mount_conflict_free": True}, None),
    ],
)
async def test_storage_preflight_uses_effective_disk_and_preserves_failure_codes(
    tmp_path, selected_path, storage_checks, blocked
):
    bootstrap, profile, transport, _ = setup(tmp_path)
    profile = profile.model_copy(
        update={"data_mount": "/profile-disk", "sandbox_storage_path": selected_path}
    )
    transport.run.side_effect = [
        json.dumps({**PREFLIGHT, "checks": {**PREFLIGHT["checks"], **storage_checks}})
    ]
    result = await bootstrap.preflight(NODE, profile)
    command = shlex.split(transport.run.call_args.args[1])
    assert command[:2] == ["python3", "-c"]
    assert command[2] == _PREFLIGHT
    assert json.loads(command[3]) == {
        "data_mount": selected_path or "/profile-disk",
        "install_prerequisites": True,
    }
    assert result.ready is (blocked is None)
    assert result.blocked_reasons == (() if blocked is None else (blocked,))


@pytest.mark.asyncio
async def test_remote_blocked_reasons_are_limited_to_our_vocabulary(tmp_path):
    """A node reports why it is not ready only in codes this control plane defines."""
    bootstrap, profile, transport, _ = setup(tmp_path)
    remote = ["kata_install_failed", "<img src=x onerror=alert(1)>", "y" * 10_000]
    transport.run.side_effect = [
        json.dumps(PREFLIGHT),
        json.dumps({"ready": False, "blocked_reasons": remote}),
    ]
    deployed = await bootstrap.deploy(NODE, profile, INTENT, SecretStr("token"))
    assert deployed.blocked_reasons == ("kata_install_failed",)
    transport.run.side_effect = [json.dumps({"ready": False, "blocked_reasons": remote})]
    checked = await bootstrap.readiness(NODE, profile, INTENT)
    assert checked.blocked_reasons == ("kata_install_failed",)
    transport.run.side_effect = [
        json.dumps({"ready": False, "blocked_reasons": "host_deployment_failed"})
    ]
    assert (await bootstrap.readiness(NODE, profile, INTENT)).blocked_reasons == ()


@pytest.mark.asyncio
async def test_machine_replacement_stops_bootstrap_even_when_address_unchanged(tmp_path):
    bootstrap, profile, transport, _ = setup(tmp_path)
    transport.run.side_effect = [json.dumps({**PREFLIGHT, "machine_identity": "replacement"})]
    result = await bootstrap.deploy(NODE, profile, INTENT, SecretStr("token"))
    assert not result.ready and "machine_identity_changed" in result.blocked_reasons
    assert transport.run.await_count == 1


@pytest.mark.asyncio
async def test_profile_cannot_copy_management_credentials_or_widen_isolation(tmp_path):
    bootstrap, profile, transport, secrets = setup(tmp_path)
    secrets["deployment:env"] += "\nAGENT_SANDBOX_BACKEND=container"
    with pytest.raises(ProviderError, match="security boundary"):
        await bootstrap.deploy(NODE, profile, INTENT, SecretStr("token"))
    assert transport.run.await_count == 1
    bootstrap, profile, transport, _ = setup(tmp_path)
    profile = profile.model_copy(
        update={"files": {".compute-secrets/api-token": "cloudlab-account-1"}}
    )
    with pytest.raises(ProviderError, match="dedicated deployment secrets"):
        await bootstrap.deploy(NODE, profile, INTENT, SecretStr("token"))
    assert transport.run.await_count == 1


@pytest.mark.asyncio
async def test_bundle_digest_and_archive_paths_are_verified_before_upload(tmp_path):
    bootstrap, profile, transport, _ = setup(tmp_path)
    profile = profile.model_copy(update={"source_sha256": "0" * 64})
    with pytest.raises(ProviderError, match="digest"):
        await bootstrap.deploy(NODE, profile, INTENT, SecretStr("token"))
    member = tarfile.TarInfo("outside")
    member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
    bootstrap, profile, transport, _ = setup(tmp_path, unsafe_member=member)
    with pytest.raises(ProviderError, match="unsafe path"):
        await bootstrap.deploy(NODE, profile, INTENT, SecretStr("token"))
    assert transport.run.await_count == 1


@pytest.mark.asyncio
async def test_the_bundle_is_repacked_without_holding_the_event_loop(tmp_path, monkeypatch):
    """API requests and Host heartbeats share this loop; packing a release must not stall them."""
    bootstrap, profile, _, _ = setup(tmp_path)
    packing, served = threading.Event(), threading.Event()
    answered: list[bool] = []
    original = tarfile.TarFile.addfile

    def addfile(self, *args, **kwargs):
        if not packing.is_set():
            packing.set()
            # Only the event loop can answer; packing on it would wait in vain.
            answered.append(served.wait(timeout=5))
        return original(self, *args, **kwargs)

    async def serve() -> None:
        while not packing.is_set():
            await asyncio.sleep(0.001)
        served.set()

    monkeypatch.setattr(tarfile.TarFile, "addfile", addfile)
    server = asyncio.create_task(serve())
    payload = await bootstrap._archive(profile, INTENT, SecretStr("token"))
    await asyncio.wait_for(server, timeout=5)
    assert answered == [True]
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        assert "deploy/host/sandbox_host.sh" in archive.getnames()


@pytest.mark.asyncio
async def test_inventory_includes_stopped_containers_and_orphans(tmp_path):
    bootstrap, profile, transport, _ = setup(tmp_path)
    transport.run.side_effect = [
        json.dumps(
            {
                "complete": True,
                "retained_containers": 1,
                "unknown_containers": 0,
                "orphan_state": True,
                "blocked_reasons": ["retained_containers_present", "orphan_state_present"],
            }
        )
    ]
    inventory = await bootstrap.inventory(NODE, profile, INTENT)
    assert inventory.complete and inventory.retained_containers == 1 and inventory.orphan_state
    command = transport.run.call_args.args[1]
    script = shlex.split(command)[4]
    assert "['docker', 'ps', '-aq']" in script
    assert "['docker', 'volume', 'ls', '-q']" in script


def test_remote_scripts_compile_and_deployment_refuses_existing_unowned_root(
    tmp_path, monkeypatch, capsys
):
    for script in (_PREFLIGHT, _DEPLOY, _READINESS, _INVENTORY):
        compile(script, "remote-bootstrap", "exec")
    root = tmp_path / "existing-host"
    root.mkdir()
    original = root / ".env"
    original.write_text("existing secret")
    params = {"root": str(root), "sha": "a" * 40, "install": True, "identity": {"host": "other"}}
    monkeypatch.setattr("sys.argv", ["remote", json.dumps(params)])
    # A refusal exits cleanly with its reason: the transport drops stderr and
    # reports any other exit only as an opaque remote failure.
    with pytest.raises(SystemExit) as refused:
        exec(_DEPLOY, {})
    assert refused.value.code == 0
    assert json.loads(capsys.readouterr().out) == {
        "ready": False,
        "blocked_reasons": ["deployment_identity_conflict"],
    }
    assert original.read_text() == "existing secret"


def _deploy_refusal(tmp_path, monkeypatch, capsys, root, archive: bytes) -> dict:
    from types import SimpleNamespace

    params = {"root": str(root), "sha": "a" * 40, "install": False, "identity": {"host": "h"}}
    monkeypatch.setattr("sys.argv", ["remote", json.dumps(params)])
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(archive)))
    with pytest.raises(SystemExit) as refused:
        exec(_DEPLOY, {})
    assert refused.value.code == 0
    return json.loads(capsys.readouterr().out)


def _payload(member: tarfile.TarInfo) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.addfile(member, io.BytesIO(b"") if member.isfile() else None)
    return buffer.getvalue()


def test_remote_deployment_reports_each_unsafe_state_as_a_reason(tmp_path, monkeypatch, capsys):
    """Unsafe paths and archives are named, so an operator need not log in to tell them apart."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(elsewhere)
    harmless = _payload(tarfile.TarInfo("pyproject.toml"))
    assert _deploy_refusal(tmp_path, monkeypatch, capsys, linked_root, harmless) == {
        "ready": False,
        "blocked_reasons": ["unsafe_deployment_path"],
    }
    lock_root = tmp_path / "locked"
    (tmp_path / ".locked.compute-lock").symlink_to(elsewhere / "lock")
    assert _deploy_refusal(tmp_path, monkeypatch, capsys, lock_root, harmless) == {
        "ready": False,
        "blocked_reasons": ["unsafe_deployment_path"],
    }
    link = tarfile.TarInfo("escape")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    assert _deploy_refusal(tmp_path, monkeypatch, capsys, tmp_path / "archive", _payload(link)) == {
        "ready": False,
        "blocked_reasons": ["invalid_deployment_archive"],
    }
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / ".compute-identity.json").write_text(json.dumps({"host": "h"}))
    (planted / "deploy").symlink_to(elsewhere)
    nested = _payload(tarfile.TarInfo("deploy/host/sandbox_host.sh"))
    assert _deploy_refusal(tmp_path, monkeypatch, capsys, planted, nested) == {
        "ready": False,
        "blocked_reasons": ["unsafe_deployment_path"],
    }
    assert not any(elsewhere.iterdir())


@pytest.mark.parametrize("install", [False, True])
def test_remote_deployment_retry_identity_and_enrollment_cleanup(
    tmp_path, monkeypatch, capsys, install
):
    import subprocess
    from types import SimpleNamespace

    root = tmp_path / "managed"
    params = {
        "root": str(root),
        "sha": "a" * 40,
        "install": install,
        "identity": {"host": "managed"},
    }
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        data = b"A='keep'\nAGENT_ENROLL_TOKEN='spent-token'\nB='keep-too'\n"
        member = tarfile.TarInfo(".env")
        member.size, member.mode = len(data), 0o600
        archive.addfile(member, io.BytesIO(data))
    monkeypatch.setattr("sys.argv", ["remote", json.dumps(params)])
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("subprocess.run", run)
    for _ in range(2):
        monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(payload.getvalue())))
        exec(_DEPLOY, {})
    assert (root / ".deployed-sha").read_text() == "a" * 40 + "\n"
    assert (root / ".env").read_text() == "A='keep'\nB='keep-too'\n"
    assert (root / ".env").stat().st_mode & 0o777 == 0o600
    assert calls.count(["bash", "deploy/host/sandbox_host.sh", "up"]) == 2
    installations = [args for args in calls if args[:2] == ["apt-get", "install"]]
    assert len(installations) == (2 if install else 0)
    for args in installations:
        # The host recipe uses Buildx's --provenance flag and extracts Kata's
        # zstd archive; Ubuntu's Docker and Compose packages do not supply them.
        assert {"docker-buildx", "zstd"}.issubset(args)
        assert calls.index(args) < calls.index(["bash", "deploy/host/sandbox_host.sh", "up"])
    assert all(json.loads(line)["ready"] for line in capsys.readouterr().out.splitlines())


def _release_payload() -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        data = b"A='keep'\nAGENT_ENROLL_TOKEN='spent-token'\n"
        member = tarfile.TarInfo(".env")
        member.size, member.mode = len(data), 0o600
        archive.addfile(member, io.BytesIO(data))
    return payload.getvalue()


def test_same_owner_advances_the_release_only_after_the_recipe_succeeds(
    tmp_path, monkeypatch, capsys
):
    import subprocess
    from types import SimpleNamespace

    root = tmp_path / "managed"
    root.mkdir()
    old = {
        "allocation_id": "alloc",
        "node_id": "node",
        "host": "managed",
        "deployment_sha": "a" * 40,
    }
    (root / ".compute-identity.json").write_text(json.dumps(old))
    new = {**old, "deployment_sha": "b" * 40}
    params = {"root": str(root), "sha": "b" * 40, "install": False, "identity": new}
    monkeypatch.setattr("sys.argv", ["remote", json.dumps(params)])
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(_release_payload())))
    monkeypatch.setattr(
        "subprocess.run", lambda args, **kwargs: subprocess.CompletedProcess(args, 0)
    )
    exec(_DEPLOY, {})
    assert json.loads((root / ".compute-identity.json").read_text()) == new
    assert (root / ".deployed-sha").read_text() == "b" * 40 + "\n"
    assert json.loads(capsys.readouterr().out)["ready"] is True


@pytest.mark.parametrize("still_advertising", [True, False])
def test_failed_rollout_keeps_the_previous_marker(tmp_path, monkeypatch, capsys, still_advertising):
    import subprocess
    from types import SimpleNamespace

    root = tmp_path / "managed"
    root.mkdir()
    old = {
        "allocation_id": "alloc",
        "node_id": "node",
        "host": "managed",
        "deployment_sha": "a" * 40,
    }
    (root / ".compute-identity.json").write_text(json.dumps(old))
    if still_advertising:
        (root / ".shared-live-ready").write_text("a" * 40 + "\n")
    params = {
        "root": str(root),
        "sha": "b" * 40,
        "install": False,
        "identity": {**old, "deployment_sha": "b" * 40},
    }
    monkeypatch.setattr("sys.argv", ["remote", json.dumps(params)])
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(_release_payload())))

    def run(args, **kwargs):
        code = 1 if args == ["bash", "deploy/host/sandbox_host.sh", "up"] else 0
        return subprocess.CompletedProcess(args, code)

    monkeypatch.setattr("subprocess.run", run)
    try:
        exec(_DEPLOY, {})
    except SystemExit as exc:
        assert exc.code in (0, None)
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "ready": False,
        "blocked_reasons": ["host_deployment_failed"],
        "advertising": still_advertising,
    }
    assert json.loads((root / ".compute-identity.json").read_text()) == old
    assert not (root / ".deployed-sha").exists()


def test_profile_rejects_system_root_or_traversal():
    kwargs = {
        "id": "id",
        "label": "label",
        "source_bundle": "/bundle",
        "source_sha256": "a" * 64,
        "deployment_sha": "b" * 40,
        "environment_ref": "deployment:env",
    }
    with pytest.raises(ValidationError):
        DeploymentProfile(**kwargs, deploy_root="/opt")
    with pytest.raises(ValidationError):
        DeploymentProfile(**kwargs, files={"../outside": "deployment:file"})


@pytest.mark.asyncio
async def test_checked_operator_example_loads_and_renders_real_bootstrap_payload(
    tmp_path, monkeypatch
):
    """The shipped runbook profile must satisfy the actual deployment boundary."""
    from cloud_agent.compute_config import ComputeConfiguration

    repo = Path(__file__).resolve().parents[2]
    document = json.loads((repo / "deploy/control-plane/compute.example.json").read_text())
    _, bundle_profile, _, _ = setup(tmp_path)
    document["deployment_profiles"][0].update(
        source_bundle=bundle_profile.source_bundle,
        source_sha256=bundle_profile.source_sha256,
        deployment_sha=bundle_profile.deployment_sha,
    )
    for name in document["deployment_secrets"]:
        secret_path = tmp_path / name
        secret_path.write_text(
            (repo / "deploy/control-plane/compute-host.env.example").read_text()
            if name == "pilot-env"
            else "trusted-test-tunnel-material"
        )
        document["deployment_secrets"][name] = str(secret_path)
    config_path = tmp_path / "compute.json"
    config_path.write_text(json.dumps(document))
    monkeypatch.setenv("AGENT_COMPUTE_CONFIG", str(config_path))
    configuration = ComputeConfiguration.from_env()
    resolver = AsyncMock()
    resolver.resolve.side_effect = lambda ref: Path(
        configuration.deployment_secrets[ref.removeprefix("deployment:")]
    ).read_text()
    bootstrap = SshHostBootstrap(transport=AsyncMock(), resolver=resolver)
    payload = await bootstrap._archive(configuration.profiles["pilot"], INTENT, SecretStr("once"))
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        env = dotenv_values(
            stream=io.StringIO(archive.extractfile(".env").read().decode()), interpolate=False
        )
        assert env["AGENT_MCP_TUNNEL_REMOTE_PORT"] == "8001"
        assert env["AGENT_CONTROL_PLANE_TUNNEL_REMOTE_PORT"] == "8000"
        assert env["AGENT_TUNNEL_SSH_KEY"] == "/opt/cloud-agent-pilot/.compute-secrets/gateway.key"
        assert env["AGENT_SANDBOX_MANAGER_REVERSE_PORT"] == "19092"
        assert "{" not in archive.extractfile(".env").read().decode()
        secrets = [m for m in archive.getmembers() if m.name.startswith(".compute-secrets/")]
        assert len(secrets) == 6
        # The secrets above were written without a final newline, as an Admin
        # request delivers them; OpenSSH cannot read a key in that form.
        for member in secrets:
            assert archive.extractfile(member).read() == b"trusted-test-tunnel-material\n"


@pytest.mark.parametrize(
    "extra",
    [
        None,
        "other-host/credential",
        "other-host/.enroll.lock",
        "managed-host-1/nested/credential",
        "managed-host-1/workspace.txt",
        "symlink",
    ],
)
def test_remote_inventory_only_exempts_exact_current_host_enrollment_files(
    tmp_path, monkeypatch, capsys, extra
):
    """Run the shipped inventory script against isolated real filesystem state."""
    import pathlib

    filesystem = tmp_path / "machine"
    state = filesystem / "var/lib/cloud-agent" / INTENT.host
    state.mkdir(parents=True)
    (state / "credential").write_text("persisted enrollment")
    (state / ".enroll.lock").touch()
    if extra == "symlink":
        (state / "credential").unlink()
        (state / "credential").symlink_to(tmp_path / "unobserved")
    elif extra:
        unexpected = state.parent / extra
        unexpected.parent.mkdir(parents=True, exist_ok=True)
        unexpected.write_text("retained or unmanaged")
    original_path = pathlib.Path

    def isolated_path(value):
        value = str(value)
        return (
            filesystem / value.lstrip("/")
            if value.startswith("/var/lib/")
            else original_path(value)
        )

    params = {
        "root": str(tmp_path / "no-deploy-yet"),
        "identity": {"host": INTENT.host, "node_id": INTENT.node_id},
    }
    monkeypatch.setattr("sys.argv", ["remote", json.dumps(params)])
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/docker")
    # The complete Docker inventory is empty; only on-disk state distinguishes
    # the safe fresh Host from another Host's unobserved retained files.
    monkeypatch.setattr("subprocess.check_output", lambda args, **kwargs: "")
    with monkeypatch.context() as local:
        local.setattr("pathlib.Path", isolated_path)
        exec(_INVENTORY, {})
    result = json.loads(capsys.readouterr().out)
    assert result["complete"] is True
    assert result["orphan_state"] is (extra is not None)
    assert ("orphan_state_present" in result["blocked_reasons"]) is (extra is not None)


def _run_inventory(tmp_path, monkeypatch, capsys, recorded=None) -> dict:
    """Run the shipped inventory script over ``tmp_path / "machine"`` with empty Docker.

    ``recorded`` arrives on standard input, as ``SshHostBootstrap.inventory`` sends it.
    """
    import pathlib

    filesystem = tmp_path / "machine"
    original_path = pathlib.Path

    def isolated_path(value):
        value = str(value)
        return (
            filesystem / value.lstrip("/")
            if value.startswith("/var/lib/")
            else original_path(value)
        )

    params = {
        "root": str(tmp_path / "no-deploy-yet"),
        "identity": {"host": INTENT.host, "node_id": INTENT.node_id},
    }
    monkeypatch.setattr("sys.argv", ["remote", json.dumps(params)])
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(recorded or {"checkpoints": {}, "removals": {}}))
    )
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/docker")
    monkeypatch.setattr("subprocess.check_output", lambda args, **kwargs: "")
    with monkeypatch.context() as local:
        local.setattr("pathlib.Path", isolated_path)
        exec(_INVENTORY, {})
    result = json.loads(capsys.readouterr().out)
    assert result["complete"] is True
    return result


def _host_archives(tmp_path) -> Path:
    """The Sandbox Manager's archive state, as this Host's Compose file mounts it."""
    state = tmp_path / "machine/var/lib/cloud-agent" / INTENT.host
    state.mkdir(parents=True)
    (state / "credential").write_text("persisted enrollment")
    return state / "sandbox-archives"


#: The checkpoint fields the control plane records, as ``recorded_host_state`` returns them.
_CHECKPOINT_FIELDS = (
    "checkpoint_id",
    "sandbox_id",
    "generation",
    "container_id",
    "base_image",
    "runtime_id",
    "runtime_version",
    "object_key",
    "version_id",
    "sha256",
    "size_bytes",
    "new_layer_bytes",
    "layer_count",
    "format_version",
    "start_seq",
)

#: What the control plane recorded of the removal ``_discarded`` carries out.
_REMOVAL = {
    "thread_id": "thread-origin",
    "sandbox_id": "sandbox-1",
    "generation": 2,
    "container_id": "container-1",
}


def _saved_checkpoint(archives: Path, sandbox_id: str = "asbx_one") -> dict:
    """Have the Host's checkpoint store keep a receipt; return what the control plane recorded."""
    from tests.unit.test_sandbox_checkpoints import _receipt

    from cloud_agent_host.sandbox_checkpoints import SandboxCheckpointStore

    receipt = _receipt(sandbox_id)
    SandboxCheckpointStore(archives / "checkpoints").save(receipt)
    return {key: receipt.as_dict()[key] for key in _CHECKPOINT_FIELDS}


def _discarded(
    archives: Path, request_id: str, *, finish: bool = True, absent: bool = False
) -> None:
    """Remove one stopped container through the Host's own discard, leaving its receipts.

    With ``absent`` the container is already gone, and the discard proves it.
    """
    from tests.unit.test_sandbox_archive import _Manager

    from cloud_agent_host.chat_sandbox import ChatSandboxSpec
    from cloud_agent_host.sandbox_archive import ArchiveIdentity, SandboxArchiveService
    from cloud_agent_host.shared_session_supervisor import (
        SharedChatSessionSupervisor,
        SharedMemberManifest,
    )

    spec = ChatSandboxSpec(
        thread_id="thread-origin", sandbox_id="sandbox-1", generation=2, runtime_id="claude-code"
    )
    manager = _Manager(spec)
    supervisor = SharedChatSessionSupervisor(manager)
    if absent:
        manager.current = None
    else:
        supervisor.register_manifest(
            spec,
            [
                SharedMemberManifest(
                    thread_id="thread-origin", native_session_id="native", resident_slot_id=0
                )
            ],
            expected_container_id="container-1",
        )
    service = SandboxArchiveService(supervisor, state_dir=archives)
    identity = ArchiveIdentity("thread-origin", request_id, 2, "container-1", "")
    service.discard(identity, sandbox_id="sandbox-1")
    assert manager.removals == (0 if absent else 1)
    if not finish:
        (service._directory(identity) / "discarded.json").unlink()


def test_inventory_excuses_the_checkpoint_receipt_the_control_plane_recorded(
    tmp_path, monkeypatch, capsys
):
    """A Host keeps each sandbox's latest checkpoint receipt; its data is in object storage."""
    archives = _host_archives(tmp_path)
    expected = _saved_checkpoint(archives)

    recorded = {"checkpoints": {"asbx_one": expected}, "removals": {}}
    assert _run_inventory(tmp_path, monkeypatch, capsys, recorded)["orphan_state"] is False
    assert _run_inventory(tmp_path, monkeypatch, capsys)["orphan_state"] is True


@pytest.mark.parametrize(
    "changed",
    [
        {"checkpoint_id": "f" * 32},
        {"generation": 4},
        {"container_id": "container-other"},
        {"object_key": "cloud-agent/sandbox/v2/other.oci.tar"},
        {"version_id": "v2"},
        {"sha256": "f" * 64},
        {"size_bytes": 1},
        {"start_seq": 7},
    ],
)
def test_inventory_counts_a_checkpoint_receipt_that_differs_from_the_record(
    tmp_path, monkeypatch, capsys, changed
):
    archives = _host_archives(tmp_path)
    expected = _saved_checkpoint(archives)

    recorded = {"checkpoints": {"asbx_one": {**expected, **changed}}, "removals": {}}
    assert _run_inventory(tmp_path, monkeypatch, capsys, recorded)["orphan_state"] is True


@pytest.mark.parametrize("kept", [("sandbox_id", "checkpoint_id"), ()])
def test_inventory_counts_a_checkpoint_receipt_missing_what_was_recorded(
    tmp_path, monkeypatch, capsys, kept
):
    archives = _host_archives(tmp_path)
    expected = _saved_checkpoint(archives)
    path = archives / "checkpoints" / (hashlib.sha256(b"asbx_one").hexdigest() + ".json")
    stored = json.loads(path.read_text())
    path.write_text(json.dumps({key: stored[key] for key in kept}))

    recorded = {"checkpoints": {"asbx_one": expected}, "removals": {}}
    assert _run_inventory(tmp_path, monkeypatch, capsys, recorded)["orphan_state"] is True


def test_inventory_never_excuses_a_receipt_filed_under_another_sandbox(
    tmp_path, monkeypatch, capsys
):
    archives = _host_archives(tmp_path)
    expected = _saved_checkpoint(archives)
    one = archives / "checkpoints" / (hashlib.sha256(b"asbx_one").hexdigest() + ".json")
    one.rename(one.with_name(hashlib.sha256(b"asbx_two").hexdigest() + ".json"))

    recorded = {"checkpoints": {"asbx_two": {**expected, "sandbox_id": "asbx_two"}}, "removals": {}}
    assert _run_inventory(tmp_path, monkeypatch, capsys, recorded)["orphan_state"] is True


@pytest.mark.parametrize("absent", [False, True])
def test_inventory_excuses_a_confirmed_removals_finished_receipts(
    tmp_path, monkeypatch, capsys, absent
):
    """Both forms the Host writes: the container it removed, or one it proved gone."""
    archives = _host_archives(tmp_path)
    _discarded(archives, "superseded-1", absent=absent)

    recorded = {"checkpoints": {}, "removals": {"superseded-1": _REMOVAL}}
    assert _run_inventory(tmp_path, monkeypatch, capsys, recorded)["orphan_state"] is False


@pytest.mark.parametrize(
    ("removals", "finish", "extra"),
    [
        ({}, True, None),
        ({"superseded-2": _REMOVAL}, True, None),
        # The Host recorded the request but never finished it.
        ({"superseded-1": _REMOVAL}, False, None),
        ({"superseded-1": _REMOVAL}, True, "archive.tar"),
        ({"superseded-1": {**_REMOVAL, "thread_id": "thread-other"}}, True, None),
        ({"superseded-1": {**_REMOVAL, "generation": 999}}, True, None),
        ({"superseded-1": {**_REMOVAL, "container_id": "container-other"}}, True, None),
        ({"superseded-1": {**_REMOVAL, "sandbox_id": "sandbox-other"}}, True, None),
    ],
)
def test_inventory_counts_receipts_of_any_other_removal(
    tmp_path, monkeypatch, capsys, removals, finish, extra
):
    archives = _host_archives(tmp_path)
    _discarded(archives, "superseded-1", finish=finish)
    if extra:
        directory = archives / hashlib.sha256(b"superseded-1").hexdigest()
        (directory / extra).write_text("retained data")

    result = _run_inventory(
        tmp_path, monkeypatch, capsys, {"checkpoints": {}, "removals": removals}
    )
    assert result["orphan_state"] is True


@pytest.mark.parametrize(
    "names", [("discard.json",), ("discarded.json",), ("discard.json", "discarded.json")]
)
@pytest.mark.parametrize(
    "rewrite",
    [
        # Another removal, the receipts agreeing with each other where both change.
        lambda receipt: {
            **receipt,
            "identity": {**receipt["identity"], "generation": 999, "container_id": "other"},
        },
        lambda receipt: {"identity": {"request_id": receipt["identity"]["request_id"]}},
        lambda receipt: {**receipt, "sandbox_id": "sandbox-1", "target_absent": True},
    ],
)
def test_inventory_counts_receipts_that_do_not_record_the_removal(
    tmp_path, monkeypatch, capsys, names, rewrite
):
    archives = _host_archives(tmp_path)
    _discarded(archives, "superseded-1")
    directory = archives / hashlib.sha256(b"superseded-1").hexdigest()
    for name in names:
        path = directory / name
        path.write_text(json.dumps(rewrite(json.loads(path.read_text()))))

    recorded = {"checkpoints": {}, "removals": {"superseded-1": _REMOVAL}}
    assert _run_inventory(tmp_path, monkeypatch, capsys, recorded)["orphan_state"] is True


def test_inventory_counts_a_discard_whose_spec_names_another_group(tmp_path, monkeypatch, capsys):
    archives = _host_archives(tmp_path)
    _discarded(archives, "superseded-1")
    path = archives / hashlib.sha256(b"superseded-1").hexdigest() / "discard.json"
    started = json.loads(path.read_text())
    started["spec"]["sandbox_id"] = "sandbox-other"
    path.write_text(json.dumps(started))

    recorded = {"checkpoints": {}, "removals": {"superseded-1": _REMOVAL}}
    assert _run_inventory(tmp_path, monkeypatch, capsys, recorded)["orphan_state"] is True


def test_inventory_never_excuses_receipts_filed_under_another_request(
    tmp_path, monkeypatch, capsys
):
    archives = _host_archives(tmp_path)
    _discarded(archives, "superseded-other")
    (archives / hashlib.sha256(b"superseded-other").hexdigest()).rename(
        archives / hashlib.sha256(b"superseded-1").hexdigest()
    )

    recorded = {"checkpoints": {}, "removals": {"superseded-1": _REMOVAL}}
    assert _run_inventory(tmp_path, monkeypatch, capsys, recorded)["orphan_state"] is True


@pytest.mark.asyncio
async def test_inventory_records_travel_on_stdin_through_a_real_ssh_client_process(
    tmp_path, monkeypatch
):
    """A Host with a long history still gets a command line of fixed length.

    Only the ``ssh`` binary is replaced: the real bootstrap and transport
    start it as a subprocess, and it keeps what it was given.
    """
    from cloud_agent.ports.external.host_bootstrap import RecordedHostState
    from cloud_agent.providers.compute.ssh import OpenSshTransport

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "seen"
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        f'for last; do :; done; printf "%s" "$last" > {seen}.command\n'
        f"cat > {seen}.stdin\n"
        """printf '{"complete": true}'\n"""
    )
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    resolver = AsyncMock()
    resolver.resolve.side_effect = lambda ref: (
        "PRIVATE KEY\n" if ref == "ssh-1" else "node.example.org ssh-ed25519 VERIFIED\n"
    )
    _, profile, _, _ = setup(tmp_path)
    bootstrap = SshHostBootstrap(transport=OpenSshTransport(resolver), resolver=resolver)

    async def command_for(groups: int) -> tuple[int, dict]:
        recorded = RecordedHostState(
            checkpoints={
                f"asbx_{i}": {"checkpoint_id": f"{i:032x}", "sandbox_id": f"asbx_{i}"}
                for i in range(groups)
            },
            removals={f"superseded-{i:064x}": dict(_REMOVAL) for i in range(groups)},
        )
        result = await bootstrap.inventory(NODE, profile, INTENT, recorded)
        assert result.complete
        sent = json.loads((tmp_path / "seen.stdin").read_text())
        assert sent == recorded.model_dump(mode="json")
        return len((tmp_path / "seen.command").read_text()), sent

    small, _ = await command_for(1)
    large, sent = await command_for(5000)
    assert len(sent["checkpoints"]) == len(sent["removals"]) == 5000
    assert large == small < 64 * 1024


def test_inventory_still_counts_an_administrators_archive_request(tmp_path, monkeypatch, capsys):
    """Archive-before-delete receipts are not a confirmed removal, whatever their id."""
    archives = _host_archives(tmp_path)
    directory = archives / hashlib.sha256(b"delete-1").hexdigest()
    directory.mkdir(parents=True)
    for name in ("prepare.json", "upload.json", "commit.json", "deleted.json"):
        (directory / name).write_text(json.dumps({"receipt": {}}))

    recorded = {"checkpoints": {}, "removals": {"delete-1": _REMOVAL}}
    assert _run_inventory(tmp_path, monkeypatch, capsys, recorded)["orphan_state"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "UNRELATED_DATABASE_PASSWORD",
        "AGENT_COMPUTE_SECRET_KEY",
        "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY",
        "AGENT_CONTROL_TOKEN_SECRET",
        "AGENT_SESSION_SECRET",
    ],
)
async def test_deployment_environment_rejects_unrelated_and_control_plane_authority(tmp_path, name):
    bootstrap, profile, transport, secrets = setup(tmp_path)
    secrets["deployment:env"] += f"\n{name}=must-not-leave-control-plane"
    with pytest.raises(ProviderError, match="security boundary"):
        await bootstrap.deploy(NODE, profile, INTENT, SecretStr("enrollment"))
    assert transport.run.await_count == 1


@pytest.mark.asyncio
async def test_a_template_can_name_the_sandbox_image_published_for_the_deployed_commit(tmp_path):
    bootstrap, profile, transport, secrets = setup(tmp_path)
    secrets["deployment:env"] += (
        "\nAGENT_SANDBOX_IMAGE=ghcr.io/harvardmadsys/cloud-agent-sandbox:sandbox-{deployment_sha}"
        "\nAGENT_SANDBOX_IMAGE_PULL=true"
    )

    result = await bootstrap.deploy(NODE, profile, INTENT, SecretStr("one-time-enrollment"))

    assert result.ready
    stdin = transport.run.call_args.kwargs["stdin"]
    with tarfile.open(fileobj=io.BytesIO(stdin), mode="r:gz") as archive:
        env = dotenv_values(
            stream=io.StringIO(archive.extractfile(".env").read().decode()), interpolate=False
        )
    assert env["AGENT_SANDBOX_IMAGE"] == (
        "ghcr.io/harvardmadsys/cloud-agent-sandbox:sandbox-" + profile.deployment_sha
    )
    assert env["AGENT_SANDBOX_IMAGE_PULL"] == "true"
