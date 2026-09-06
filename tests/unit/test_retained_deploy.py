"""Deployment checks for the retained/shared-live staging topology."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX_HOST = REPO_ROOT / "deploy/host/sandbox_host.sh"
HOST_DEPLOY = REPO_ROOT / "deploy/host/deploy.sh"
HOST_COMPOSE = REPO_ROOT / "deploy/host/docker-compose.sandbox.yml"
CONTROL_COMPOSE = REPO_ROOT / "deploy/control-plane/docker-compose.yml"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _host(
    tmp_path: Path,
    *,
    slots: str | None = "3",
    replicas: str = "3",
    running_replicas: int = 3,
    credential: bool = True,
    configured_root: str = "/var/lib/docker",
    daemon_root: str = "/var/lib/docker",
    capability: str = "shared-sandbox-v1,native-fork-at-anchor-v1",
    credential_rejected: bool = False,
) -> dict[str, str]:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    state_root = tmp_path / "host-state"
    state_root.mkdir()
    if credential:
        (state_root / "credential").write_text("host credential")

    dotenv = [
        "COMPOSE_PROJECT=test-retained",
        "AGENT_SANDBOX_BACKEND=container",
        "AGENT_SANDBOX_IMAGE=test-sandbox:latest",
        "AGENT_SHARED_RESIDENT_CAPACITY=8",
        f"AGENT_HOST_STATE_ROOT={state_root}",
        f"AGENT_RETAINED_DOCKER_DATA_ROOT={configured_root}",
    ]
    if slots is not None:
        dotenv.append(f"AGENT_HOST_SLOTS={slots}")
    dotenv.append(f"AGENT_RUNNER_REPLICAS={replicas}")
    (app_dir / ".env").write_text("\n".join([*dotenv, ""]))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    rejection_log = "agent_host_credential_rejected" if credential_rejected else ""
    runner_ids = "\n".join(f"runner-{index}" for index in range(1, running_replicas + 1))
    _write_executable(
        fake_bin / "docker",
        "#!/bin/sh\n"
        'case " $* " in *" config --environment "*) cat >/dev/null; { if [ -f .env ]; then cat .env; fi; env; } | awk -F= \'{v[$1]=$0} END {for (k in v) print v[k]}\'; exit 0 ;; esac\n'
        'case "$*" in\n'
        f"  'info --format {{{{.DockerRootDir}}}}') echo '{daemon_root}' ;;\n"
        "  'info') exit 0 ;;\n"
        f"  *' ps -q runner') printf '%s\\n' '{runner_ids}' ;;\n"
        f"  *' logs --since 2m runner') echo '{rejection_log}' ;;\n"
        f"  exec*'_shared_live_host_capabilities'*) echo '{capability}' ;;\n"
        f"  exec*'retained_docker_data_root'*) echo '{daemon_root}' ;;\n"
        "  exec*'AGENT_CONTROL_PLANE_URL'*) echo '200' ;;\n"
        "  run*) printf '200' ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "APP_DIR": str(app_dir),
    }
    # Exercise the same .env path an operator uses, independent of the test
    # process's own environment.
    for name in (
        "COMPOSE_PROJECT",
        "AGENT_SANDBOX_BACKEND",
        "AGENT_SANDBOX_IMAGE",
        "AGENT_HOST_SLOTS",
        "AGENT_RUNNER_REPLICAS",
        "AGENT_SHARED_RESIDENT_CAPACITY",
        "AGENT_CHAT_IDLE_TIMEOUT_S",
        "AGENT_HOST_STATE_ROOT",
        "AGENT_DEPLOYMENT_SHA",
        "AGENT_SHARED_LIVE_READY_FILE",
        "AGENT_ENROLL_TOKEN",
        "AGENT_RETAINED_DOCKER_DATA_ROOT",
        "SIBLING_COMPOSE_PROJECTS",
        "AGENT_EGRESS_NETWORK_PLATFORM_ONLY",
        "AGENT_EGRESS_NETWORK_TRUSTED",
        "AGENT_EGRESS_NETWORK_UPLINK",
        "AGENT_GATEWAY_HOSTNAME",
        "AGENT_MCP_HOSTNAME",
        "AGENT_CONTROL_PLANE_HOSTNAME",
        "AGENT_WORKSPACE_BROKER_TOKEN",
        "AGENT_TUNNEL_SSH_DESTINATION",
        "AGENT_TUNNEL_SSH_KEY",
        "AGENT_TUNNEL_SSH_KNOWN_HOSTS",
        "AGENT_MCP_TUNNEL_SSH_DESTINATION",
        "AGENT_MCP_TUNNEL_SSH_KEY",
        "AGENT_MCP_TUNNEL_SSH_KNOWN_HOSTS",
        "AGENT_CONTROL_PLANE_TUNNEL_SSH_DESTINATION",
        "AGENT_CONTROL_PLANE_TUNNEL_SSH_KEY",
        "AGENT_CONTROL_PLANE_TUNNEL_SSH_KNOWN_HOSTS",
        "AGENT_BROKER_REVERSE_BIND",
        "AGENT_BROKER_REVERSE_PORT",
        "AGENT_WORKSPACE_BROKER_ADVERTISE_URL",
    ):
        env.pop(name, None)
    return env


def _add_required_host_paths(env: dict[str, str], tmp_path: Path) -> None:
    """Populate the three relays and reverse broker path used by ``up``."""
    app_dir = Path(env["APP_DIR"])
    values = ["AGENT_WORKSPACE_BROKER_TOKEN=test-broker-token"]
    for index, prefix in enumerate(
        ("AGENT_TUNNEL", "AGENT_MCP_TUNNEL", "AGENT_CONTROL_PLANE_TUNNEL"),
        start=1,
    ):
        key = tmp_path / f"tunnel-{index}.key"
        known = tmp_path / f"tunnel-{index}.known-hosts"
        key.write_text(f"private key {index}")
        known.write_text(f"host key {index}")
        values.extend(
            (
                f"{prefix}_SSH_DESTINATION=agent-{index}@service-host",
                f"{prefix}_SSH_KEY={key}",
                f"{prefix}_SSH_KNOWN_HOSTS={known}",
            )
        )
    values.extend(
        (
            "AGENT_BROKER_REVERSE_BIND=172.24.0.1",
            "AGENT_BROKER_REVERSE_PORT=18092",
            "AGENT_WORKSPACE_BROKER_ADVERTISE_URL=http://172.24.0.1:18092",
        )
    )
    with (app_dir / ".env").open("a") as dotenv:
        dotenv.write("\n".join([*values, ""]))


def _run(env: dict[str, str], *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SANDBOX_HOST), *command],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_shared_live_is_the_only_deployment_mode() -> None:
    """No configuration can route a job back to one-shot execution."""
    sources = "\n".join(
        path.read_text() for path in (SANDBOX_HOST, HOST_DEPLOY, HOST_COMPOSE, CONTROL_COMPOSE)
    )
    for removed in (
        "AGENT_RESTARTABLE_SANDBOX",
        "AGENT_SHARED_LIVE_SANDBOX",
        "STAGING_SHARED_LIVE_SANDBOX",
    ):
        assert removed not in sources


def test_target_sha_marker_is_a_rollout_fence_not_a_topology_switch() -> None:
    """The marker can delay rollout but cannot select a legacy executor."""
    host_script = SANDBOX_HOST.read_text()
    host_deploy = HOST_DEPLOY.read_text()

    assert "AGENT_DEPLOYMENT_SHA" in host_deploy
    assert "shared-live-ready" in host_script
    assert "mark-ready" in host_script
    assert "AGENT_RESTARTABLE_SANDBOX" not in host_script
    assert "AGENT_SHARED_LIVE_SANDBOX" not in host_script


def test_host_builds_then_drains_runners_before_replacing_the_broker() -> None:
    script = SANDBOX_HOST.read_text()

    build = script.index("compose build")
    marker = script.index("prepare_shared_live_marker", build)
    drain = script.index("compose stop runner", marker)
    replace = script.index("compose up -d --no-build --remove-orphans", drain)

    assert build < marker < drain < replace


def test_direct_host_commands_support_environment_only_configuration(tmp_path: Path) -> None:
    """The direct operator path must not require a nonexistent optional .env."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "docker",
        "#!/bin/sh\ncase \"$*\" in\n  *'--env-file .env'*) exit 42 ;;\n  *'config --environment'*) cat >/dev/null; env; exit 0 ;;\n  *) exit 0 ;;\nesac\n",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "APP_DIR": str(app_dir),
        "COMPOSE_PROJECT": "env-only-host",
    }

    result = _run(env, "status")

    assert result.returncode == 0, result.stderr


def test_only_the_pending_target_commit_can_be_marked_ready(tmp_path: Path) -> None:
    env = _host(tmp_path)
    marker = tmp_path / "host-state/shared-live-ready"
    marker.write_text("pending:target-sha\n")

    wrong = _run(env, "mark-ready", "other-sha")
    assert wrong.returncode != 0
    assert marker.read_text().strip() == "pending:target-sha"

    ready = _run(env, "mark-ready", "target-sha")
    assert ready.returncode == 0, ready.stderr
    assert marker.read_text().strip() == "target-sha"


def test_host_deploy_publishes_readiness_only_after_recording_the_target() -> None:
    script = HOST_DEPLOY.read_text()
    deferred_up = script.index("DEFER_SHARED_READY_MARKER=1")
    deployed_sha = script.index("printf '%s\\n' \"${SHA}\" > .deployed-sha")
    mark_ready = script.index('bash deploy/host/sandbox_host.sh mark-ready "${SHA}"')

    assert 'AGENT_DEPLOYMENT_SHA="${SHA}"' in script
    assert deferred_up < deployed_sha < mark_ready


def test_shared_live_capacity_defaults_are_deployable(tmp_path: Path) -> None:
    result = _run(_host(tmp_path, slots=None), "preflight")

    assert result.returncode == 0, result.stderr
    assert "retained storage verified" in result.stdout


def test_host_env_example_has_one_value_per_key() -> None:
    """A later duplicate silently overrides the environment-specific value."""
    keys = [
        line.split("=", 1)[0]
        for line in (REPO_ROOT / "deploy/host/.env.example").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    ]

    assert len(keys) == len(set(keys))


def test_shared_live_refuses_an_invalid_chat_idle_timeout(tmp_path: Path) -> None:
    env = _host(tmp_path)
    app_dir = Path(env["APP_DIR"])
    with (app_dir / ".env").open("a") as dotenv:
        dotenv.write("AGENT_CHAT_IDLE_TIMEOUT_S=not-a-number\n")

    result = _run(env, "preflight")

    assert result.returncode != 0
    assert "AGENT_CHAT_IDLE_TIMEOUT_S must be a positive number" in result.stderr


def test_sibling_stacks_require_the_existing_closed_network_name(tmp_path: Path) -> None:
    """An automatic rename would either cross-wire environments or strand chats."""
    env = _host(tmp_path)
    app_dir = Path(env["APP_DIR"])
    with (app_dir / ".env").open("a") as dotenv:
        dotenv.write("SIBLING_COMPOSE_PROJECTS=other-environment\n")

    result = _run(env, "preflight")

    assert result.returncode != 0
    assert "AGENT_EGRESS_NETWORK_PLATFORM_ONLY must name" in result.stderr


def test_egress_phase_networks_must_remain_distinct(tmp_path: Path) -> None:
    env = _host(tmp_path)
    app_dir = Path(env["APP_DIR"])
    with (app_dir / ".env").open("a") as dotenv:
        dotenv.write(
            "AGENT_EGRESS_NETWORK_PLATFORM_ONLY=same-network\n"
            "AGENT_EGRESS_NETWORK_TRUSTED=same-network\n"
        )

    result = _run(env, "preflight")

    assert result.returncode != 0
    assert "must be three distinct names" in result.stderr


def test_scoped_relay_aliases_must_be_distinct(tmp_path: Path) -> None:
    """A shared Docker alias would randomly route a request to the wrong relay."""
    env = _host(tmp_path)
    app_dir = Path(env["APP_DIR"])
    with (app_dir / ".env").open("a") as dotenv:
        dotenv.write("AGENT_GATEWAY_HOSTNAME=same-relay\nAGENT_MCP_HOSTNAME=same-relay\n")

    result = _run(env, "preflight")

    assert result.returncode != 0
    assert "must be distinct Docker aliases" in result.stderr


@pytest.mark.parametrize(
    "missing",
    [
        "AGENT_WORKSPACE_BROKER_TOKEN",
        "AGENT_MCP_TUNNEL_SSH_KEY",
        "AGENT_CONTROL_PLANE_TUNNEL_SSH_KNOWN_HOSTS",
        "AGENT_BROKER_REVERSE_BIND",
        "AGENT_WORKSPACE_BROKER_ADVERTISE_URL",
    ],
)
def test_host_up_refuses_any_missing_scoped_or_reverse_path(tmp_path: Path, missing: str) -> None:
    env = _host(tmp_path)
    _add_required_host_paths(env, tmp_path)
    with (Path(env["APP_DIR"]) / ".env").open("a") as dotenv:
        dotenv.write(f"{missing}=\n")

    result = _run(env, "up")

    assert result.returncode != 0
    assert missing in result.stderr


@pytest.mark.parametrize("unsafe_bind", ["0.0.0.0", "127.0.0.1", "localhost", "::1"])
def test_host_up_refuses_an_unsafe_reverse_broker_bind(tmp_path: Path, unsafe_bind: str) -> None:
    env = _host(tmp_path)
    _add_required_host_paths(env, tmp_path)
    with (Path(env["APP_DIR"]) / ".env").open("a") as dotenv:
        dotenv.write(f"AGENT_BROKER_REVERSE_BIND={unsafe_bind}\n")

    result = _run(env, "up")

    assert result.returncode != 0
    assert "not the control-plane Docker bridge gateway" in result.stderr


def test_first_kata_deploy_builds_the_image_before_using_it_for_the_kernel_probe() -> None:
    """A clean host has no previous sandbox image for ``docker run`` to use."""
    script = SANDBOX_HOST.read_text()
    command = script[script.index("cmd_up() {") : script.index("cmd_check() {")]

    assert command.index("docker build") < command.index("kata_smoke_test")
    assert command.index("kata_smoke_test") < command.index("compose up")


def test_shared_live_refuses_more_slots_than_runner_executors(tmp_path: Path) -> None:
    result = _run(_host(tmp_path, slots="3", replicas="2"), "preflight")

    assert result.returncode != 0
    assert "overstates AGENT_RUNNER_REPLICAS=2" in result.stderr


def test_shared_live_check_requires_every_configured_runner_to_be_alive(tmp_path: Path) -> None:
    result = _run(_host(tmp_path, replicas="3", running_replicas=1), "check")

    assert result.returncode != 0
    assert "only 1 of 3 configured runner replicas are running" in result.stderr


def test_shared_live_check_runs_the_real_sandbox_preflight_before_attesting() -> None:
    """A running container is not ready while its claim loop is still probing."""
    script = SANDBOX_HOST.read_text()
    check = script[script.index("check_shared_live_stack() {") : script.index("# ── Isolation")]

    assert "backend.preflight()" in check
    assert check.index("backend.preflight()") < check.index("backend.version_stamp()")


def test_retained_mode_requires_an_enrolled_host_identity(tmp_path: Path) -> None:
    result = _run(_host(tmp_path, credential=False), "preflight")

    assert result.returncode != 0
    assert "AGENT_ENROLL_TOKEN or the persisted host credential" in result.stderr


def test_retained_mode_refuses_the_wrong_docker_data_root(tmp_path: Path) -> None:
    result = _run(
        _host(
            tmp_path,
            configured_root="/mnt/retained-docker",
            daemon_root="/var/lib/docker",
        ),
        "preflight",
    )

    assert result.returncode != 0
    assert "DockerRootDir is '/var/lib/docker'" in result.stderr
    assert "expects '/mnt/retained-docker'" in result.stderr


def test_shared_live_check_attests_every_runner_and_broker(tmp_path: Path) -> None:
    result = _run(_host(tmp_path), "check")

    assert result.returncode == 0, result.stderr
    assert "shared-live ready" in result.stdout
    assert "shared-sandbox-v1" in result.stdout
    assert "native-fork-at-anchor-v1" in result.stdout
    assert "MCP-only relay" in result.stdout


def test_host_readiness_probes_both_sandbox_relays() -> None:
    script = SANDBOX_HOST.read_text()
    check = script[script.index("cmd_check() {") : script.index("cmd_status() {")]
    probe = script[
        script.index("probe_sandbox_relay() {") : script.index("check_shared_live_stack() {")
    ]

    assert "GATEWAY_HOSTNAME" in check
    assert "MCP_HOSTNAME" in check
    assert check.count("probe_sandbox_relay") == 2
    assert "docker run --rm --network" in probe
    assert "curl -fsS" in probe
    assert '--runtime "$KATA_RUNTIME"' in probe


def test_host_up_waits_for_parallel_control_bootstrap_but_manual_check_is_immediate() -> None:
    script = SANDBOX_HOST.read_text()
    up = script[script.index("cmd_up() {") : script.index("cmd_check() {")]
    dispatch = script[script.rindex('case "${1:-}" in') :]

    assert 'readiness_wait="${readiness_wait:-900}"' in up
    assert '( cmd_check "$readiness_wait" )' in up
    assert "check) cmd_check 0" in dispatch


def test_shared_live_check_fails_when_runner_does_not_advertise(tmp_path: Path) -> None:
    result = _run(_host(tmp_path, capability=""), "check")

    assert result.returncode != 0
    assert "did not attest shared-sandbox-v1" in result.stderr


def test_shared_live_check_requires_historical_fork_attestation(tmp_path: Path) -> None:
    result = _run(_host(tmp_path, capability="shared-sandbox-v1"), "check")

    assert result.returncode != 0
    assert "did not attest native-fork-at-anchor-v1" in result.stderr


def test_shared_live_check_fails_for_a_rejected_host_credential(tmp_path: Path) -> None:
    result = _run(_host(tmp_path, credential_rejected=True), "check")

    assert result.returncode != 0
    assert "rejected this host's persisted credential" in result.stderr
