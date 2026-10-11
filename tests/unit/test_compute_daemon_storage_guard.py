"""Custom daemon storage is refused before a node's workload can be touched."""

from __future__ import annotations

import json
import shlex

import pytest
from tests.unit.test_compute_host_rotation_scripts import (
    deploy_params,
    machine as machine,
    payload,
    run_remote,
)
from tests.unit.test_compute_storage_preflight import disk as disk, inspect, snapshot

from cloud_agent.providers.compute.host_bootstrap import _DEPLOY

UNSUPPORTED = [
    ("docker", "", '{"data-root":"/custom/docker"}', None),
    ("docker", "", '{"graph":"/custom/docker"}', None),
    ("docker", "--data-root=/custom/docker", None, None),
    ("docker", "--data-root /custom/docker", None, None),
    ("docker", "--graph=/custom/docker", None, None),
    ("docker", "-g/custom/docker", None, None),
    ("docker", "-g=/custom/docker", None, None),
    ("docker", "-Dg/custom/docker", None, None),
    (
        "docker",
        "--config-file=/etc/docker/custom.json",
        '{"data-root":"/custom/docker"}',
        "/etc/docker/custom.json",
    ),
    ("docker", "$DOCKER_OPTS", None, None),
    ("docker", "--containerd=/custom/containerd.sock", None, None),
    ("docker", "", '{"containerd":"/custom/containerd.sock"}', None),
    ("docker", "", "not valid JSON", None),
    ("containerd", "", 'version = 2\nroot = "/custom/containerd"\n', None),
    ("containerd", "--root=/custom/containerd", None, None),
    ("containerd", "-r /custom/containerd", None, None),
    (
        "containerd",
        "--config /etc/containerd/custom.toml",
        'root = "/custom/containerd"\n',
        "/etc/containerd/custom.toml",
    ),
    ("containerd", "", 'imports = ["/etc/containerd/custom.toml"]\n', None),
    ("containerd", "--address=/custom/containerd.sock", None, None),
    ("containerd", "--address /custom/containerd.sock", None, None),
    ("containerd", "-a /custom/containerd.sock", None, None),
    ("containerd", "-a=/custom/containerd.sock", None, None),
    ("containerd", "-a/custom/containerd.sock", None, None),
    ("containerd", "", '[grpc]\naddress = "/custom/containerd.sock"\n', None),
    ("containerd", "", 'grpc.address = "/custom/containerd.sock"\n', None),
    (
        "containerd",
        "",
        '[plugins."io.containerd.grpc.v1.cri"]\nsandbox_image="pause"\n'
        '["grpc"]\n"address" = "/custom/containerd.sock"\n',
        None,
    ),
    ("containerd", "", 'grpc = {address = "/custom/containerd.sock"}\n', None),
]


def configure(node, service, flags="", config=None, config_path=None, *, running=False):
    daemon = "dockerd" if service == "docker" else "containerd"
    argv = ["/usr/bin/" + daemon, *shlex.split(flags)]
    unit = node.units[service]
    unit["ExecStart"] = (
        f"{{ path=/usr/bin/{daemon} ; argv[]={shlex.join(argv)} ; ignore_errors=no ; }}"
    )
    if config is not None:
        path = node.root / (
            config_path
            or ("/etc/docker/daemon.json" if service == "docker" else "/etc/containerd/config.toml")
        ).lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(config)
    if running:
        pid = "123" if service == "docker" else "456"
        unit["MainPID"] = pid
        process = node.root / "proc" / pid
        process.mkdir(parents=True)
        (process / "cmdline").write_bytes(b"\0".join(word.encode() for word in argv) + b"\0")
    retained = node.root / "custom-data/retained-workspace"
    retained.parent.mkdir(parents=True, exist_ok=True)
    retained.write_text("user data outside the default directories")


@pytest.mark.parametrize("service,flags,config,config_path", UNSUPPORTED)
@pytest.mark.parametrize("running", [False, True])
def test_preflight_rejects_custom_or_uninspectable_daemon_storage_read_only(
    disk, monkeypatch, capsys, service, flags, config, config_path, running
):
    configure(disk, service, flags, config, config_path, running=running)
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]
    assert not any(command[0] == "docker" for command in disk.calls)
    assert not any(
        command[:2] in (["systemctl", "start"], ["systemctl", "stop"]) for command in disk.calls
    )


@pytest.mark.parametrize("service,flags,config,config_path", UNSUPPORTED)
@pytest.mark.parametrize("running", [False, True])
def test_deploy_rejects_custom_daemon_storage_before_any_write_or_service_mutation(
    tmp_path, machine, monkeypatch, capsys, service, flags, config, config_path, running
):
    configure(machine, service, flags, config, config_path, running=running)
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


@pytest.mark.parametrize(
    "service,flag,value",
    [
        ("docker", "--data-root", "/custom/data"),
        ("containerd", "--root", "/custom/data"),
        ("containerd", "--address", "/custom/containerd.sock"),
    ],
)
def test_current_process_storage_is_checked_as_well_as_restart_configuration(
    disk, monkeypatch, capsys, service, flag, value
):
    configure(disk, service, running=True)
    pid = disk.units[service]["MainPID"]
    daemon = "dockerd" if service == "docker" else "containerd"
    (disk.root / "proc" / pid / "cmdline").write_bytes(
        f"/usr/bin/{daemon}\0{flag}={value}\0".encode()
    )
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]


@pytest.mark.parametrize(
    "service,flags,config,config_path",
    [
        ("docker", "-D -H fd:// --containerd=/run/containerd/containerd.sock", "{}", None),
        ("docker", "--data-root /var/lib/docker/", None, None),
        ("docker", "-g=/var/lib/docker", None, None),
        (
            "docker",
            "--config-file=/etc/docker/custom.json",
            '{"data-root":"/var/lib/docker"}',
            "/etc/docker/custom.json",
        ),
        (
            "containerd",
            "--root /var/lib/containerd",
            'version=2\nroot="/var/lib/containerd"\nimports=[]\n',
            None,
        ),
        ("containerd", "--address=/run/containerd/containerd.sock", None, None),
        ("containerd", "--address /var/run/containerd/containerd.sock", None, None),
        ("containerd", "-a /run/containerd/containerd.sock", None, None),
        ("containerd", "-a=/var/run/containerd/containerd.sock", None, None),
        ("containerd", "-a/run/containerd/containerd.sock", None, None),
        (
            "containerd",
            "",
            'version=2\nroot="/var/lib/containerd"\n[grpc] # default socket\n'
            'address="/run/containerd/containerd.sock"\n'
            '[plugins."io.containerd.grpc.v1.cri"]\nsandbox_image="pause"\n',
            None,
        ),
        ("containerd", "", 'grpc.address="/var/run/containerd/containerd.sock"\n', None),
        (
            "containerd",
            "",
            '[plugins."io.containerd.grpc.v1.cri"]\nsandbox_image="pause"\n'
            "[ 'grpc' ]\n'address'='/run/containerd/containerd.sock'\n",
            None,
        ),
    ],
)
def test_stock_daemon_units_and_literal_default_storage_configuration_are_supported(
    disk, monkeypatch, capsys, service, flags, config, config_path
):
    configure(disk, service, flags, config, config_path, running=True)
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"] and checks["data_mount_conflict_free"]


@pytest.mark.parametrize("install", [False, True])
def test_missing_services_are_supported_only_when_installation_is_authorized(
    disk, monkeypatch, capsys, install
):
    for unit in disk.units.values():
        unit.update(LoadState="not-found", ExecStart="", MainPID="0")
    checks = inspect(disk, monkeypatch, capsys, install=install)
    assert checks["data_mount_available"]
    assert checks["data_mount_conflict_free"] is install


def test_shell_wrapped_service_is_refused_without_running_its_command(disk, monkeypatch, capsys):
    disk.units["docker"]["ExecStart"] = (
        "{ path=/bin/sh ; argv[]=/bin/sh -c '/usr/bin/dockerd -g/custom/data' ; "
        "ignore_errors=no ; }"
    )
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]
    assert not any(command[0] in ("sh", "/bin/sh", "docker") for command in disk.calls)


def test_fresh_node_configuration_is_checked_before_installing_missing_daemons(
    disk, monkeypatch, capsys
):
    for unit in disk.units.values():
        unit.update(LoadState="not-found", ExecStart="", MainPID="0")
    (disk.root / "etc/docker/daemon.json").write_text(json.dumps({"data-root": "/custom/data"}))
    checks = inspect(disk, monkeypatch, capsys)
    assert checks["data_mount_available"]
    assert not checks["data_mount_conflict_free"]


def test_deployment_pins_docker_to_the_inspected_local_daemon(
    tmp_path, machine, monkeypatch, capsys
):
    monkeypatch.setenv("DOCKER_HOST", "tcp://unrelated-daemon.example:2376")
    monkeypatch.setenv("DOCKER_CONTEXT", "unrelated-context")
    for key in ("DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        monkeypatch.setenv(key, "remote-client-value")
    result = run_remote(
        _DEPLOY,
        deploy_params(tmp_path / "deploy", install=False),
        monkeypatch,
        capsys,
        stdin=payload(),
    )
    assert result["ready"]
    assert machine.environments
    for environment in machine.environments:
        assert environment["DOCKER_HOST"] == "unix:///var/run/docker.sock"
        assert environment["DOCKER_CONTEXT"] == "default"
        assert not any(
            key in environment for key in ("DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
        )
