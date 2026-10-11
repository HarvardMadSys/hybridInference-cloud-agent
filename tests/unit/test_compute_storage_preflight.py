"""Execute the SSH preflight with isolated disks and read-only command fakes."""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cloud_agent.providers.compute.host_bootstrap import _PREFLIGHT


@pytest.fixture
def disk(tmp_path, monkeypatch):
    root = tmp_path / "node"
    for directory in ("etc/docker", "etc/containerd", "dev", "var/lib", "mydata", "proc"):
        (root / directory).mkdir(parents=True)
    (root / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
    (root / "etc/machine-id").write_text("stable-node")
    (root / "dev/kvm").touch()
    machine = SimpleNamespace(
        root=root,
        calls=[],
        mounted={"/mydata"},
        bind_sources={},
        inventories={},
        sudo_inspected=False,
        daemon_active=True,
        socket_active=True,
        daemon_root="/var/lib/docker",
        readonly=set(),
        units={
            service: {
                "LoadState": "loaded",
                "ExecStart": f"{{ path=/usr/bin/{daemon} ; argv[]=/usr/bin/{daemon} ; ignore_errors=no ; }}",
                "MainPID": "0",
            }
            for service, daemon in (("docker", "dockerd"), ("containerd", "containerd"))
        },
    )
    machine.units.update(
        {
            f"var-lib-{name}.mount": {
                "LoadState": "not-found",
                "FragmentPath": "",
                "DropInPaths": "",
            }
            for name in ("docker", "containerd")
        }
    )
    original_path = pathlib.Path
    original_samefile = os.path.samefile
    original_statvfs = os.statvfs

    def logical(value):
        return str(value).replace(str(root), "")

    def isolated_path(value="", *rest):
        value = str(value)
        if value.startswith(
            (
                "/etc/",
                "/dev/",
                "/var/lib",
                "/mydata",
                "/alias",
                "/proc",
                "/run/systemd/",
                "/usr/local/lib/systemd/",
                "/usr/lib/systemd/",
                "/lib/systemd/",
            )
        ):
            return original_path(str(root) + value, *rest)
        return original_path(value, *rest)

    def samefile(source, target):
        if machine.bind_sources.get(logical(target)) == logical(source):
            return True
        return original_samefile(source, target)

    def statvfs(path):
        if logical(path) in machine.readonly:
            return SimpleNamespace(f_flag=os.ST_RDONLY)
        return original_statvfs(path)

    def run(args, **kwargs):
        command = [logical(arg) for arg in args]
        machine.calls.append(command)
        if command[:4] == ["sudo", "-n", "python3", "-c"]:
            machine.sudo_inspected = True
            output = io.StringIO()
            with patch("sys.argv", ["storage", args[5]]), redirect_stdout(output):
                exec(args[4], {})
            return subprocess.CompletedProcess(command, 0, output.getvalue(), "")
        if command in (
            ["sudo", "-n", "true"],
            ["sudo", "-n", "docker", "compose", "version"],
        ):
            return subprocess.CompletedProcess(command, 0)
        if command[:2] == ["mountpoint", "-q"]:
            return subprocess.CompletedProcess(command, 0 if command[2] in machine.mounted else 1)
        if command[:2] == ["systemctl", "show"]:
            output = "\n".join(f"{key}={value}" for key, value in machine.units[command[2]].items())
            return subprocess.CompletedProcess(command, 0, output, "")
        if command in (
            ["docker", "ps", "-aq"],
            ["docker", "image", "ls", "-aq"],
            ["docker", "volume", "ls", "-q"],
        ):
            if not machine.daemon_active and machine.socket_active:
                machine.daemon_active = True
            code, output = machine.inventories.get(tuple(command), (0, ""))
            return subprocess.CompletedProcess(command, code, output, "")
        raise AssertionError(f"Preflight must only issue read-only commands: {command}")

    monkeypatch.setattr("pathlib.Path", isolated_path)
    monkeypatch.setattr("os.path.samefile", samefile)
    monkeypatch.setattr("os.statvfs", statvfs)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("subprocess.run", run)
    return machine


def snapshot(root):
    """Capture directories, links and file bytes to detect any preflight mutation."""
    return {
        str(path.relative_to(root)): (
            ("symlink", os.readlink(path))
            if path.is_symlink()
            else ("directory", None)
            if path.is_dir()
            else ("file", path.read_bytes())
        )
        for path in root.rglob("*")
    }


def inspect(disk, monkeypatch, capsys, *, selected="/mydata", install=True, deployment=None):
    before = snapshot(disk.root)
    parameters = {"data_mount": selected, "install_prerequisites": install}
    if deployment is not None:
        parameters["deployment"] = deployment
    monkeypatch.setattr(
        "sys.argv",
        ["preflight", json.dumps(parameters)],
    )
    exec(_PREFLIGHT, {})
    assert snapshot(disk.root) == before
    result = json.loads(capsys.readouterr().out)
    assert all(isinstance(value, bool) for value in result["checks"].values())
    return result["checks"]


def test_fresh_disk_preflight_succeeds_without_creating_store_directories(
    disk, monkeypatch, capsys
):
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"] and checks["data_mount_conflict_free"]
    assert not (disk.root / "mydata/docker").exists()
    assert not (disk.root / "var/lib/containerd").exists()


@pytest.mark.parametrize("state", ["missing", "unmounted", "file", "symlink", "parent_link"])
def test_selected_storage_must_be_an_existing_mount_without_symlinks(
    disk, monkeypatch, capsys, state
):
    base = disk.root / "mydata"
    selected = "/mydata"
    if state == "unmounted":
        disk.mounted.clear()
    elif state == "parent_link":
        (base / "volume").mkdir()
        (disk.root / "alias").symlink_to(base, target_is_directory=True)
        selected = "/alias/volume"
        disk.mounted.add(selected)
    else:
        base.rmdir()
        if state == "file":
            base.write_text("not a disk")
        elif state == "symlink":
            (disk.root / "alias").mkdir()
            base.symlink_to(disk.root / "alias", target_is_directory=True)
    checks = inspect(disk, monkeypatch, capsys, selected=selected)
    assert not checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]


@pytest.mark.parametrize("store", ["docker", "containerd"])
@pytest.mark.parametrize("location", ["source", "target"])
@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_store_path_conflicts_are_reported_without_mutation(
    disk, monkeypatch, capsys, store, location, kind
):
    path = disk.root / ("mydata" if location == "source" else "var/lib") / store
    if kind == "symlink":
        path.symlink_to(disk.root / "mydata", target_is_directory=True)
    else:
        path.write_text("unexpected file")
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]


@pytest.mark.parametrize("source", ["missing", "wrong", "correct"])
@pytest.mark.parametrize("store", ["docker", "containerd"])
def test_existing_store_mount_must_match_the_selected_disk(
    disk, monkeypatch, capsys, source, store
):
    target = disk.root / f"var/lib/{store}"
    target.mkdir()
    (target / "retained-state").write_text("keep")
    disk.mounted.add(f"/var/lib/{store}")
    if source != "missing":
        (disk.root / f"mydata/{store}").mkdir()
    disk.bind_sources[f"/var/lib/{store}"] = (
        f"/mydata/{store}" if source == "correct" else f"/other-disk/{store}"
    )
    other = "containerd" if store == "docker" else "docker"
    for directory in (disk.root / f"mydata/{other}", disk.root / f"var/lib/{other}"):
        directory.mkdir()
    disk.mounted.add(f"/var/lib/{other}")
    disk.bind_sources[f"/var/lib/{other}"] = f"/mydata/{other}"
    root = disk.root / "managed"
    root.mkdir()
    identity = {"host": "managed"}
    (root / ".compute-identity.json").write_text(json.dumps(identity))
    checks = inspect(
        disk, monkeypatch, capsys, deployment={"root": str(root), "identity": identity}
    )
    assert checks["data_mount_available"]
    assert checks["data_mount_conflict_free"] is (source == "correct")


def test_unmounted_containerd_state_blocks_even_when_docker_inventory_is_empty(
    disk, monkeypatch, capsys
):
    directory = disk.root / "var/lib/containerd/other-namespace"
    directory.mkdir(parents=True)
    (directory / "snapshots").write_text("retained user state")
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]


@pytest.mark.parametrize(
    ("daemon_active", "daemon_root"),
    [(True, "/var/lib/docker"), (False, "/var/lib/docker"), (True, "/other-disk/docker")],
)
def test_nonempty_unmounted_docker_store_is_preserved_without_contacting_any_daemon(
    disk, monkeypatch, capsys, daemon_active, daemon_root
):
    target = disk.root / "var/lib/docker"
    target.mkdir()
    (target / "retained-state").write_text("user state outside the active daemon")
    disk.daemon_active = daemon_active
    disk.daemon_root = daemon_root
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]
    assert disk.daemon_active is daemon_active
    assert not any(command[0] == "docker" for command in disk.calls)


def test_initial_docker_metadata_also_requires_explicit_operator_cleanup(disk, monkeypatch, capsys):
    target = disk.root / "var/lib/docker"
    target.mkdir()
    (target / "engine-metadata").write_text("initial daemon metadata")
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]
    assert not any(command[0] == "docker" for command in disk.calls)


@pytest.mark.parametrize(
    "readonly_path",
    ["/mydata", "/mydata/docker", "/mydata/containerd", "/var/lib/docker", "/var/lib/containerd"],
)
def test_readonly_disk_or_store_is_unavailable_without_mutation(
    disk, monkeypatch, capsys, readonly_path
):
    path = disk.root / readonly_path.lstrip("/")
    path.mkdir(parents=True, exist_ok=True)
    if readonly_path == "/var/lib/docker":
        (disk.root / "mydata/docker").mkdir()
        disk.mounted.add(readonly_path)
        disk.bind_sources[readonly_path] = "/mydata/docker"
    disk.readonly.add(readonly_path)
    checks = inspect(disk, monkeypatch, capsys)
    assert not checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]
    assert not any(command[0] in {"docker", "systemctl", "mount"} for command in disk.calls)


def test_management_user_inspects_root_owned_storage_through_sudo(disk, monkeypatch, capsys):
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"] and checks["data_mount_conflict_free"]
    assert disk.sudo_inspected


def test_legacy_preflight_without_a_storage_selection_keeps_its_original_checks(
    disk, monkeypatch, capsys
):
    checks = inspect(disk, monkeypatch, capsys, selected=None)
    assert set(checks) == {
        "supported_os",
        "architecture",
        "privileged_deploy",
        "kvm",
        "docker",
        "compose",
    }
    assert not disk.sudo_inspected
    assert not any(command[0] == "mountpoint" for command in disk.calls)
