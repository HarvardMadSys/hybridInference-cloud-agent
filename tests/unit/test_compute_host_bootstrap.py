"""Shared SSH deployment protects credentials, retained state and repeatability."""

from __future__ import annotations

import hashlib
import io
import json
import shlex
import tarfile
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
    env["AGENT_WORKSPACE_BROKER_TOKEN"] = "broker-secret-$literal"
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
async def test_vanilla_node_bootstrap_uses_one_recipe_and_secrets_stay_out_of_arguments(tmp_path):
    bootstrap, profile, transport, _ = setup(tmp_path)
    result = await bootstrap.deploy(NODE, profile, INTENT, SecretStr("one-time-enrollment"))
    assert result.ready
    deploy_call = transport.run.call_args
    command = deploy_call.args[1]
    assert "one-time-enrollment" not in command and "broker-secret" not in command
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
        assert env["AGENT_WORKSPACE_BROKER_TOKEN"] == "broker-secret-$literal"
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


def test_remote_scripts_compile_and_deployment_refuses_existing_unowned_root(tmp_path, monkeypatch):
    for script in (_PREFLIGHT, _DEPLOY, _READINESS, _INVENTORY):
        compile(script, "remote-bootstrap", "exec")
    root = tmp_path / "existing-host"
    root.mkdir()
    original = root / ".env"
    original.write_text("existing secret")
    params = {"root": str(root), "sha": "a" * 40, "install": True, "identity": {"host": "other"}}
    monkeypatch.setattr("sys.argv", ["remote", json.dumps(params)])
    with pytest.raises(SystemExit, match="identity conflict"):
        exec(_DEPLOY, {})
    assert original.read_text() == "existing secret"


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
        assert env["AGENT_BROKER_REVERSE_PORT"] == "19092"
        assert "{" not in archive.extractfile(".env").read().decode()
        assert len([m for m in archive.getmembers() if m.name.startswith(".compute-secrets/")]) == 6


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
