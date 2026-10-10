"""Deployment checks for the retained/shared-live staging topology."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
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
    running_containers: int = 1,
    turn_starts: bool = True,
    credential: bool = True,
    configured_root: str = "/var/lib/docker",
    daemon_root: str = "/var/lib/docker",
    capability: str = "shared-sandbox-v1,native-fork-at-anchor-v1",
    credential_rejected: bool = False,
    attest_exit: int = 0,
    broker_running: bool = True,
    broker_serving: bool = True,
    candidate_exit: int = 0,
    network_settings_exit: int = 0,
    storage_json: str | None = None,
    storage_exit: int = 0,
    storage_hang: bool = False,
    storage_ignores_term: bool = False,
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
    (app_dir / ".env").write_text("\n".join([*dotenv, ""]))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    rejection_log = "agent_host_credential_rejected" if credential_rejected else ""
    runner_ids = "\n".join(f"runner-{index}" for index in range(1, running_containers + 1))
    broker_id = "workspace-broker-1" if broker_running else ""
    # The readiness wait probes the broker with urllib; the fake answers it
    # separately from the attestation exec so a broker that is up but not yet
    # serving can be modelled.
    broker_probe_exit = 0 if broker_serving else 1
    # Only modelled when a test asks for it: the storage probe's describe
    # command is a second `docker exec`, and a fake that answered it by default
    # would make every deployment test exercise the attestation path.
    storage_case = ""
    if storage_hang and storage_ignores_term:
        # SIG_IGN survives `exec`, so this probe is the one GNU `timeout`'s
        # `-k` exists for: it outlives SIGTERM and has to be SIGKILLed.
        storage_case = "  exec*'host_capacity describe'*) trap '' TERM; exec sleep 300 ;;\n"
    elif storage_hang:
        # `exec` so the hang *is* this process: a killed `sleep` child would
        # leave the pipe into the parser open, and the test would measure that
        # instead of the bound.
        storage_case = "  exec*'host_capacity describe'*) exec sleep 300 ;;\n"
    elif storage_json is not None:
        storage_case = (
            "  exec*'host_capacity describe'*) printf '%s' '"
            + storage_json
            + "'; exit "
            + str(storage_exit)
            + " ;;\n"
        )
    turn_probe = "declined no turn was delivered" if turn_starts else "ImportError: no module"
    turn_probe_exit = 0 if turn_starts else 1
    _write_executable(
        fake_bin / "docker",
        "#!/bin/sh\n"
        'case " $* " in *" config --environment "*) cat >/dev/null; { if [ -f .env ]; then cat .env; fi; env; } | awk -F= \'{v[$1]=$0} END {for (k in v) print v[k]}\'; exit 0 ;; esac\n'
        'case "$*" in\n'
        f"  'info --format {{{{.DockerRootDir}}}}') echo '{daemon_root}' ;;\n"
        "  'info') exit 0 ;;\n"
        '  *\' config --format json\') echo \'{"name":"test-retained","services":{"runner":{"image":"test-candidate-runner"}}}\' ;;\n'
        f"  run*'cloud_agent_host.candidate_check') exit {candidate_exit} ;;\n"
        f"  run*'network_from_env()') exit {network_settings_exit} ;;\n"
        f"  *' ps -q runner') printf '%s\\n' '{runner_ids}' ;;\n"
        f"  *' ps -q workspace-broker') printf '%s\\n' '{broker_id}' ;;\n"
        f"  *' logs --since 2m runner') echo '{rejection_log}' ;;\n"
        f"  exec*'_shared_live_host_capabilities'*) echo '{capability}'; exit {attest_exit} ;;\n"
        + storage_case
        + f"  exec*8092/health*) exit {broker_probe_exit} ;;\n"
        # The realistic shape: the same preflight a real turn runs logs a Kata
        # measurement warning to stderr before the child answers on stdout,
        # and the script captures both. The verdict has to be read past it.
        "  exec*--turn*) echo '2026-09-17 06:18:18,846 - cloud_agent_host.chat_sandbox - WARNING - Kata guest cannot measure part of a member window: storage_io' >&2; "
        f"echo '{turn_probe}'; exit {turn_probe_exit} ;;\n"
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
        "AGENT_RELAY_NETWORK",
        "AGENT_EGRESS_CHAT_NETWORK_POOL",
        "AGENT_EGRESS_DNS_SERVER",
        "AGENT_EGRESS_PROTECTED_CIDRS",
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
        "AGENT_HOST_STORAGE_PROBE",
        # Runtime image defaults are reviewed source pins. A launcher's own
        # CLI version must not become an operator override in this fixture.
        "CLAUDE_CODE_VERSION",
        "CODEX_VERSION",
        "PI_VERSION",
        "OPENCODE_VERSION",
        "KILO_VERSION",
        "DOCKER_DAEMON_JSON",
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
            "AGENT_EGRESS_CHAT_NETWORK_POOL=10.240.0.0/16",
            "AGENT_EGRESS_DNS_SERVER=1.1.1.1",
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
    marker = Path(env["APP_DIR"]) / ".shared-live-ready"
    marker.write_text("pending:target-sha\n")

    wrong = _run(env, "mark-ready", "other-sha")
    assert wrong.returncode != 0
    assert marker.read_text().strip() == "pending:target-sha"

    ready = _run(env, "mark-ready", "target-sha")
    assert ready.returncode == 0, ready.stderr
    assert marker.read_text().strip() == "target-sha"


def test_deploy_marker_does_not_require_writable_enrollment_state(tmp_path: Path) -> None:
    """Container-owned credentials must not block an unprivileged rollout."""
    env = _host(tmp_path)
    _add_required_host_paths(env, tmp_path)
    _write_executable(tmp_path / "bin/sleep", "#!/bin/sh\nexit 0\n")
    env.update(AGENT_DEPLOYMENT_SHA="target-sha", DEFER_SHARED_READY_MARKER="1")
    state_root = tmp_path / "host-state"
    legacy_marker = state_root / "shared-live-ready"
    legacy_marker.write_text("previous-generation\n")
    legacy_marker.chmod(0o444)
    state_root.chmod(0o555)
    credential = state_root / "credential"
    credential_before = credential.stat()

    try:
        started = _run(env, "up")
        assert started.returncode == 0, started.stderr
        marker = Path(env["APP_DIR"]) / ".shared-live-ready"
        assert marker.read_text() == "pending:target-sha\n"
        pending_inode = marker.stat().st_ino
        assert not (Path(env["APP_DIR"]) / ".deployed-sha").exists()

        ready = _run(env, "mark-ready", "target-sha")
        assert ready.returncode == 0, ready.stderr
        assert marker.read_text() == "target-sha\n"
        # A read-only file bind in each runner must see the updated inode.
        assert marker.stat().st_ino == pending_inode
        assert marker.stat().st_mode & 0o777 == 0o644
        assert legacy_marker.read_text() == "previous-generation\n"
        assert legacy_marker.stat().st_mode & 0o777 == 0o444
        assert state_root.stat().st_mode & 0o777 == 0o555
        assert credential.read_text() == "host credential"
        assert credential.stat().st_mode == credential_before.st_mode
        assert credential.stat().st_uid == credential_before.st_uid
        assert credential.stat().st_gid == credential_before.st_gid
    finally:
        state_root.chmod(0o755)
        legacy_marker.chmod(0o644)


def test_host_deploy_publishes_readiness_only_after_recording_the_target() -> None:
    script = HOST_DEPLOY.read_text()
    deferred_up = script.index("DEFER_SHARED_READY_MARKER=1")
    deployed_sha = script.index("printf '%s\\n' \"${SHA}\" > .deployed-sha")
    mark_ready = script.index('bash "$SANDBOX_HOST_SCRIPT" mark-ready "${SHA}"')

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


def test_the_sandbox_network_settings_are_required_by_name(tmp_path: Path) -> None:
    """A missing pool or resolver is named before anything is built or drained."""
    env = _host(tmp_path)
    _add_required_host_paths(env, tmp_path)
    dotenv = Path(env["APP_DIR"]) / ".env"
    dotenv.write_text(
        "\n".join(
            line
            for line in dotenv.read_text().splitlines()
            if not line.startswith(("AGENT_EGRESS_CHAT_NETWORK_POOL=", "AGENT_EGRESS_DNS_SERVER="))
        )
        + "\n"
    )

    result = _run(env, "prepare")

    assert result.returncode != 0
    assert "AGENT_EGRESS_CHAT_NETWORK_POOL" in result.stderr
    assert "AGENT_EGRESS_DNS_SERVER" in result.stderr


def test_refused_network_settings_stop_the_rollout_before_draining(tmp_path: Path) -> None:
    """The candidate's own parser reads the settings while the old runners serve.

    A public pool or a resolver inside the pool would otherwise surface only as
    new runners refusing to start, after the serving ones were drained.
    """
    env = _host(tmp_path, network_settings_exit=1)
    _add_required_host_paths(env, tmp_path)
    marker = tmp_path / "app/.shared-live-ready"
    marker.write_text("previous-generation\n")
    env["AGENT_DEPLOYMENT_SHA"] = "target-generation"

    result = _run(env, "up")

    assert result.returncode != 0
    assert "sandbox network settings were refused" in result.stderr
    assert "draining existing runners" not in result.stdout
    assert marker.read_text() == "previous-generation\n"


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
    """A clean host has no previous sandbox image for ``docker run`` to use.

    Sliced from ``cmd_prepare`` rather than ``cmd_up``: the rollout is two named
    halves since #270, and this ordering lives in the first of them. ``cmd_up``
    is now the pair, so slicing it would read four lines and assert nothing.
    """
    script = SANDBOX_HOST.read_text()
    command = script[script.index("cmd_prepare() {") : script.index("cmd_check() {")]

    assert command.index("docker build") < command.index("kata_smoke_test")
    assert command.index("kata_smoke_test") < command.index("compose up")


def test_shared_live_refuses_a_default_pool_size_outside_the_bounds(tmp_path: Path) -> None:
    """One executor would run a fork's sibling behind its parent."""
    result = _run(_host(tmp_path, slots="1"), "preflight")

    assert result.returncode != 0
    assert "AGENT_HOST_SLOTS must be an integer from 2 through 128" in result.stderr


def test_a_leftover_replica_count_is_warned_about_not_read(tmp_path: Path) -> None:
    env = _host(tmp_path)
    with (Path(env["APP_DIR"]) / ".env").open("a") as dotenv:
        dotenv.write("AGENT_RUNNER_REPLICAS=3\n")

    result = _run(env, "preflight")

    assert result.returncode == 0, result.stderr
    assert "AGENT_RUNNER_REPLICAS is no longer read" in result.stderr


def test_the_turn_probe_reads_its_answer_past_log_noise(tmp_path: Path) -> None:
    """Staging, 2026-09-17: the probe's first rollout captured a preflight
    warning on stderr ahead of the decline, judged the image unable to run
    turns, and rolled the host back. The answer is a line, not the output."""
    result = _run(_host(tmp_path), "check")

    assert result.returncode == 0, result.stderr
    assert "turn processes start: declined no turn was delivered" in result.stdout
    assert "cannot run turns" not in result.stderr


def test_shared_live_check_requires_the_image_to_start_a_turn(tmp_path: Path) -> None:
    """A configured limit is not proof that a turn can be started at all.

    A host that claims turns it cannot start takes the queue apart one claim at
    a time, so the deployment proves the path a turn takes up to the moment it
    would begin — and no further, since nothing is claimed by the probe.
    """
    result = _run(_host(tmp_path, turn_starts=False), "check")

    assert result.returncode != 0
    assert "cannot start a turn process" in result.stderr


def test_shared_live_check_does_not_count_runner_containers(tmp_path: Path) -> None:
    """One container per host; its limit, not a container count, is the concurrency."""
    result = _run(_host(tmp_path, running_containers=1), "check")

    assert result.returncode == 0, result.stderr
    assert "turn processes start" in result.stdout


def test_up_no_longer_takes_a_replica_count(tmp_path: Path) -> None:
    env = _host(tmp_path)
    _add_required_host_paths(env, tmp_path)

    result = _run(env, "up", "3")

    assert result.returncode != 0
    assert "no longer takes a replica count" in result.stderr


def test_shared_live_check_runs_the_real_sandbox_preflight_before_attesting() -> None:
    """A running container is not ready while its claim loop is still probing."""
    script = SANDBOX_HOST.read_text()
    check = script[script.index("check_shared_live_stack() {") : script.index("# ── Isolation")]

    assert "backend.preflight()" in check
    assert check.index("backend.preflight()") < check.index("backend.version_stamp()")


def test_the_replica_attestations_start_before_any_is_waited_on() -> None:
    """The preflights must overlap rather than queue.

    Each one makes a real request through the tunnels from inside its own
    container and none reads another's answer, so running them in series bought
    nothing and cost the sum of three waits: 16.6s of a 46s host deployment.
    """
    script = SANDBOX_HOST.read_text()
    check = script[script.index("check_shared_live_stack() {") : script.index("# ── Isolation")]
    launched = check.index("docker exec")
    waited = check.index('wait "${attest_pids')

    assert launched < waited, "the replica attestations were serialized again"
    assert "&" in check[launched:waited], "the attestation does not run in the background"


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
    up = script[script.index("cmd_prepare() {") : script.index("cmd_check() {")]
    dispatch = script[script.rindex('case "${1:-}" in') :]

    assert 'readiness_wait="${readiness_wait:-900}"' in up
    assert '( cmd_check "$readiness_wait" )' in up
    assert "check) cmd_check 0" in dispatch


def test_every_failing_container_is_reported_not_just_the_first(tmp_path: Path) -> None:
    """Run together, all the answers exist at once; report all of them.

    A host runs one runner container, but a rollout lists the outgoing one
    beside its replacement, so the check still asks every container it finds.
    "They all failed" is a configuration problem and "one failed" is that
    container, and a serial loop could only ever surface the first — which is
    the pattern the reader needs to tell them apart.
    """
    result = _run(_host(tmp_path, running_containers=3, capability=""), "check")

    assert result.returncode != 0
    for index in (1, 2, 3):
        assert f"runner-{index} did not attest shared-sandbox-v1" in result.stderr


def test_a_runner_whose_preflight_cannot_run_reports_the_exit_code(tmp_path: Path) -> None:
    """125/126/127 mean the container or the exec is gone, not that a check failed.

    The status has to be captured with `|| rc=$?`. Read from `$?` after the
    command it is never written at all: the script is `set -Eeuo pipefail`, so a
    failing `docker exec` ends the subshell where it stands — on the only path
    that reads the file. That left every failure with an absent status, which
    the reader below happens to treat as a failure; the next reader may not.
    """
    result = _run(_host(tmp_path, attest_exit=125), "check")

    assert result.returncode != 0
    assert "could not complete sandbox preflight" in result.stderr
    assert "docker exec status: 125" in result.stderr, (
        "the exit code is what tells a vanished container from a preflight that raised"
    )


def test_the_attestation_cleanup_belongs_to_the_shell_that_created_it() -> None:
    """`cmd_up` runs these checks in a subshell, and a subshell resets traps.

    A handler the script installs at the top level therefore never runs for the
    `exit` that a failed check performs inside it — and `ATTEST_DIR` is set in
    the subshell, so the parent's copy is still empty. The directory then leaks
    on exactly the path that fails.
    """
    script = SANDBOX_HOST.read_text()
    function = script.index("check_shared_live_stack() {")
    check = script[function : script.index("# ── Isolation")]
    registered = "trap cleanup_attest_dir EXIT"

    # Exactly one, and inside the function. Asserting only that it is absent
    # *before* the function passes for a registration after it, which is the
    # same top-level trap and the same leak.
    assert script.count(registered) == 1, (
        f"the cleanup is registered {script.count(registered)} times; it belongs once, "
        "in the shell that creates the directory"
    )
    assert registered in check, (
        "the cleanup is registered outside check_shared_live_stack, where the subshell in "
        "cmd_up resets it and a failed `up` leaks its directory"
    )


def test_a_failed_attestation_prints_the_end_of_the_traceback() -> None:
    """The exception and its message are the *last* lines of a traceback.

    Each replica's stderr goes to its own file now instead of streaming to the
    terminal, so what gets printed is a choice — and the head of a Python
    traceback is frames, which is the half that explains nothing.
    """
    script = SANDBOX_HOST.read_text()
    check = script[script.index("check_shared_live_stack() {") : script.index("# ── Isolation")]

    assert "tail -n 40" in check
    assert "sed -n '1,20p'" not in check


def test_host_up_probes_the_tunnels_instead_of_pausing_first() -> None:
    """`up` must reach the probe, not sleep on the way to it.

    `compose up -d` has already created and started every container, and the
    probe retries until the readiness deadline, so a fixed pause before it buys
    nothing and is paid in full by every deployment: 5.1s measured from `up -d`
    returning to the retained-storage line, with the first probe succeeding
    immediately after it.
    """
    script = SANDBOX_HOST.read_text()
    up = script[script.index("cmd_prepare() {") : script.index("cmd_check() {")]
    # Statements, not the word: the comment above the probe explains the pause
    # that was removed, and a substring search over the function would fail on
    # the strength of the sentence describing it.
    pauses = [line for line in up.splitlines() if line.strip().startswith("sleep")]

    assert not pauses, f"the host start pauses before probing; the probe already retries: {pauses}"


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


def test_shared_live_check_waits_for_the_broker_and_names_it_on_timeout(tmp_path: Path) -> None:
    """A broker that is up but not yet serving is what a rollout must wait out.

    The runners' preflight asks the broker for its retained sandboxes once, so a
    replica judging a broker that started a second ago reports the session
    supervisor as unavailable and fails a rollout whose stack is healthy moments
    later. With no time left to wait, the verdict has to name the broker rather
    than the runners that merely repeated its silence.
    """
    result = _run(_host(tmp_path, broker_serving=False), "check")

    assert result.returncode != 0
    assert "workspace broker did not answer /health" in result.stderr


def test_shared_live_check_reports_a_missing_broker_replica(tmp_path: Path) -> None:
    """No broker at all is a different statement from one still starting."""
    result = _run(_host(tmp_path, broker_running=False), "check")

    assert result.returncode != 0
    assert "no workspace-broker replica is running" in result.stderr


# ---------------------------------------------------------------------------
# A failed rollout has to leave a host that can still take work (issue #259).
#
# `sandbox_host.sh up` invalidates this host's shared-live marker and drains the
# runners that were serving before it proves the replacement. A failed check
# therefore leaves the host advertising nothing, and a runner advertising
# nothing does not poll at all. On 2026-09-13 that lasted 29.6 hours.
#
# These drive the real `deploy/host/deploy.sh` against a git checkout whose
# bring-up is a stub, because what is under test is the orchestrator's failure
# path rather than Docker, Kata or the stack.
# ---------------------------------------------------------------------------

#: Stands in for the real bring-up. `@GENERATION@` names the commit the files
#: around it belong to, so a restore that ran the wrong tree's script is visible
#: in the log rather than inferred from the marker.
_STUB_BRING_UP = """#!/usr/bin/env bash
set -euo pipefail
echo "@GENERATION@ ${1:-} ${AGENT_DEPLOYMENT_SHA:-}" >>"${STUB_LOG}"
case "${1:-}" in
  up)
    if [ "${STUB_FAILS_SHA:-}" = "${AGENT_DEPLOYMENT_SHA:-}" ]; then
      # STUB_FAIL_ONCE models the transient failure a re-deploy recovers from;
      # without it every attempt at that commit fails, which is a broken host.
      so_far="$(grep -c ' up ' "${STUB_LOG}" || true)"
      if [ "${STUB_FAIL_ONCE:-0}" != "1" ] || [ "${so_far}" -le 1 ]; then
        if [ "${STUB_FAIL_BEFORE_MARKER:-0}" != "1" ]; then
          printf 'pending:%s\\n' "${AGENT_DEPLOYMENT_SHA}" >.shared-live-ready
        fi
        exit 1
      fi
    fi
    printf 'pending:%s\\n' "${AGENT_DEPLOYMENT_SHA}" >.shared-live-ready
    ;;
  mark-ready)
    printf '%s\\n' "${2}" >.shared-live-ready
    ;;
esac
"""


def _deploy_git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    ).stdout


def _deploy_output(result: subprocess.CompletedProcess[str]) -> str:
    """`log` reports on stdout and `die` on stderr; together they are the deploy."""
    return result.stdout + result.stderr


def _deploy_host_rollout(
    tmp_path: Path,
    *,
    recorded: str | None = "previous",
    advertising: bool = False,
    fail_before_marker: bool = False,
    fail_once: bool = False,
    run_from: str = "outside",
) -> tuple[subprocess.CompletedProcess[str], Path, str, str, list[str]]:
    """Run one failing rollout and report what it left behind.

    `recorded` is what `.deployed-sha` names: `"previous"` for the generation
    this checkout can produce, `"target"` for the commit being deployed, a
    literal commit it cannot produce, or nothing at all. `advertising` starts the
    host off already serving the previous generation, which is what a rollout
    that fails before it invalidates the marker leaves alone. `fail_once` makes
    that commit's first attempt the only one that fails, which is what a retry
    recovers from. Returns the result, the deploy root, both commits and the
    bring-up log, in the order the stub was called.
    """
    root = tmp_path / "deploy-root"
    protocol = root / "deploy/DEPLOY_PROTOCOL"
    protocol.parent.mkdir(parents=True)
    protocol.write_text("shared-live-exact-sha-v4\n")
    bring_up = root / "deploy/host/sandbox_host.sh"
    bring_up.parent.mkdir(parents=True, exist_ok=True)

    bring_up.write_text(_STUB_BRING_UP.replace("@GENERATION@", "previous"))
    _deploy_git("init", "-q", cwd=root)
    _deploy_git("add", "-A", cwd=root)
    _deploy_git("commit", "-qm", "previous", cwd=root)
    previous = _deploy_git("rev-parse", "HEAD", cwd=root).strip()

    bring_up.write_text(_STUB_BRING_UP.replace("@GENERATION@", "target"))
    _deploy_git("add", "-A", cwd=root)
    _deploy_git("commit", "-qm", "target", cwd=root)
    target = _deploy_git("rev-parse", "HEAD", cwd=root).strip()

    source = tmp_path / "source"
    _deploy_git("clone", "--quiet", str(root), str(source), cwd=tmp_path)

    (root / ".env").write_text("COMPOSE_PROJECT=test-restore\n")
    if recorded == "previous":
        (root / ".deployed-sha").write_text(previous + "\n")
    elif recorded == "target":
        (root / ".deployed-sha").write_text(target + "\n")
    elif recorded is not None:
        (root / ".deployed-sha").write_text(recorded + "\n")
    if advertising:
        (root / ".shared-live-ready").write_text(previous + "\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "docker", "#!/bin/sh\nexit 0\n")

    # A rollout is run from a checkout that is not the one it rewrites; `inside`
    # is the invocation the restore deliberately refuses.
    if run_from == "inside":
        orchestrator = root / "deploy/host/deploy.sh"
    else:
        orchestrator = tmp_path / "orchestrator/deploy/host/deploy.sh"
    orchestrator.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HOST_DEPLOY, orchestrator)

    log = tmp_path / "bring-up.log"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DEPLOY_ROOT": str(root),
        "SOURCE_REPO": str(source),
        "STUB_LOG": str(log),
        "STUB_FAILS_SHA": target,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    if fail_before_marker:
        env["STUB_FAIL_BEFORE_MARKER"] = "1"
    if fail_once:
        env["STUB_FAIL_ONCE"] = "1"

    result = subprocess.run(
        ["bash", str(orchestrator), target],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, root, previous, target, calls


def test_a_failed_rollout_restores_the_recorded_generation(tmp_path: Path) -> None:
    """The 29.6-hour failure was `up` leaving nothing advertised behind it."""
    result, root, previous, target, calls = _deploy_host_rollout(tmp_path)

    assert result.returncode != 0, "a restored rollout is still a failed deploy"
    assert (root / ".deployed-sha").read_text().strip() == previous
    assert (root / ".shared-live-ready").read_text().strip() == previous
    # The restored generation's own script and tree, not the target's.
    assert calls == [
        f"target up {target}",
        f"previous up {previous}",
        f"previous mark-ready {previous}",
    ]
    assert f"serving {previous} again" in _deploy_output(result)


def test_a_transient_failure_rerolls_the_recorded_commit(tmp_path: Path) -> None:
    """Redeploying the recorded generation is the cheapest recovery there is.

    When `.deployed-sha` already names the target — a manual re-run of a commit
    that served before — the fallback is that same commit rolled out again, and
    it costs a rebuild rather than a rollback. Refusing it here would leave a
    host down over a failure a re-run recovers from.
    """
    result, root, _previous, target, calls = _deploy_host_rollout(
        tmp_path, recorded="target", fail_once=True
    )

    assert result.returncode != 0
    assert (root / ".shared-live-ready").read_text().strip() == target
    assert (root / ".deployed-sha").read_text().strip() == target
    assert calls == [
        f"target up {target}",
        f"target up {target}",
        f"target mark-ready {target}",
    ]


def test_a_rollout_that_kept_the_host_serving_is_not_restored(tmp_path: Path) -> None:
    """A build failure never invalidated anything, so nothing should move.

    Restoring here would interrupt a host that is still working, which is a
    worse outcome than the failed deploy it is reacting to.
    """
    result, root, previous, target, calls = _deploy_host_rollout(
        tmp_path, advertising=True, fail_before_marker=True
    )

    assert result.returncode != 0
    assert calls == [f"target up {target}"]
    assert (root / ".shared-live-ready").read_text().strip() == previous
    assert (root / ".deployed-sha").read_text().strip() == previous
    assert "nothing was interrupted" in _deploy_output(result)


def test_a_failed_rollout_with_nothing_recorded_says_so(tmp_path: Path) -> None:
    """First deploy, or a `.deployed-sha` nobody wrote: there is no way back."""
    result, root, _previous, _target, calls = _deploy_host_rollout(tmp_path, recorded=None)

    assert result.returncode != 0
    assert len(calls) == 1
    assert (root / ".shared-live-ready").read_text().strip().startswith("pending:")
    assert "records no generation to fall back to" in _deploy_output(result)


def test_the_restore_refuses_a_generation_this_host_cannot_produce(tmp_path: Path) -> None:
    """A name in a file is not a tree: checking out nothing restores nothing."""
    result, root, _previous, target, calls = _deploy_host_rollout(tmp_path, recorded="0" * 40)

    assert result.returncode != 0
    assert len(calls) == 1, "a target that cannot be produced must not be attempted"
    assert (root / ".shared-live-ready").read_text().strip() == f"pending:{target}"
    assert "nor fetchable from" in _deploy_output(result)


def test_the_restore_refuses_to_rewrite_the_tree_it_is_running_from(tmp_path: Path) -> None:
    """Bash reads a script by offset, so a reset under it is not survivable.

    CI runs the orchestrator from `.deploy-orchestrator`, a checkout that is not
    DEPLOY_ROOT. Refusing the other invocation is better than executing whatever
    the reset leaves at the running script's offset.
    """
    result, root, previous, target, calls = _deploy_host_rollout(tmp_path, run_from="inside")

    assert result.returncode != 0
    assert calls == [f"target up {target}"]
    assert "refusing to restore from inside" in _deploy_output(result)
    assert (root / ".deployed-sha").read_text().strip() == previous
    assert (root / ".shared-live-ready").read_text().strip() == f"pending:{target}"


def test_host_deploy_reads_the_recorded_generation_before_it_overwrites_it() -> None:
    """`.deployed-sha` is the only name that is known to have served work.

    Read it after a successful rollout has written it and the value is the
    commit that just failed — the same mistake `PREVIOUS` already makes.
    """
    script = HOST_DEPLOY.read_text()

    assert script.index('RECORDED="$(cat .deployed-sha') < script.index(
        "printf '%s\\n' \"${SHA}\" > .deployed-sha"
    )
    assert 'mark-ready "${recorded}"' in script
    assert "restore_recorded_generation" in script


# ── Storage attestation ───────────────────────────────────────────────
#
# The runner refuses to publish a storage reading that the deployment has not
# attested, so these tests are about two things at once: that a sound topology
# produces a `verified` entry the runner will accept, and that nothing here can
# fail a deployment. The second is why every assertion below also checks the
# exit status.

STORAGE_GIB = 1024**3
STORAGE_UPPERDIR = "/var/lib/docker/overlay2/9f1c/diff"
STORAGE_MOUNT_ROOT = "/var/lib/docker"
STORAGE_SIZE = 100 * STORAGE_GIB


def storage_document(
    *,
    upperdir: str = STORAGE_UPPERDIR,
    total: int = STORAGE_SIZE,
    free: int = 40 * STORAGE_GIB,
    available: int = 35 * STORAGE_GIB,
) -> str:
    """What the runner's own describe command prints for a sound topology."""
    return json.dumps(
        {
            "ok": True,
            "reason": None,
            "fstype": "overlay",
            "upperdir": upperdir,
            "total_bytes": total,
            "free_bytes": free,
            "available_bytes": available,
        }
    )


def _write_fake_timeout(tmp_path: Path) -> None:
    """A stand-in for coreutils' `timeout`, which this test machine lacks.

    It implements the contract the deployment relies on — SIGTERM at the
    budget, SIGKILL after `-k`'s grace, 124 or 137 on the way out — because a
    double that killed outright would hide the one probe that matters most: the
    one that ignores SIGTERM. It records what the script asked for beside
    itself, and takes a `timeout.cap` override so a test can reach the bound
    without waiting eight seconds.
    """
    _write_executable(
        tmp_path / "bin/timeout",
        "#!/bin/sh\n"
        # Empty means no `-k` was given, and GNU `timeout` then waits for the
        # child however long it takes. A default grace here would supply the
        # guarantee the deployment script is supposed to ask for.
        'kill_after=""\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -k) kill_after="$2"; shift 2 ;;\n'
        '    --kill-after=*) kill_after="${1#--kill-after=}"; shift ;;\n'
        "    -s|--signal) shift 2 ;;\n"
        "    --signal=*) shift ;;\n"
        "    -*) shift ;;\n"
        "    *) break ;;\n"
        "  esac\n"
        "done\n"
        'budget="$1"\n'
        "shift\n"
        'requested="$budget"\n'
        'cap="$(dirname "$0")/timeout.cap"\n'
        'if [ -f "$cap" ]; then budget="$(cat "$cap")"; fi\n'
        '"$@" &\n'
        "child=$!\n"
        "elapsed=0\n"
        "signalled=0\n"
        'forced=""\n'
        # Polled in hundredths. The loop's first check usually finds the child
        # alive because the fork has not been reaped yet, so the tick is the
        # floor on every wrapped call, and a coarser one would be paid by every
        # call in every test.
        'while kill -0 "$child" 2>/dev/null; do\n'
        '  if [ "$signalled" = "0" ] && [ "$elapsed" -ge $((budget * 100)) ]; then\n'
        '    kill -TERM "$child" 2>/dev/null\n'
        "    signalled=1\n"
        "  fi\n"
        '  if [ "$signalled" = "1" ] && [ -n "$kill_after" ] &&\n'
        '     [ "$elapsed" -ge $(((budget + kill_after) * 100)) ]; then\n'
        '    kill -9 "$child" 2>/dev/null\n'
        '    forced="+kill"\n'
        "    break\n"
        "  fi\n"
        "  sleep 0.01\n"
        "  elapsed=$((elapsed + 1))\n"
        "done\n"
        "status=0\n"
        'wait "$child" 2>/dev/null || status=$?\n'
        'if [ "$signalled" = "1" ]; then\n'
        '  if [ -n "$forced" ]; then status=137; else status=124; fi\n'
        "fi\n"
        'printf \'%s %s%s\\n\' "$requested" "${kill_after:-none}" "$forced" >>"$(dirname "$0")/timeout.log"\n'
        'exit "$status"\n',
    )


def _cap_fake_timeout(tmp_path: Path, seconds: int) -> None:
    """Shorten the fake `timeout`'s budget so a bounded call is cheap to test."""
    (tmp_path / "bin/timeout.cap").write_text(str(seconds))


def _budgets_the_script_asked_for(tmp_path: Path) -> set[str]:
    """The `budget kill-after[+kill]` lines the fake recorded, one per call.

    Empty when the log is absent: the script skips the attestation outright
    when the fence has nothing left, and a caller asserting on the budgets
    should fail saying that, not with a file-not-found from in here.
    """
    log = tmp_path / "bin/timeout.log"
    return set(log.read_text().splitlines()) if log.exists() else set()


def _add_storage_host_tools(
    tmp_path: Path,
    *,
    upper_size: int | None = None,
    used: int = 60 * STORAGE_GIB,
    available: int = 35 * STORAGE_GIB,
    findmnt_fails: bool = False,
    df_upper_hangs: bool = False,
    upperdir: str = STORAGE_UPPERDIR,
) -> None:
    """Fake the two host tools the attestation shells out to.

    ``findmnt`` and ``df`` do not exist on the machine these tests run on, and
    the values they answer with *are* the thing under test — a mount root, an
    upper directory that reports a quota, a df that disagrees with the
    container, a df that never answers. Faking them is the only way to exercise
    the comparison at all.
    """
    upper_answer = (
        # `exec` so the hang is this process: the bounded call has to be the
        # thing that ends it, and the header this line may already have printed
        # is exactly what must not be mistaken for a size.
        "exec sleep 300"
        if df_upper_hangs
        else f"echo {STORAGE_SIZE if upper_size is None else upper_size}"
    )
    sizes = {STORAGE_MOUNT_ROOT: f"echo {STORAGE_SIZE}", upperdir: upper_answer}
    size_arms = "\n".join(f"      '{path}') {answer} ;;" for path, answer in sizes.items())
    _write_executable(
        tmp_path / "bin/df",
        "#!/bin/sh\n"
        'path=""; for arg in "$@"; do path="$arg"; done\n'
        'case "$*" in\n'
        "  *--output=size*)\n"
        '    echo "1B-blocks"\n'
        '    case "$path" in\n'
        f"{size_arms}\n"
        "      *) exit 1 ;;\n"
        "    esac ;;\n"
        "  *--output=used,avail*)\n"
        '    echo "1B-blocks 1B-blocks"\n'
        '    case "$path" in\n'
        f"      '{STORAGE_MOUNT_ROOT}') echo '{used} {available}' ;;\n"
        "      *) exit 1 ;;\n"
        "    esac ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    _write_fake_timeout(tmp_path)
    _write_executable(
        tmp_path / "bin/findmnt",
        "#!/bin/sh\n"
        'field=""; expect=""\n'
        'for arg in "$@"; do\n'
        '  case "$expect" in\n'
        '    field) field="$arg"; expect=""; continue ;;\n'
        "  esac\n"
        '  case "$arg" in\n'
        '    -o) expect="field" ;;\n'
        "  esac\n"
        "done\n"
        f"if [ '{1 if findmnt_fails else 0}' = '1' ]; then exit 1; fi\n"
        'case "$field" in\n'
        f"  FSTYPE,TARGET) echo 'xfs {STORAGE_MOUNT_ROOT}' ;;\n"
        f"  TARGET) echo '{STORAGE_MOUNT_ROOT}' ;;\n"
        "  FSTYPE) echo 'xfs' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
    )


def _storage_host(
    tmp_path: Path,
    *,
    describe: str | None = None,
    storage_exit: int = 0,
    storage_hang: bool = False,
    storage_ignores_term: bool = False,
    capability: str = "shared-sandbox-v1,native-fork-at-anchor-v1",
    probe: str | None = None,
    findmnt_fails: bool = False,
    df_upper_hangs: bool = False,
    upper_size: int | None = None,
    used: int = 60 * STORAGE_GIB,
    available: int = 35 * STORAGE_GIB,
    running_containers: int = 1,
    host_upperdir: str = STORAGE_UPPERDIR,
) -> dict[str, str]:
    """A host whose storage attestation inputs the caller controls.

    One runner container by default, which is what a host now runs: its
    concurrency is the container's own slot limit, not a count of replicas.
    A test that needs to see per-container behaviour asks for more; the rest
    are spared paying the same per-attempt cost once per container.
    """
    env = _host(
        tmp_path,
        capability=capability,
        running_containers=running_containers,
        storage_json=storage_document() if describe is None else describe,
        storage_exit=storage_exit,
        storage_hang=storage_hang,
        storage_ignores_term=storage_ignores_term,
    )
    env["AGENT_DEPLOYMENT_SHA"] = "target-sha"
    _add_storage_host_tools(
        tmp_path,
        upper_size=upper_size,
        used=used,
        available=available,
        findmnt_fails=findmnt_fails,
        df_upper_hangs=df_upper_hangs,
        upperdir=host_upperdir,
    )
    if probe is not None:
        with (Path(env["APP_DIR"]) / ".env").open("a") as dotenv:
            dotenv.write(f"AGENT_HOST_STORAGE_PROBE={probe}\n")
    return env


def _verdict(env: dict[str, str]) -> dict:
    return json.loads((Path(env["APP_DIR"]) / ".storage-probe" / "verdict.json").read_text())


def test_a_sound_topology_becomes_a_verified_entry(tmp_path: Path) -> None:
    env = _storage_host(tmp_path)

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    assert f"storage verified against {STORAGE_MOUNT_ROOT} (xfs," in result.stdout
    document = _verdict(env)
    assert document["schema_version"] == 1
    assert document["deployment_sha"] == "target-sha"
    entry = document["entries"][STORAGE_UPPERDIR]
    assert entry["verdict"] == "verified"
    assert entry["mount_root"] == STORAGE_MOUNT_ROOT
    assert entry["fstype"] == "xfs"
    assert entry["size_bytes"] == STORAGE_SIZE


def test_a_writable_layer_path_with_a_blank_is_still_attested(tmp_path: Path) -> None:
    """The kernel escapes blanks in a mount table; host tools want the bytes.

    The runner keeps its upper directory escaped because that string is the key
    it looks its own verdict up by. Handing the same string to `findmnt -T`,
    `df` or `docker --mount` names nothing at all, so a host whose storage root
    contains a space could never be attested — permanently unverified, with a
    warning blaming the host's permissions.
    """
    escaped = "/var/lib/my\\040docker/overlay2/9f1c/diff"
    env = _storage_host(
        tmp_path,
        describe=storage_document(upperdir=escaped),
        host_upperdir="/var/lib/my docker/overlay2/9f1c/diff",
    )

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    entries = _verdict(env)["entries"]
    # The key keeps its escapes: it is what the runner will look itself up by.
    assert entries[escaped]["verdict"] == "verified"
    assert entries[escaped]["mount_root"] == STORAGE_MOUNT_ROOT


@pytest.mark.parametrize("value", ["OFF", "0", "false", "disabled", "overlay-root", "zfs_dataset"])
def test_a_probe_name_neither_side_agrees_on_is_refused(tmp_path: Path, value: str) -> None:
    """One spelling, refused at configuration time rather than diverging.

    The runner normalises this variable and its vocabulary is wider than the
    script's, so a value only one side recognises used to produce a rollout log
    reading "storage verified" beside a page reading "switched off" — or
    "not supported" — with nothing to say which had it right.
    """
    env = _storage_host(tmp_path, probe=value)

    result = _run(env, "check")

    assert result.returncode != 0
    assert "AGENT_HOST_STORAGE_PROBE must be" in result.stderr


def test_a_quota_on_the_upper_directory_is_reported_as_unsupported(tmp_path: Path) -> None:
    """The whole reason the comparison is against the mount root.

    `df <upperdir>` returns the project quota, so an attestation that compared
    the container against that path would agree with a container reading the
    quota — and call a 10 GiB ceiling the machine's capacity.
    """
    env = _storage_host(tmp_path, upper_size=10 * STORAGE_GIB)

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    entry = _verdict(env)["entries"][STORAGE_UPPERDIR]
    assert entry["verdict"] == "unsupported"
    assert "project quota" in entry["reason"]


def test_a_daemon_configured_with_overlay2_size_is_unsupported(tmp_path: Path) -> None:
    """Checked even when the sizes happen to agree: the config is the tell."""
    env = _storage_host(tmp_path)
    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text('{"storage-opts": ["overlay2.size=10G"]}')
    env["DOCKER_DAEMON_JSON"] = str(daemon_json)

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    entry = _verdict(env)["entries"][STORAGE_UPPERDIR]
    assert entry["verdict"] == "unsupported"
    assert "overlay2.size" in entry["reason"]


def test_a_failing_helper_is_named_instead_of_findmnt(tmp_path: Path) -> None:
    """The tool that failed is the tool the operator is sent to.

    `findmnt` failing is the *normal* state on a host whose storage root the
    deployment user cannot traverse — it is why the helper exists. Reporting
    the helper's own failure as findmnt's sends the reader after a permission
    problem they have already worked around, and the helper's account of
    itself goes to a stream nothing else prints.
    """
    env = _storage_host(tmp_path, findmnt_fails=True)
    _add_storage_helper(tmp_path, exit_code=3, complaint="mounted directory does not match")

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    entry = _verdict(env)["entries"][STORAGE_UPPERDIR]
    assert entry["verdict"] == "unverified"
    assert "storage helper exited 3" in entry["reason"]
    assert "storage helper exited 3" in result.stderr
    assert "mounted directory does not match" in result.stderr


def test_a_helper_that_answers_without_a_mount_root_is_unverified(tmp_path: Path) -> None:
    """The findmnt wording belongs to the case where nothing failed outright."""
    env = _storage_host(tmp_path, findmnt_fails=True)
    _add_storage_helper(tmp_path, record="{}")

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    entry = _verdict(env)["entries"][STORAGE_UPPERDIR]
    assert entry["verdict"] == "unverified"
    assert "could not locate" in entry["reason"]
    assert "storage helper exited" not in entry["reason"]


def test_a_size_disagreement_between_container_and_host_is_unsupported(tmp_path: Path) -> None:
    env = _storage_host(tmp_path, describe=storage_document(total=200 * STORAGE_GIB))

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    entry = _verdict(env)["entries"][STORAGE_UPPERDIR]
    assert entry["verdict"] == "unsupported"
    assert str(STORAGE_SIZE) in entry["reason"]


def test_occupancy_that_never_agrees_is_unverified_and_names_the_deltas(tmp_path: Path) -> None:
    """Used and available are a diagnostic band, never a topology verdict.

    This branch also records the filesystem type, and it is the host's: inside
    the container every root is an `overlay`, which names nothing an operator
    can act on.
    """
    env = _storage_host(tmp_path, used=60 * STORAGE_GIB + 8 * STORAGE_GIB)

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    entry = _verdict(env)["entries"][STORAGE_UPPERDIR]
    assert entry["verdict"] == "unverified"
    assert "differs by" in entry["reason"]


def test_a_runner_that_cannot_describe_its_filesystem_gets_no_entry(tmp_path: Path) -> None:
    """The runner reaches the same conclusion itself; no entry is written for it."""
    env = _storage_host(tmp_path, describe='{"ok": false, "reason": "unsupported"}')

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    assert _verdict(env)["entries"] == {}
    assert "cannot describe its storage filesystem" in result.stderr


def test_storage_measurement_switched_off_clears_the_verdict(tmp_path: Path) -> None:
    """`off` has to reach the replicas that are already running.

    Their environment was fixed when they were created, so they keep the
    overlay probe until an `up` replaces them. Left with the verdict that
    verified them, they would go on publishing capacity for a host the operator
    has just switched off; emptied, they report `unverified` on their next
    sample and the figures stop at once.
    """
    env = _storage_host(tmp_path)
    assert _run(env, "check").returncode == 0
    assert _verdict(env)["entries"][STORAGE_UPPERDIR]["verdict"] == "verified"
    env["AGENT_HOST_STORAGE_PROBE"] = "off"

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    assert "switched off" in result.stdout
    assert _verdict(env)["entries"] == {}


def test_a_failed_rollout_still_writes_a_fail_closed_verdict(tmp_path: Path) -> None:
    """A runner that starts before the checks finish must read `unverified`.

    The capability failure stops the deployment before storage verification
    runs, which is exactly the window the empty entry set exists for.
    """
    env = _storage_host(tmp_path, capability="")
    _add_required_host_paths(env, tmp_path)
    env["DEFER_SHARED_READY_MARKER"] = "1"
    _write_executable(tmp_path / "bin/sleep", "#!/bin/sh\nexit 0\n")

    result = _run(env, "up")

    assert result.returncode != 0
    document = _verdict(env)
    assert document["entries"] == {}
    assert document["schema_version"] == 1
    assert document["deployment_sha"] == "target-sha"
    assert document["written_at"].endswith("Z")


def test_the_verdict_is_replaced_without_disturbing_the_bound_directory(tmp_path: Path) -> None:
    """The directory is what runners bind; the file inside it is swapped.

    Swapped, not truncated: a replica samples every thirty seconds and opens
    the current directory entry each time, so a writer that emptied the file
    first would hand whoever arrived mid-write a document it can only read as
    `unverified` — a host reported unmeasured moments after it passed.
    """
    env = _storage_host(tmp_path)
    first = _run(env, "check")
    assert first.returncode == 0, first.stderr
    verdict_file = Path(env["APP_DIR"]) / ".storage-probe" / "verdict.json"
    inode = verdict_file.stat().st_ino
    directory_inode = verdict_file.parent.stat().st_ino

    second = _run(env, "check")

    assert second.returncode == 0, second.stderr
    assert verdict_file.stat().st_ino != inode
    assert verdict_file.parent.stat().st_ino == directory_inode
    assert verdict_file.stat().st_mode & 0o777 == 0o644
    assert not list(verdict_file.parent.glob("*.tmp"))


def test_check_refreshes_a_verdict_without_a_redeploy(tmp_path: Path) -> None:
    env = _storage_host(tmp_path)
    verdict_file = Path(env["APP_DIR"]) / ".storage-probe" / "verdict.json"
    verdict_file.parent.mkdir()
    verdict_file.write_text('{"schema_version":1,"entries":{}}')

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    assert _verdict(env)["entries"][STORAGE_UPPERDIR]["verdict"] == "verified"


def test_check_recreates_a_deleted_verdict_in_the_same_bound_directory(tmp_path: Path) -> None:
    env = _storage_host(tmp_path)
    assert _run(env, "check").returncode == 0
    verdict_file = Path(env["APP_DIR"]) / ".storage-probe" / "verdict.json"
    directory_inode = verdict_file.parent.stat().st_ino
    verdict_file.unlink()

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    assert _verdict(env)["entries"][STORAGE_UPPERDIR]["verdict"] == "verified"
    assert verdict_file.parent.stat().st_ino == directory_inode
    assert verdict_file.stat().st_mode & 0o777 == 0o644


def test_check_does_not_fail_when_the_verdict_directory_cannot_be_created(tmp_path: Path) -> None:
    env = _storage_host(tmp_path)
    (Path(env["APP_DIR"]) / ".storage-probe").write_text("a file blocks the directory")

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    assert "could not prepare the storage-probe directory" in result.stderr


def test_no_storage_failure_path_can_end_the_deployment() -> None:
    """The one exception is the bind-mount source, and it is a separate function."""
    script = SANDBOX_HOST.read_text()
    verify = script[
        script.index("verify_storage_probe() {") : script.index("probe_sandbox_relay() {")
    ]
    check = script[script.index("check_shared_live_stack() {") : script.index("# ── Isolation")]

    assert "die " not in verify
    assert "verify_storage_probe " in check
    # The non-fatal contract is why the fatal verdict list is built before it.
    assert check.index("verdicts+=(") < check.index("verify_storage_probe ")
    # And why the attestation runs after every check that can refuse the host.
    # Those checks retry against this same deadline, so seconds spent measuring
    # a disk first are seconds they do not get: the attestation cannot end a
    # deployment by its own exit status, but running before them it can end one
    # by leaving them no time. Last, it can only spend what they did not need.
    assert check.rindex("die ") < check.index("verify_storage_probe ")
    assert check.index("cannot reach the control-plane health endpoint") < check.index(
        "verify_storage_probe "
    )


def test_the_verdict_marker_is_prepared_before_the_containers_start() -> None:
    """The runner containers bind it, so it has to exist before `compose up`.

    In `activate`, which is the half of a phased deployment that starts
    containers: `prepare` may run in a separate process, and a bind source
    written there would not be this one.
    """
    script = SANDBOX_HOST.read_text()
    activate = script[script.index("cmd_activate() {") : script.index("cmd_up() {")]

    assert activate.index("prepare_storage_verdict") < activate.index("compose up -d")
    assert activate.index("prepare_shared_live_marker") < activate.index("prepare_storage_verdict")


# ── Storage attestation: it must not be able to hold a rollout open ────


def test_a_rollout_fence_with_no_room_left_skips_the_attestation(tmp_path: Path) -> None:
    """A spent fence is not a reason to spend thirty more seconds.

    The readiness deadline is a promise to the control plane, and a rollout
    whose probes have already eaten it cannot be saved by measuring storage
    late. The attestation must notice that it has no budget and make no call at
    all — not make an eight-second call and then notice.
    """
    env = _storage_host(tmp_path, storage_hang=True)
    _add_required_host_paths(env, tmp_path)
    with (Path(env["APP_DIR"]) / ".env").open("a") as dotenv:
        dotenv.write("SHARED_READY_TIMEOUT=2\n")

    started = time.monotonic()
    result = _run(env, "up")
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    # The rollout published its readiness marker, which is the thing a hang
    # would have prevented.
    assert (Path(env["APP_DIR"]) / ".shared-live-ready").read_text().strip() == "target-sha"
    # And no number was published for a filesystem nobody managed to measure.
    assert _verdict(env)["entries"] == {}
    assert "no room left for a storage attestation" in result.stderr
    # Not one probe call was made, and the whole rollout finished well inside
    # what a single bounded call would have cost.
    assert not (tmp_path / "bin/timeout.log").exists()
    assert elapsed < 6, f"storage spent {elapsed:.1f}s inside a two-second fence"


def test_a_hung_storage_probe_is_bounded_on_a_manual_check(tmp_path: Path) -> None:
    """`check` has no rollout fence to borrow, so the per-call bound is it."""
    env = _storage_host(tmp_path, storage_hang=True)
    _cap_fake_timeout(tmp_path, 1)

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    assert _verdict(env)["entries"] == {}
    assert "did not answer the storage probe description within its budget" in result.stderr
    # Every call was bounded at the script's own budget, with a force-kill
    # grace behind it.
    assert _budgets_the_script_asked_for(tmp_path) == {"8 2"}


def test_a_probe_that_ignores_sigterm_is_still_cut_off(tmp_path: Path) -> None:
    """SIGTERM alone is not a bound: GNU `timeout` waits for a process that
    ignores it, which is what `-k` exists for. Without the force-kill this test
    never finishes — and neither would the rollout it stands for."""
    env = _storage_host(tmp_path, storage_hang=True, storage_ignores_term=True)
    _cap_fake_timeout(tmp_path, 1)

    started = time.monotonic()
    result = _run(env, "check")
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert _verdict(env)["entries"] == {}
    # The probe had to be killed, which is the fact the budget depends on.
    assert any(line.endswith("+kill") for line in _budgets_the_script_asked_for(tmp_path))
    assert elapsed < 20, f"an ignored SIGTERM held the check for {elapsed:.1f}s"


def test_a_storage_attestation_that_cannot_be_bounded_does_not_run() -> None:
    """Without coreutils `timeout` there is no bound, so there is no attestation.

    Structural on purpose: every host this script supports carries coreutils,
    which is exactly why the branch cannot be reached by running it here — and
    why the guard has to be checked rather than assumed. It must also come
    before the first probe call, or an unbounded call happens anyway.
    """
    script = SANDBOX_HOST.read_text()
    verify = script[
        script.index("verify_storage_probe() {") : script.index("probe_sandbox_relay() {")
    ]
    guard = verify.index("command -v timeout")
    first_call = verify.index('storage_bounded "$storage_deadline" docker exec')

    assert guard < first_call
    assert "return 0" in verify[guard:first_call]


def test_the_storage_budget_comes_from_the_fence_not_from_beside_it() -> None:
    """A rollout's fence caps the attestation; a manual check has no fence."""
    script = SANDBOX_HOST.read_text()
    verify = script[
        script.index("verify_storage_probe() {") : script.index("probe_sandbox_relay() {")
    ]
    bounded = script[script.index("storage_bounded() {") : script.index("storage_verdict_file() {")]

    # The fence flag distinguishes the two callers: an expired rollout fence
    # must not be read as "no fence, take the full budget".
    assert "local fenced=0" in script
    assert 'check_shared_live_stack "$deadline" "$fenced"' in script
    assert 'verify_storage_probe "$ATTEST_DIR" "$runner_ids" "$deadline" "$fenced"' in script
    assert "if (( fenced )); then" in verify
    assert 'storage_deadline="$deadline"' in verify
    assert "if (( storage_deadline - SECONDS <= STORAGE_KILL_AFTER_S )); then" in verify
    # Each call may only spend what is left: the per-call bound is recomputed
    # from the deadline rather than handed out at a fixed eight seconds.
    assert "remaining=$(( deadline - SECONDS - STORAGE_KILL_AFTER_S ))" in bounded
    assert 'timeout -k "$STORAGE_KILL_AFTER_S" "$remaining" "$@"' in bounded
    assert 'storage_bounded "$storage_deadline" docker exec' in verify
    assert 'storage_bounded "$storage_deadline" findmnt' in verify
    assert 'storage_df_size "$mount_root" "$storage_deadline"' in verify


def test_a_partly_spent_fence_shrinks_the_call_bound(tmp_path: Path) -> None:
    """The per-call bound is what is left of the fence, not a flat eight seconds.

    A rollout with a few seconds of fence left cannot afford an eight-second
    call, and making one is how the fence is missed even though every single
    call is "bounded". This is the case the first version of the fix got wrong:
    it bounded each call at eight seconds and only checked the budget between
    phases, so a two-second fence still cost eight.

    The fence is wide enough that the checks running before the attestation
    cannot consume all of it. At four seconds this test asserted nothing
    whenever the machine was loaded: the script would correctly skip the
    attestation, and the assertions would be about calls that never happened.
    """
    env = _storage_host(tmp_path, storage_hang=True)
    _add_required_host_paths(env, tmp_path)
    with (Path(env["APP_DIR"]) / ".env").open("a") as dotenv:
        dotenv.write("SHARED_READY_TIMEOUT=6\n")

    started = time.monotonic()
    result = _run(env, "up")
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert _verdict(env)["entries"] == {}
    budgets = [int(line.split()[0]) for line in _budgets_the_script_asked_for(tmp_path)]
    assert budgets, "the attestation made no call, so this asserts nothing"
    assert max(budgets) < 8, f"a call was handed the flat bound: {budgets}"
    assert elapsed < 10, f"the attestation took {elapsed:.1f}s inside a six-second fence"


def test_a_df_that_cannot_size_the_upper_directory_is_unverified(tmp_path: Path) -> None:
    """No answer is not a different answer.

    A `df <upperdir>` that is cut short prints nothing, and comparing that
    nothing with the mount root's size "disagrees" with it — which used to be
    recorded as a project quota: a statement about the host's configuration
    that nobody established, on the one metric whose whole purpose is to avoid
    exactly that kind of claim. It is a missing measurement, and the page says
    so.
    """
    env = _storage_host(tmp_path, df_upper_hangs=True)
    _cap_fake_timeout(tmp_path, 1)

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    entry = _verdict(env)["entries"][STORAGE_UPPERDIR]
    assert entry["verdict"] == "unverified"
    assert "could not size" in entry["reason"]
    assert "quota" not in entry["reason"]


def test_a_quota_on_the_upper_directory_is_still_unsupported(tmp_path: Path) -> None:
    """The answer that *is* a real disagreement keeps its verdict.

    The empty case must not swallow the one the comparison exists for: two
    valid sizes that differ are a project quota, and the attestation has to go
    on saying so.
    """
    env = _storage_host(tmp_path, upper_size=10 * STORAGE_GIB)

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    entry = _verdict(env)["entries"][STORAGE_UPPERDIR]
    assert entry["verdict"] == "unsupported"
    assert "project quota" in entry["reason"]


def _add_storage_helper(
    tmp_path: Path,
    *,
    record: str | None = None,
    exit_code: int = 0,
    hangs: bool = False,
    complaint: str = "",
) -> None:
    """Model Docker's metadata helper separately from the inaccessible host path."""
    if record is None:
        record = json.dumps(
            {
                "mount_root": STORAGE_MOUNT_ROOT,
                "fstype": "xfs",
                "total_bytes": STORAGE_SIZE,
                "upper_total_bytes": STORAGE_SIZE,
                "used_bytes": 60 * STORAGE_GIB,
                "available_bytes": 35 * STORAGE_GIB,
            }
        )
    (tmp_path / "helper-response.json").write_text(record)
    docker = tmp_path / "bin/docker"
    original = docker.read_text()
    answer = "exec sleep 300" if hangs else 'cat "$test_dir/helper-response.json"'
    if complaint:
        answer = f"printf '%s\\n' '{complaint}' >&2; {answer}"
    prefix = (
        '#!/bin/sh\ntest_dir="$(dirname "$(dirname "$0")")"\n'
        'case "$*" in\n'
        "  'inspect --format {{.Image}} '*) echo 'sha256:" + "a" * 64 + "'; exit 0 ;;\n"
        "  run*'cloud_agent_host.storage_attestation'*)\n"
        '    printf \'%s\\n\' "$@" >>"$test_dir/helper-args"\n'
        f"    {answer}; exit {exit_code} ;;\n"
        "  'rm -f agent-storage-attest-'*) echo cleanup >>\"$test_dir/helper-cleanup\"; exit 0 ;;\n"
        "esac\n"
    )
    docker.write_text(prefix + original.removeprefix("#!/bin/sh\n"))
    findmnt = tmp_path / "bin/findmnt"
    mounts = json.dumps(
        {
            "filesystems": [
                {"target": STORAGE_MOUNT_ROOT, "fstype": "xfs", "maj:min": "8:2", "fsroot": "/"}
            ]
        }
    )
    (tmp_path / "mount-plan.json").write_text(mounts)
    findmnt.write_text(
        '#!/bin/sh\ncase "$*" in *--json*) cat "$(dirname "$(dirname "$0")")/mount-plan.json"; exit 0 ;; esac\n'
        + findmnt.read_text().removeprefix("#!/bin/sh\n")
    )


def test_inaccessible_host_path_uses_the_read_only_helper(tmp_path: Path) -> None:
    env = _storage_host(tmp_path, findmnt_fails=True)
    _add_storage_helper(tmp_path)

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    entry = _verdict(env)["entries"][STORAGE_UPPERDIR]
    assert entry["verdict"] == "verified"
    assert entry["mount_root"] == STORAGE_MOUNT_ROOT
    assert entry["fstype"] == "xfs"
    args = (tmp_path / "helper-args").read_text().splitlines()
    for flag, value in (
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges:true"),
        ("--entrypoint", "python"),
        (
            "--mount",
            f"type=bind,src={STORAGE_MOUNT_ROOT},dst=/disk,readonly,bind-recursive=disabled,bind-propagation=rslave",
        ),
        # Pinned because the helper needs no privilege it does not have as
        # `nobody`, and because nothing else would notice it regaining one.
        ("--user", "65534:65534"),
    ):
        assert args[args.index(flag) + 1] == value
    assert "sha256:" + "a" * 64 in args
    assert "--read-only" in args and "--pull=never" in args and "--rm" in args
    assert "--privileged" not in args and "--pid=host" not in args and "--cap-add" not in args
    assert (
        f"type=bind,src={STORAGE_UPPERDIR},dst=/upper,readonly,bind-recursive=disabled,bind-propagation=rslave"
        in args
    )
    # Not the pre-25 spelling: a current CLI does not accept it at all. Docker
    # 29.2.1 answers `bind-nonrecursive=true` with "invalid argument ... use
    # bind-recursive=disabled instead" and exits 125, so reaching for the older
    # key to support older hosts takes the feature away from every host that is
    # not one.
    assert "bind-nonrecursive" not in " ".join(args)


def test_helper_still_rejects_a_real_project_quota(tmp_path: Path) -> None:
    env = _storage_host(tmp_path, findmnt_fails=True)
    _add_storage_helper(tmp_path)
    response = tmp_path / "helper-response.json"
    value = json.loads(response.read_text())
    value["upper_total_bytes"] = 10 * STORAGE_GIB
    response.write_text(json.dumps(value))

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    entry = _verdict(env)["entries"][STORAGE_UPPERDIR]
    assert entry["verdict"] == "unsupported"
    assert "project quota" in entry["reason"]


@pytest.mark.parametrize("record,exit_code", [("{}", 0), ("null", 0), ("{", 0), (None, 125)])
def test_failed_or_malformed_helper_does_not_publish_capacity(
    tmp_path: Path, record: str | None, exit_code: int
) -> None:
    env = _storage_host(tmp_path, findmnt_fails=True)
    _add_storage_helper(tmp_path, record=record, exit_code=exit_code)

    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    assert _verdict(env)["entries"][STORAGE_UPPERDIR]["verdict"] == "unverified"
    if exit_code:
        assert (tmp_path / "helper-cleanup").exists()


def test_hung_helper_is_bounded_and_cleaned_up(tmp_path: Path) -> None:
    # Two containers: the cleanup this asserts is per container, and one
    # container cannot tell "each helper is removed" from "the last one is".
    env = _storage_host(tmp_path, findmnt_fails=True, running_containers=2)
    _add_storage_helper(tmp_path, hangs=True)
    _cap_fake_timeout(tmp_path, 1)

    started = time.monotonic()
    result = _run(env, "check")

    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 12
    assert _verdict(env)["entries"][STORAGE_UPPERDIR]["verdict"] == "unverified"
    assert (tmp_path / "helper-cleanup").read_text().splitlines() == ["cleanup", "cleanup"]


def test_every_command_that_reads_configuration_reads_the_resolved_one(
    tmp_path: Path,
) -> None:
    """The phase commands must parse `.env` the way `up` does, not the way a
    hand-rolled reader would.

    Review found `prepare` and `activate` missing from the list of commands that
    load Compose's resolved environment after the phase split. Compose expands
    `${VAR}` and strips an inline comment; the fallback reader does neither, so
    the same file that `up` correctly refused was accepted by `prepare` — with a
    state root that was the literal text `${STATE_BASE}/agent-host` and a
    network name with ` # shared default` on the end of it.
    """
    dispatch = SANDBOX_HOST.read_text()
    loads = dispatch[dispatch.index('case "${1:-}" in') :]
    loads = loads[: loads.index("esac")]
    assert "prepare" in loads and "activate" in loads, loads


def _resolve_environment_with_real_compose(tmp_path: Path) -> None:
    """Let the fixture's fake `docker` hand one subcommand to the real one.

    `config --environment` is where interpolation and inline comments are
    decided, and a fake that answers it cannot show which reader a command used.
    Everything else stays recorded and synthetic: no pull, no run, no service.
    """
    real = shutil.which("docker")
    if not real:
        pytest.skip("Compose's own dotenv parser is what this compares against")
    docker = tmp_path / "bin" / "docker"
    lines = docker.read_text().splitlines(keepends=True)
    lines[1:1] = [f'case " $* " in *" config --environment "*) exec "{real}" "$@";; esac\n']
    docker.write_text("".join(lines))


@pytest.mark.parametrize("command", ["up", "prepare"])
def test_a_shared_default_is_refused_whichever_command_reads_it(
    tmp_path: Path, command: str
) -> None:
    """Interpolation and an inline comment, through Compose, for both."""
    env = _host(tmp_path)
    _add_required_host_paths(env, tmp_path)
    _resolve_environment_with_real_compose(tmp_path)
    app = Path(env["APP_DIR"])
    with (app / ".env").open("a") as stream:
        stream.write("SIBLING_COMPOSE_PROJECTS=other-environment\n")
        stream.write("STATE_BASE=/var/lib/hybridinference\n")
        stream.write("AGENT_HOST_STATE_ROOT=${STATE_BASE}/agent-host\n")
        stream.write("AGENT_ENROLL_TOKEN=synthetic-token\n")

    result = _run(env, command)

    assert result.returncode != 0
    assert "must be unique to this stack" in result.stderr, result.stdout + result.stderr
