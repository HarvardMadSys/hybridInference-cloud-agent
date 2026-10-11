"""Persistent daemon mounts cannot override selected storage on the next boot."""

from __future__ import annotations

import subprocess

import pytest
from tests.unit.test_compute_host_rotation_scripts import (
    deploy_params,
    machine as machine,
    payload,
    run_remote,
)
from tests.unit.test_compute_storage_preflight import disk as disk, inspect, snapshot

from cloud_agent.providers.compute.host_bootstrap import _DEPLOY


def fstab_entry(store, kind="matching"):
    selected = f"/mydata/{store} /var/lib/{store} none bind 0 0\n"
    escaped = r"\144ocker" if store == "docker" else r"\143ontainerd"
    return {
        "matching": selected,
        "foreign": f"/other-disk/{store} /var/lib/{store} none bind 0 0\n",
        "duplicate": selected + selected.replace(" ", "\t").rstrip() + " # duplicate\n",
        "escaped_target": f"/other-disk/{store} /var/lib/{escaped} none bind 0 0\n",
        "target_traversal": selected.replace(f"/var/lib/{store}", f"/var/lib/sub/../{store}"),
        "target_double_slash": selected.replace("/var/lib/", "//var//lib/"),
        "source_traversal": selected.replace(f"/mydata/{store}", f"/mydata/sub/../{store}"),
        "mount_type": selected.replace(" none ", " ext4 "),
        "readonly": selected.replace(" bind ", " bind,ro "),
        "noauto": selected.replace(" bind ", " bind,noauto "),
        "foreign_dependency": selected.replace(
            " bind ", " bind,x-systemd.requires-mounts-for=/other-disk "
        ),
        "whitespace": selected.replace(
            " none bind ", " none defaults,rw,bind,nofail,x-systemd.requires-mounts-for=/mydata "
        )
        .replace(" ", "\t")
        .rstrip()
        + " # existing operator entry\n",
        "escaped_matching": f"/mydata/{escaped} /var/lib/{escaped} none bind 0 0\n",
        "trailing_slash": selected.replace(f"/{store} ", f"/{store}/ "),
    }[kind]


CONFLICTS = [
    "foreign",
    "duplicate",
    "escaped_target",
    "target_traversal",
    "target_double_slash",
    "source_traversal",
    "mount_type",
    "readonly",
    "noauto",
    "foreign_dependency",
]


@pytest.mark.parametrize("store", ["docker", "containerd"])
@pytest.mark.parametrize("kind", CONFLICTS)
def test_inactive_fstab_conflicts_are_reported_by_read_only_preflight(
    disk, monkeypatch, capsys, store, kind
):
    (disk.root / "etc/fstab").write_text(fstab_entry(store, kind))
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]


@pytest.mark.parametrize("store", ["docker", "containerd"])
@pytest.mark.parametrize("kind", CONFLICTS)
def test_inactive_fstab_conflicts_block_deployment_before_any_write(
    tmp_path, machine, monkeypatch, capsys, store, kind
):
    (machine.root / "etc/fstab").write_text(fstab_entry(store, kind))
    before = snapshot(machine.root)
    root = tmp_path / "deploy"
    result = run_remote(_DEPLOY, deploy_params(root), monkeypatch, capsys, stdin=payload())
    assert result == {"ready": False, "blocked_reasons": ["data_mount_conflict"]}
    assert snapshot(machine.root) == before
    assert not root.exists()
    assert not (tmp_path / ".deploy.compute-lock").exists()
    assert all(
        command[0] == "mountpoint" or command[:2] == ["systemctl", "show"]
        for command in machine.calls
    )


@pytest.mark.parametrize("kind", ["matching", "whitespace", "escaped_matching", "trailing_slash"])
def test_matching_fstab_entries_remain_byte_for_byte_unchanged_on_staging_and_retry(
    tmp_path, machine, monkeypatch, capsys, kind
):
    current = "# existing operator configuration\nUUID=other /boot ext4 defaults 0 2\n\n"
    current += "".join(fstab_entry(store, kind) for store in ("docker", "containerd"))
    fstab = machine.root / "etc/fstab"
    fstab.write_text(current)
    for store in ("docker", "containerd"):
        machine.units[f"var-lib-{store}.mount"].update(
            LoadState="loaded", FragmentPath=f"/run/systemd/generator/var-lib-{store}.mount"
        )
    for _ in range(2):
        result = run_remote(
            _DEPLOY,
            deploy_params(tmp_path / "deploy", activate=False, install=False),
            monkeypatch,
            capsys,
            stdin=payload(),
        )
        assert result["ready"]
        assert fstab.read_text() == current
    assert len([call for call in machine.calls if call[:2] == ["mount", "--bind"]]) == 2


def test_new_fstab_entries_append_without_rewriting_existing_operator_configuration(
    tmp_path, machine, monkeypatch, capsys
):
    original = "# operator comments and spacing\n\nUUID=other\t/boot\text4\tdefaults 0 2"
    fstab = machine.root / "etc/fstab"
    fstab.write_text(original)
    result = run_remote(
        _DEPLOY,
        deploy_params(tmp_path / "deploy", activate=False, install=False),
        monkeypatch,
        capsys,
        stdin=payload(),
    )
    assert result["ready"]
    assert fstab.read_text().startswith(original + "\n")
    assert len(fstab.read_text().splitlines()) == len(original.splitlines()) + 2


def mount_override(node, store, kind):
    unit = f"var-lib-{store}.mount"
    properties = node.units[unit]
    if kind == "native":
        properties.update(LoadState="loaded", FragmentPath=f"/etc/systemd/system/{unit}")
    elif kind == "masked":
        properties.update(LoadState="masked", FragmentPath="/dev/null")
    elif kind == "dropin":
        properties.update(
            LoadState="loaded",
            FragmentPath=f"/run/systemd/generator/{unit}",
            DropInPaths=f"/etc/systemd/system/{unit}.d/override.conf",
        )
    else:
        directory = {
            "dormant_vendor": "usr/lib/systemd/system",
            "dormant_control": "etc/systemd/system.control",
        }.get(kind, "etc/systemd/system")
        path = node.root / directory / unit
        path.parent.mkdir(parents=True)
        if kind == "dormant_mask":
            path.symlink_to("/dev/null")
        elif kind in ("dormant_dropin", "dormant_control"):
            path = path.parent / (unit + ".d") / "override.conf"
            path.parent.mkdir()
            path.write_text("[Mount]\nWhat=/other-disk/docker\n")
        else:
            path.write_text("[Mount]\nWhat=/other-disk/docker\nWhere=/var/lib/docker\n")


OVERRIDES = [
    "native",
    "masked",
    "dropin",
    "dormant",
    "dormant_mask",
    "dormant_dropin",
    "dormant_vendor",
    "dormant_control",
]


@pytest.mark.parametrize("store", ["docker", "containerd"])
@pytest.mark.parametrize("kind", OVERRIDES)
def test_native_masked_or_overridden_daemon_mount_units_block_read_only_preflight(
    disk, monkeypatch, capsys, store, kind
):
    mount_override(disk, store, kind)
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]


@pytest.mark.parametrize("store", ["docker", "containerd"])
@pytest.mark.parametrize("kind", OVERRIDES)
def test_native_masked_or_overridden_daemon_mount_units_block_before_deployment_writes(
    tmp_path, machine, monkeypatch, capsys, store, kind
):
    mount_override(machine, store, kind)
    before = snapshot(machine.root)
    result = run_remote(
        _DEPLOY, deploy_params(tmp_path / "deploy"), monkeypatch, capsys, stdin=payload()
    )
    assert result == {"ready": False, "blocked_reasons": ["data_mount_conflict"]}
    assert snapshot(machine.root) == before
    assert not (tmp_path / "deploy").exists()


@pytest.mark.parametrize(
    "fragment",
    ["", "/run/systemd/generator", "/run/systemd/generator.early", "/run/systemd/generator.late"],
)
def test_synthesized_manual_or_fstab_generated_mount_units_are_supported(
    disk, monkeypatch, capsys, fragment
):
    for store in ("docker", "containerd"):
        disk.units[f"var-lib-{store}.mount"].update(
            LoadState="loaded", FragmentPath=f"{fragment}/var-lib-{store}.mount" if fragment else ""
        )
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"] and checks["data_mount_conflict_free"]


@pytest.mark.parametrize("kind", ["fstab", "native"])
def test_persistent_configuration_is_rechecked_before_writing_new_bind_entries(
    tmp_path, machine, monkeypatch, capsys, kind
):
    original_run = machine.run
    foreign = fstab_entry("docker", "foreign")

    def run(args, **kwargs):
        result = original_run(args, **kwargs)
        if args[:2] == ["mount", "--bind"] and not getattr(machine, "injected", False):
            machine.injected = True
            if kind == "fstab":
                (machine.root / "etc/fstab").write_text(foreign)
            else:
                mount_override(machine, "docker", "dormant")
        return result

    monkeypatch.setattr("subprocess.run", run)
    result = run_remote(
        _DEPLOY, deploy_params(tmp_path / "deploy"), monkeypatch, capsys, stdin=payload()
    )
    assert result == {"ready": False, "blocked_reasons": ["data_mount_conflict"]}
    assert machine.active == {"docker": True, "containerd": True}
    assert ["systemctl", "daemon-reload"] not in machine.calls
    assert not any(call[0] == "apt-get" for call in machine.calls)
    if kind == "fstab":
        assert (machine.root / "etc/fstab").read_text() == foreign
    else:
        assert not (machine.root / "etc/fstab").exists()


def partial_install(machine):
    machine.units["docker"].update(LoadState="not-found", ExecStart="", MainPID="0")
    machine.active = {"containerd": True}


def test_partial_prerequisite_install_stops_only_the_available_active_daemon(
    tmp_path, machine, monkeypatch, capsys
):
    partial_install(machine)
    original_run = machine.run

    def run(args, **kwargs):
        result = original_run(args, **kwargs)
        if args[:2] == ["systemctl", "stop"]:
            # A real multi-unit stop errors when a named unit does not exist.
            return subprocess.CompletedProcess(args, 0 if args[2:] == ["containerd"] else 5)
        return result

    monkeypatch.setattr("subprocess.run", run)
    result = run_remote(
        _DEPLOY,
        deploy_params(tmp_path / "deploy", activate=False),
        monkeypatch,
        capsys,
        stdin=payload(),
    )
    assert result["ready"]
    assert [call for call in machine.calls if call[:2] == ["systemctl", "stop"]] == [
        ["systemctl", "stop", "containerd"]
    ]


@pytest.mark.parametrize("partial", [False, True])
def test_failed_stop_restores_exactly_the_originally_active_units(
    tmp_path, machine, monkeypatch, capsys, partial
):
    if partial:
        partial_install(machine)
    original_active = machine.active.copy()
    original_run = machine.run

    def run(args, **kwargs):
        result = original_run(args, **kwargs)
        if args[:2] == ["systemctl", "stop"]:
            return subprocess.CompletedProcess(args, 1)
        return result

    monkeypatch.setattr("subprocess.run", run)
    result = run_remote(
        _DEPLOY, deploy_params(tmp_path / "deploy"), monkeypatch, capsys, stdin=payload()
    )
    assert result == {"ready": False, "blocked_reasons": ["data_mount_failed"]}
    assert machine.active == original_active
    assert ["systemctl", "start", *original_active] in machine.calls
    assert not any(call[0] in ("mount", "apt-get") for call in machine.calls)
