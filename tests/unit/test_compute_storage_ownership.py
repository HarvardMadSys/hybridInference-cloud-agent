"""Selected daemon stores must already belong to the deployment before reuse."""

from __future__ import annotations

import json

import pytest
from tests.unit.test_compute_host_rotation_scripts import (
    IDENTITY,
    deploy_params,
    machine as machine,
    payload,
    run_remote,
)
from tests.unit.test_compute_storage_preflight import disk as disk, inspect, snapshot

from cloud_agent.providers.compute.host_bootstrap import _DEPLOY


def source_state(node, store):
    state = node.root / f"mydata/{store}/retained-workspace"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_bytes(b"existing user workload state")
    return state


def bind_stores(node, binding="both"):
    for store in ("docker", "containerd"):
        for parent in ("mydata", "var/lib"):
            (node.root / parent / store).mkdir(parents=True, exist_ok=True)
        if binding == "both" or binding == store:
            node.mounted.add(f"/var/lib/{store}")
            node.bind_sources[f"/var/lib/{store}"] = f"/mydata/{store}"


def marker(root, kind, identity=IDENTITY):
    if kind == "missing":
        return
    root.mkdir()
    path = root / ".compute-identity.json"
    if kind == "symlink":
        copy = root / "copied-identity.json"
        copy.write_text(json.dumps(identity))
        path.symlink_to(copy)
    else:
        path.write_text(
            "invalid JSON"
            if kind == "malformed"
            else json.dumps({**identity, "host": "another-host"} if kind == "wrong" else identity)
        )


@pytest.mark.parametrize("store", ["docker", "containerd"])
@pytest.mark.parametrize("active", [False, True])
def test_public_preflight_rejects_nonempty_source_with_empty_default_targets_read_only(
    disk, monkeypatch, capsys, store, active
):
    source_state(disk, store)
    disk.daemon_active = active
    for name in ("docker", "containerd"):
        (disk.root / f"var/lib/{name}").mkdir()
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]
    assert disk.daemon_active is active
    assert not any(command[0] == "docker" for command in disk.calls)


@pytest.mark.parametrize("store", ["docker", "containerd"])
@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("existing_marker", ["missing", "matching"])
def test_unbound_nonempty_source_is_refused_before_writes_and_remains_refused_on_retry(
    tmp_path, machine, monkeypatch, capsys, store, active, existing_marker
):
    source_state(machine, store)
    for name in ("docker", "containerd"):
        (machine.root / f"var/lib/{name}").mkdir()
    machine.active = {"docker.socket": True, "docker": active, "containerd": active}
    root = tmp_path / "deploy"
    marker(root, existing_marker)
    before = snapshot(machine.root)
    original_root = snapshot(root) if root.exists() else None
    for _ in range(2):
        result = run_remote(_DEPLOY, deploy_params(root), monkeypatch, capsys, stdin=payload())
        assert result == {"ready": False, "blocked_reasons": ["data_mount_conflict"]}
        assert snapshot(machine.root) == before
        assert (snapshot(root) if root.exists() else None) == original_root
        assert not (tmp_path / ".deploy.compute-lock").exists()
    assert machine.active == {"docker.socket": True, "docker": active, "containerd": active}
    assert all(
        command[0] == "mountpoint" or command[:2] == ["systemctl", "show"]
        for command in machine.calls
    )


@pytest.mark.parametrize("existing_marker", ["missing", "wrong", "malformed", "symlink"])
def test_correct_existing_binds_without_matching_preexisting_identity_are_not_adopted(
    tmp_path, machine, monkeypatch, capsys, existing_marker
):
    for store in ("docker", "containerd"):
        source_state(machine, store)
    bind_stores(machine)
    root = tmp_path / "deploy"
    marker(root, existing_marker)
    before = snapshot(machine.root)
    original_root = snapshot(root) if root.exists() else None
    result = run_remote(_DEPLOY, deploy_params(root), monkeypatch, capsys, stdin=payload())
    assert result == {"ready": False, "blocked_reasons": ["data_mount_conflict"]}
    assert snapshot(machine.root) == before
    assert (snapshot(root) if root.exists() else None) == original_root
    assert not (tmp_path / ".deploy.compute-lock").exists()


@pytest.mark.parametrize("store", ["docker", "containerd"])
def test_marker_created_during_setup_cannot_authorize_newly_appearing_source_state(
    tmp_path, machine, monkeypatch, capsys, store
):
    original_run = machine.run
    state = machine.root / f"mydata/{store}/concurrent-workspace"

    def run(args, **kwargs):
        if args[:2] == ["systemctl", "stop"]:
            state.write_bytes(b"state appeared after the new marker was written")
        return original_run(args, **kwargs)

    monkeypatch.setattr("subprocess.run", run)
    root = tmp_path / "deploy"
    result = run_remote(_DEPLOY, deploy_params(root), monkeypatch, capsys, stdin=payload())
    assert result == {"ready": False, "blocked_reasons": ["data_mount_conflict"]}
    assert state.read_bytes() == b"state appeared after the new marker was written"
    assert machine.active == {"docker": True, "containerd": True}
    assert ["systemctl", "start", "docker", "containerd"] in machine.calls
    assert not any(call[0] in ("mount", "apt-get") for call in machine.calls)
    # The new marker still cannot adopt these unbound files on the next retry.
    assert json.loads((root / ".compute-identity.json").read_text()) == IDENTITY
    assert run_remote(_DEPLOY, deploy_params(root), monkeypatch, capsys, stdin=payload()) == result


@pytest.mark.parametrize("binding", ["both", "docker", "containerd"])
def test_internal_preflight_needs_exact_identity_and_both_existing_store_binds(
    disk, monkeypatch, capsys, binding
):
    for store in ("docker", "containerd"):
        source_state(disk, store)
    bind_stores(disk, binding)
    root = disk.root / "managed"
    marker(root, "matching")
    checks = inspect(
        disk, monkeypatch, capsys, deployment={"root": str(root), "identity": IDENTITY}
    )
    assert checks["data_mount_available"]
    assert checks["data_mount_conflict_free"] is (binding == "both")
    # The release stamp may advance. A different owner still cannot adopt the disk.
    advanced = {**IDENTITY, "deployment_sha": "b" * 40}
    advanced_checks = inspect(
        disk,
        monkeypatch,
        capsys,
        deployment={"root": str(root), "identity": advanced},
    )
    assert advanced_checks["data_mount_conflict_free"] is (binding == "both")
    # An initial attachment has no allocation identity even on a managed disk.
    assert not inspect(disk, monkeypatch, capsys)["data_mount_conflict_free"]


@pytest.mark.parametrize("activate,namespace", [(False, None), (True, None), (True, "ca-nodea")])
def test_staged_transfer_activation_and_rotation_retry_keep_owned_source_data(
    tmp_path, machine, monkeypatch, capsys, activate, namespace
):
    root = tmp_path / "deploy"
    identity = {**IDENTITY, **({"namespace": namespace} if namespace else {})}
    parameters = deploy_params(root, install=False, activate=False, identity=identity)
    assert run_remote(_DEPLOY, parameters, monkeypatch, capsys, stdin=payload())["ready"]
    # Transfer writes through the selected disk's existing daemon binds. Its
    # deployment marker belongs to the target and is not copied from the source.
    retained = [source_state(machine, store) for store in ("docker", "containerd")]
    for _ in range(2):
        result = run_remote(
            _DEPLOY,
            {**parameters, "activate": activate},
            monkeypatch,
            capsys,
            stdin=payload(),
        )
        assert result["ready"]
        assert all(state.read_bytes() == b"existing user workload state" for state in retained)
        assert json.loads((root / ".compute-identity.json").read_text()) == identity
    assert len([call for call in machine.calls if call[:2] == ["mount", "--bind"]]) == 2


def test_same_owner_can_advance_the_release_without_adopting_another_host(
    tmp_path, machine, monkeypatch, capsys
):
    for store in ("docker", "containerd"):
        source_state(machine, store)
    bind_stores(machine)
    root = tmp_path / "deploy"
    marker(root, "matching")
    new_identity = {**IDENTITY, "deployment_sha": "b" * 40}
    result = run_remote(
        _DEPLOY,
        deploy_params(root, sha="b" * 40, identity=new_identity),
        monkeypatch,
        capsys,
        stdin=payload(),
    )
    assert result["ready"] is True
    assert json.loads((root / ".compute-identity.json").read_text()) == new_identity
    assert ["bash", "deploy/host/sandbox_host.sh", "up"] in machine.calls
    retained = machine.root / "mydata/docker/retained-workspace"
    assert retained.read_bytes() == b"existing user workload state"
