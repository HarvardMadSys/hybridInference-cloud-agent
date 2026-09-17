"""Deployment checks for the retained/shared-live staging topology."""

from __future__ import annotations

import os
import shutil
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
        f"  *' ps -q runner') printf '%s\\n' '{runner_ids}' ;;\n"
        f"  *' ps -q workspace-broker') printf '%s\\n' '{broker_id}' ;;\n"
        f"  *' logs --since 2m runner') echo '{rejection_log}' ;;\n"
        f"  exec*'_shared_live_host_capabilities'*) echo '{capability}'; exit {attest_exit} ;;\n"
        f"  exec*8092/health*) exit {broker_probe_exit} ;;\n"
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
    protocol.write_text("shared-live-exact-sha-v3\n")
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
        stream.write("AGENT_EGRESS_NETWORK_PLATFORM_ONLY=agent-egress # shared default\n")

    result = _run(env, command)

    assert result.returncode != 0
    assert "must be unique to this stack" in result.stderr, result.stdout + result.stderr
