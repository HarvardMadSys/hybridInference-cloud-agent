"""Deployment checks for the retained/shared-live staging topology."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX_HOST = REPO_ROOT / "deploy/host/sandbox_host.sh"
HOST_DEPLOY = REPO_ROOT / "deploy/host/deploy.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _host(
    tmp_path: Path,
    *,
    restartable: str = "1",
    shared: str = "1",
    slots: str = "3",
    replicas: str = "3",
    running_replicas: int = 3,
    credential: bool = True,
    configured_root: str = "/var/lib/docker",
    daemon_root: str = "/var/lib/docker",
    capability: str = "shared-sandbox-v1",
    include_rollout: bool = True,
    github_workflow: str = "",
    deployment_environment: str = "",
    staging_override: str = "",
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
    if include_rollout:
        dotenv += [
            f"AGENT_RESTARTABLE_SANDBOX={restartable}",
            f"AGENT_SHARED_LIVE_SANDBOX={shared}",
            f"AGENT_HOST_SLOTS={slots}",
            f"AGENT_RUNNER_REPLICAS={replicas}",
        ]
    if staging_override:
        dotenv.append(f"STAGING_SHARED_LIVE_SANDBOX={staging_override}")
    if deployment_environment:
        dotenv.append(f"DEPLOYMENT_ENVIRONMENT={deployment_environment}")
    (app_dir / ".env").write_text("\n".join([*dotenv, ""]))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    rejection_log = "agent_host_credential_rejected" if credential_rejected else ""
    runner_ids = "\n".join(f"runner-{index}" for index in range(1, running_replicas + 1))
    _write_executable(
        fake_bin / "docker",
        "#!/bin/sh\n"
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
        "GITHUB_WORKFLOW": github_workflow,
        "DEPLOYMENT_ENVIRONMENT": "",
    }
    # Exercise the same .env path an operator uses, independent of the test
    # process's own environment.
    for name in (
        "COMPOSE_PROJECT",
        "AGENT_SANDBOX_BACKEND",
        "AGENT_SANDBOX_IMAGE",
        "AGENT_RESTARTABLE_SANDBOX",
        "AGENT_SHARED_LIVE_SANDBOX",
        "AGENT_HOST_SLOTS",
        "AGENT_RUNNER_REPLICAS",
        "AGENT_SHARED_RESIDENT_CAPACITY",
        "AGENT_HOST_STATE_ROOT",
        "AGENT_DEPLOYMENT_SHA",
        "AGENT_ENROLL_TOKEN",
        "AGENT_RETAINED_DOCKER_DATA_ROOT",
        "STAGING_SHARED_LIVE_SANDBOX",
    ):
        env[name] = ""
    return env


def _run(env: dict[str, str], *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SANDBOX_HOST), *command],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_shared_live_refuses_a_half_enabled_host(tmp_path: Path) -> None:
    result = _run(_host(tmp_path, restartable="0"), "preflight")

    assert result.returncode != 0
    assert "requires AGENT_RESTARTABLE_SANDBOX=1" in result.stderr


def test_default_branch_workflow_activates_the_target_commits_staging_policy(
    tmp_path: Path,
) -> None:
    """workflow_run uses main's YAML but dev's deployment script."""
    result = _run(
        _host(
            tmp_path,
            include_rollout=False,
            github_workflow="Deploy Staging",
        ),
        "preflight",
    )

    assert result.returncode == 0, result.stderr
    assert "staging retained/shared-live rollout policy loaded" in result.stdout
    assert "retained storage verified" in result.stdout


def test_manual_staging_check_loads_policy_from_persistent_environment(tmp_path: Path) -> None:
    result = _run(
        _host(
            tmp_path,
            include_rollout=False,
            deployment_environment="staging",
        ),
        "preflight",
    )

    assert result.returncode == 0, result.stderr
    assert "staging retained/shared-live rollout policy loaded" in result.stdout
    assert "retained storage verified" in result.stdout


def test_staging_policy_overrides_an_old_generic_default_off_dotenv(tmp_path: Path) -> None:
    result = _run(
        _host(
            tmp_path,
            restartable="0",
            shared="0",
            github_workflow="Deploy Staging",
        ),
        "preflight",
    )

    assert result.returncode == 0, result.stderr
    assert "retained storage verified" in result.stdout


def test_staging_policy_has_an_explicit_emergency_off_switch(tmp_path: Path) -> None:
    result = _run(
        _host(
            tmp_path,
            github_workflow="Deploy Staging",
            staging_override="0",
        ),
        "preflight",
    )

    assert result.returncode == 0, result.stderr
    assert "retained storage verified" not in result.stdout


def test_shared_live_requires_explicit_host_capacity(tmp_path: Path) -> None:
    result = _run(_host(tmp_path, slots=""), "preflight")

    assert result.returncode != 0
    assert "AGENT_HOST_SLOTS" in result.stderr
    assert ">= 2" in result.stderr


def test_shared_live_refuses_more_slots_than_runner_executors(tmp_path: Path) -> None:
    result = _run(_host(tmp_path, slots="3", replicas="2"), "preflight")

    assert result.returncode != 0
    assert "overstates AGENT_RUNNER_REPLICAS=2" in result.stderr


def test_shared_live_check_requires_every_configured_runner_to_be_alive(tmp_path: Path) -> None:
    result = _run(_host(tmp_path, replicas="3", running_replicas=1), "check")

    assert result.returncode != 0
    assert "only 1 of 3 configured runner replicas are running" in result.stderr


def test_only_the_pending_target_commit_can_be_marked_ready(tmp_path: Path) -> None:
    env = _host(tmp_path)
    marker = Path(env["APP_DIR"]).parent / "host-state/shared-live-ready"
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


def test_shared_live_check_fails_when_runner_does_not_advertise(tmp_path: Path) -> None:
    result = _run(_host(tmp_path, capability=""), "check")

    assert result.returncode != 0
    assert "did not attest shared-sandbox-v1" in result.stderr


def test_shared_live_check_fails_for_a_rejected_host_credential(tmp_path: Path) -> None:
    result = _run(_host(tmp_path, credential_rejected=True), "check")

    assert result.returncode != 0
    assert "rejected this host's persisted credential" in result.stderr
