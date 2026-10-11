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
    for directory in ("etc", "dev", "var/lib", "mydata"):
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
    )
    original_path = pathlib.Path
    original_samefile = os.path.samefile

    def logical(value):
        return str(value).replace(str(root), "")

    def isolated_path(value="", *rest):
        value = str(value)
        if value.startswith(("/etc/", "/dev/", "/var/lib", "/mydata", "/alias")):
            return original_path(str(root) + value, *rest)
        return original_path(value, *rest)

    def samefile(source, target):
        if machine.bind_sources.get(logical(target)) == logical(source):
            return True
        return original_samefile(source, target)

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
        if command in (
            ["docker", "ps", "-aq"],
            ["docker", "image", "ls", "-aq"],
            ["docker", "volume", "ls", "-q"],
        ):
            code, output = machine.inventories.get(tuple(command), (0, ""))
            return subprocess.CompletedProcess(command, code, output, "")
        raise AssertionError(f"Preflight must only issue read-only commands: {command}")

    monkeypatch.setattr("pathlib.Path", isolated_path)
    monkeypatch.setattr("os.path.samefile", samefile)
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


def inspect(disk, monkeypatch, capsys, *, selected="/mydata"):
    before = snapshot(disk.root)
    monkeypatch.setattr("sys.argv", ["preflight", json.dumps({"data_mount": selected})])
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
def test_existing_store_mount_must_match_the_selected_disk(disk, monkeypatch, capsys, source):
    target = disk.root / "var/lib/containerd"
    target.mkdir()
    (target / "retained-state").write_text("keep")
    disk.mounted.add("/var/lib/containerd")
    if source != "missing":
        (disk.root / "mydata/containerd").mkdir()
    disk.bind_sources["/var/lib/containerd"] = (
        "/mydata/containerd" if source == "correct" else "/other-disk/containerd"
    )
    checks = inspect(disk, monkeypatch, capsys)
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
    "command",
    [["docker", "ps", "-aq"], ["docker", "image", "ls", "-aq"], ["docker", "volume", "ls", "-q"]],
)
@pytest.mark.parametrize("inventory", [(0, "retained-object"), (1, "")])
def test_nonempty_docker_store_requires_complete_empty_inventories(
    disk, monkeypatch, capsys, command, inventory
):
    target = disk.root / "var/lib/docker"
    target.mkdir()
    (target / "engine-metadata").write_text("initial daemon metadata")
    disk.inventories[tuple(command)] = inventory
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]


def test_initial_docker_metadata_with_empty_inventories_can_use_the_disk(disk, monkeypatch, capsys):
    target = disk.root / "var/lib/docker"
    target.mkdir()
    (target / "engine-metadata").write_text("initial daemon metadata")
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"] and checks["data_mount_conflict_free"]
    assert ["docker", "volume", "ls", "-q"] in disk.calls


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
