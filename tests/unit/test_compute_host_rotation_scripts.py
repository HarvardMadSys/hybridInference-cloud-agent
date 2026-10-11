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
    _STOP,
    _TRANSFER_KEY,
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
            else:
                out = "\n".join(self.containers)
        elif args[:3] == ["docker", "image", "ls"]:
            out = "\n".join(self.images)
        elif args[:3] == ["docker", "volume", "ls"]:
            out = "\n".join(self.volumes)
        elif args[:2] == ["docker", "stop"]:
            stopped = set(args[2:])
            self.runner = [c for c in self.runner if c not in stopped]
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


def test_stop_halts_the_runner_first_then_every_container_then_the_daemons(
    tmp_path, machine, monkeypatch, capsys
):
    root = tmp_path / "deploy"
    root.mkdir()
    (root / ".compute-identity.json").write_text(json.dumps(IDENTITY))
    machine.runner = ["runner-1"]
    machine.containers = ["runner-1", "chat-1", "broker-1"]
    params = {"root": str(root), "project": "ca-nodea", "identity": IDENTITY}
    result = run_remote(_STOP, params, monkeypatch, capsys)
    assert result == {"stopped": True, "reason": None}
    stops = [
        call for call in machine.calls if call[:2] in (["docker", "stop"], ["systemctl", "stop"])
    ]
    assert stops == [
        ["docker", "stop", "-t", "60", "runner-1"],
        ["docker", "stop", "chat-1", "broker-1"],
        ["systemctl", "stop", "docker.socket", "docker", "containerd"],
    ]
    runner_query = next(
        call for call in machine.calls if "label=com.docker.compose.service=runner" in call
    )
    assert "label=com.docker.compose.project=ca-nodea" in runner_query
    # The shim check matches process names, never this script's own command line.
    assert ["pgrep", "-x", "containerd-shim"] in machine.calls


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
    assert keys == ["restrict ssh-ed25519 KEY ca-transfer"]
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


async def test_reenrollment_requires_a_token():
    bootstrap = SshHostBootstrap(transport=AsyncMock(), resolver=AsyncMock())
    with pytest.raises(ProviderError, match="fresh token"):
        await bootstrap.deploy(SOURCE, PROFILE, INTENT, None, reenroll=True)
