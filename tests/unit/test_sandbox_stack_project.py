"""The compose project is configuration, not a constant.

Staging and production share the runner host, so the project name is what keeps
their two stacks apart — it prefixes every container, image and network compose
creates. It used to be hardcoded, and a hardcoded value on a shared host is not
a collision anyone sees: the second `up` does not refuse, it *adopts* the first
stack's containers and recreates them with its own configuration. One
environment's deploy would silently reconfigure the other's runners.

So these pin two things. That nothing in `deploy/host/` names a project itself,
and that a compose command without one refuses rather than guessing.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX_HOST = REPO_ROOT / "deploy/host/sandbox_host.sh"
HOST_DEPLOY = REPO_ROOT / "deploy/host/deploy.sh"
HOST_ENV_EXAMPLE = REPO_ROOT / "deploy/host/.env.example"


def _host(
    tmp_path: Path, *, dotenv: str = "", compose_environment: str | None = None
) -> dict[str, str]:
    """An APP_DIR with a .env, and a docker that records what it was asked."""
    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    (app_dir / ".env").write_text(dotenv)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
printf "%s\\n" "$*" >> "$DOCKER_LOG"
case " $* " in
  *" config --environment "*)
    cat >/dev/null
    printf "%s\\n" "$COMPOSE_ENVIRONMENT"
    ;;
esac
exit 0
"""
    )
    docker.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "APP_DIR": str(app_dir),
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "COMPOSE_ENVIRONMENT": compose_environment if compose_environment is not None else dotenv,
    }
    # The caller's own environment must not decide this, or the .env precedence
    # under test never runs. Absence matters: Compose treats exported emptiness
    # as an override rather than falling back to dotenv.
    env.pop("COMPOSE_PROJECT", None)
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SANDBOX_HOST), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_compose_command_refuses_without_a_project(tmp_path: Path) -> None:
    """`down` with no project configured must not fall back to a default.

    A default here is a value somebody forgets to override exactly once, and the
    machine it is forgotten on is the one running both environments.
    """
    result = _run(_host(tmp_path), "down")

    assert result.returncode != 0
    assert "COMPOSE_PROJECT is not set" in result.stderr
    invocations = Path(tmp_path / "docker.log").read_text()
    assert "config --environment" in invocations
    assert " down" not in invocations, "it acted on a stack before checking"


def test_the_project_comes_from_dotenv(tmp_path: Path) -> None:
    """The value compose is given is the one this host's .env names."""
    env = _host(tmp_path, dotenv="COMPOSE_PROJECT=freeinference-cloud-agent-sandbox-staging\n")

    result = _run(env, "down")

    assert result.returncode == 0, result.stderr
    invocations = Path(env["DOCKER_LOG"]).read_text()
    assert "compose -p freeinference-cloud-agent-sandbox-staging" in invocations
    assert "-f deploy/host/docker-compose.sandbox.yml" in invocations


def test_the_environment_beats_dotenv(tmp_path: Path) -> None:
    """`deploy.sh` exports it, and an export has to win over the file.

    Both are legitimate sources — the deploy passes it, an operator running the
    script by hand relies on the file — and the deploy is the one that must be
    able to say which stack it is deploying.
    """
    env = _host(tmp_path, dotenv="COMPOSE_PROJECT=from-dotenv\n")
    env["COMPOSE_PROJECT"] = "from-the-environment"
    env["COMPOSE_ENVIRONMENT"] = "COMPOSE_PROJECT=from-the-environment\n"

    result = _run(env, "down")

    assert result.returncode == 0, result.stderr
    assert "compose -p from-the-environment" in Path(env["DOCKER_LOG"]).read_text()


def test_an_empty_environment_value_also_beats_dotenv(tmp_path: Path) -> None:
    """Compose treats exported emptiness as an override, not as absence."""
    env = _host(tmp_path, dotenv="COMPOSE_PROJECT=from-dotenv\n")
    env["COMPOSE_PROJECT"] = ""
    env["COMPOSE_ENVIRONMENT"] = "COMPOSE_PROJECT=\n"

    result = _run(env, "down")

    assert result.returncode != 0
    assert "COMPOSE_PROJECT is not set" in result.stderr


def test_no_project_name_is_written_into_the_scripts() -> None:
    """The old constant is gone from both halves of the host deploy.

    `deploy.sh` reads the same value `sandbox_host.sh` does and exports it, so
    the stray-stack guard and the bring-up cannot disagree about which
    containers belong to this deployment — which is the disagreement that
    produced two workspace brokers on this host once already.
    """
    bringup = SANDBOX_HOST.read_text()
    deploy = HOST_DEPLOY.read_text()

    assert "-p hybridinference-agent-remote" not in bringup
    assert "EXPECTED_PROJECT=hybridinference-agent-remote" not in deploy
    assert 'EXPECTED_PROJECT="${COMPOSE_PROJECT}"' in deploy
    assert "export COMPOSE_PROJECT" in deploy


def test_a_named_sibling_environment_is_not_a_stray_stack() -> None:
    """The guard has to allow the other environment on this same machine.

    It exists to catch a *duplicate of this stack* under another project name.
    Once staging and production share a host, the other environment matches that
    description exactly, so it is named rather than guessed at: anything not in
    `SIBLING_COMPOSE_PROJECTS` is still reported.
    """
    deploy = HOST_DEPLOY.read_text()

    assert 'KNOWN="${EXPECTED_PROJECT},${SIBLING_COMPOSE_PROJECTS}"' in deploy
    # And read the same way as everything else: a variable honoured from the
    # environment but not from `.env` is a line an operator writes, restarts,
    # and watches do nothing.
    assert "from_env SIBLING_COMPOSE_PROJECTS" in deploy


_SHARED_HOST_ENV = "\n".join(
    [
        "COMPOSE_PROJECT=freeinference-cloud-agent-sandbox-prod",
        "SIBLING_COMPOSE_PROJECTS=freeinference-cloud-agent-sandbox-staging",
        "AGENT_ENROLL_TOKEN=test-enrollment-token",
        "AGENT_SANDBOX_IMAGE=freeinference-cloud-agent-job:prod",
        "AGENT_TUNNEL_SSH_DESTINATION=user@gateway",
        "AGENT_TUNNEL_SSH_KEY=/dev/null",
        "AGENT_TUNNEL_SSH_KNOWN_HOSTS=/etc/hosts",
        "AGENT_MCP_TUNNEL_SSH_DESTINATION=user@control",
        "AGENT_MCP_TUNNEL_SSH_KEY=/dev/null",
        "AGENT_MCP_TUNNEL_SSH_KNOWN_HOSTS=/etc/hosts",
        "AGENT_CONTROL_PLANE_TUNNEL_SSH_DESTINATION=user@control",
        "AGENT_CONTROL_PLANE_TUNNEL_SSH_KEY=/dev/null",
        "AGENT_CONTROL_PLANE_TUNNEL_SSH_KNOWN_HOSTS=/etc/hosts",
        "AGENT_BROKER_REVERSE_BIND=172.24.0.1",
        "AGENT_BROKER_REVERSE_PORT=18092",
        "AGENT_WORKSPACE_BROKER_ADVERTISE_URL=http://172.24.0.1:18092",
        "AGENT_WORKSPACE_BROKER_TOKEN=test-broker-token",
        "",
    ]
)


def test_a_shared_machine_must_name_its_own_host_state_root(tmp_path: Path) -> None:
    """Two stacks may not share the persisted host enrollment identity.

    Chat data has no host worktree/snapshot root. The one remaining host bind
    contains the enrolment credential: the control plane reads a host from the
    credential rather than from the claim body, so sharing it makes the second
    stack register as the first and AGENT_RUNNER_HOST cannot correct it.
    """
    result = _run(_host(tmp_path, dotenv=_SHARED_HOST_ENV), "up")

    assert result.returncode != 0
    assert "AGENT_HOST_STATE_ROOT" in result.stderr


def test_the_host_template_assigns_each_setting_once() -> None:
    """The final dotenv assignment wins, so duplicate defaults bypass the guard."""
    template = HOST_ENV_EXAMPLE.read_text()
    assignments: dict[str, list[str]] = {}
    for name, value in re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", template, flags=re.MULTILINE):
        assignments.setdefault(name, []).append(value)
    duplicates = {name: values for name, values in assignments.items() if len(values) != 1}
    assert not duplicates, f"dotenv uses the final duplicate assignment: {duplicates}"
    for name in (
        "AGENT_HOST_STATE_ROOT",
        "AGENT_EGRESS_NETWORK_PLATFORM_ONLY",
    ):
        assert assignments[name] == [""], f"{name} must stay explicit in the template"


def test_a_shared_machine_refuses_the_legacy_shared_state_roots(tmp_path: Path) -> None:
    """Spelling the old defaults explicitly is still cross-environment sharing."""
    dotenv = _SHARED_HOST_ENV + "\n".join(
        [
            "AGENT_HOST_STATE_ROOT=/var/lib/hybridinference/agent-host",
            "AGENT_WORKDIR_ROOT=/var/lib/hybridinference/agent-jobs",
            "AGENT_SNAPSHOT_ROOT=/var/lib/hybridinference/agent-snapshots",
            "AGENT_EGRESS_NETWORK_PLATFORM_ONLY=freeinference-cloud-agent-egress-prod",
            "",
        ]
    )

    result = _run(_host(tmp_path, dotenv=dotenv), "up")

    assert result.returncode != 0
    assert "AGENT_HOST_STATE_ROOT" in result.stderr


def test_a_shared_machine_refuses_the_legacy_shared_egress_network(tmp_path: Path) -> None:
    """Duplicate tunnel aliases on one Docker network cross the environment boundary."""
    dotenv = _SHARED_HOST_ENV + "\n".join(
        [
            "AGENT_HOST_STATE_ROOT=/var/lib/freeinference-cloud-agent-prod/host",
            "AGENT_WORKDIR_ROOT=/var/lib/freeinference-cloud-agent-prod/jobs",
            "AGENT_SNAPSHOT_ROOT=/var/lib/freeinference-cloud-agent-prod/snapshots",
            "AGENT_EGRESS_NETWORK_PLATFORM_ONLY=agent-egress",
            "",
        ]
    )

    result = _run(_host(tmp_path, dotenv=dotenv), "up")

    assert result.returncode != 0
    assert "AGENT_EGRESS_NETWORK_PLATFORM_ONLY" in result.stderr


def test_the_shared_guard_uses_compose_resolved_values(tmp_path: Path) -> None:
    """Interpolation and inline comments must not disguise shared defaults."""
    dotenv = _SHARED_HOST_ENV + "\n".join(
        [
            "STATE_BASE=/var/lib/hybridinference",
            "AGENT_HOST_STATE_ROOT=${STATE_BASE}/agent-host",
            "AGENT_WORKDIR_ROOT=/var/lib/freeinference-cloud-agent-prod/jobs",
            "AGENT_SNAPSHOT_ROOT=/var/lib/freeinference-cloud-agent-prod/snapshots",
            "AGENT_EGRESS_NETWORK_PLATFORM_ONLY=agent-egress # legacy shared name",
            "",
        ]
    )
    resolved = dotenv.replace(
        "AGENT_HOST_STATE_ROOT=${STATE_BASE}/agent-host",
        "AGENT_HOST_STATE_ROOT=/var/lib/hybridinference/agent-host",
    ).replace(
        "AGENT_EGRESS_NETWORK_PLATFORM_ONLY=agent-egress # legacy shared name",
        "AGENT_EGRESS_NETWORK_PLATFORM_ONLY=agent-egress",
    )

    result = _run(_host(tmp_path, dotenv=dotenv, compose_environment=resolved), "up")

    assert result.returncode != 0
    assert "AGENT_HOST_STATE_ROOT" in result.stderr
    assert "AGENT_EGRESS_NETWORK_PLATFORM_ONLY" in result.stderr


def test_the_shared_guard_canonicalizes_state_roots(tmp_path: Path) -> None:
    """Equivalent legacy paths must not evade an exact string comparison."""
    dotenv = _SHARED_HOST_ENV + "\n".join(
        [
            "AGENT_HOST_STATE_ROOT=/var/lib/hybridinference/agent-host/../agent-host///",
            "AGENT_WORKDIR_ROOT=/var/lib/freeinference-cloud-agent-prod/jobs",
            "AGENT_SNAPSHOT_ROOT=/var/lib/freeinference-cloud-agent-prod/snapshots",
            "AGENT_EGRESS_NETWORK_PLATFORM_ONLY=freeinference-cloud-agent-egress-prod",
            "",
        ]
    )

    result = _run(_host(tmp_path, dotenv=dotenv), "up")

    assert result.returncode != 0
    assert "AGENT_HOST_STATE_ROOT" in result.stderr


def test_ready_marker_is_owned_by_the_deployment_checkout(tmp_path: Path) -> None:
    """Compose enrollment paths must not redirect deployment metadata writes."""
    state_root = tmp_path / "host-state"
    state_root.mkdir()
    dotenv = "AGENT_HOST_STATE_ROOT=${STATE_BASE}/host-state\n"
    resolved = f"AGENT_HOST_STATE_ROOT={state_root}\n"
    env = _host(tmp_path, dotenv=dotenv, compose_environment=resolved)
    marker = Path(env["APP_DIR"]) / ".shared-live-ready"
    marker.write_text("pending:target-sha\n")

    result = _run(env, "mark-ready", "target-sha")

    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "target-sha\n"
    assert not (state_root / "shared-live-ready").exists()


def test_a_lone_stack_needs_no_such_thing(tmp_path: Path) -> None:
    """A host running one environment has nothing to collide with.

    The requirement is tied to `SIBLING_COMPOSE_PROJECTS` rather than applied
    always, so the single-stack deployments that predate co-location keep
    working without configuring paths they do not care about. This gets past
    `require_env` — it fails later, in the isolation preflight, which is a
    different check and the point here.
    """
    alone = _SHARED_HOST_ENV.replace(
        "SIBLING_COMPOSE_PROJECTS=freeinference-cloud-agent-sandbox-staging", ""
    )

    result = _run(_host(tmp_path, dotenv=alone), "up")

    assert "AGENT_HOST_STATE_ROOT" not in result.stderr


def test_the_stray_matcher_still_recognises_this_stack(tmp_path: Path) -> None:
    """The container-name pattern has to survive the service rename.

    Services lost their redundant `agent-` prefix, so a container is now
    `<project>-runner-1` where it was `<project>-agent-runner-1`. A pattern
    written for the old shape would report nothing at all — a guard that has
    stopped looking reads exactly like a host with no strays.
    """
    program = tmp_path / "guard.awk"
    deploy = HOST_DEPLOY.read_text()
    start = deploy.index("BEGIN { n = split(known")
    end = deploy.index("}')", start) + 1  # keep the action block's closing brace
    program.write_text(deploy[start:end])

    listing = "\n".join(
        [
            "ours ours-runner-1",
            "ours ours-workspace-broker-1",
            "sibling sibling-runner-1",
            "agenthost agenthost-agent-gateway-tunnel-1",
            "legacy legacy-agent-workspace-broker-1",
            "unrelated some-postgres",
        ]
    )
    result = subprocess.run(
        ["awk", "-v", "known=ours,sibling", "-f", str(program)],
        input=listing,
        capture_output=True,
        text=True,
        check=True,
    )

    reported = result.stdout
    assert "agenthost-agent-gateway-tunnel-1" in reported
    assert "legacy-agent-workspace-broker-1" in reported, "the pre-rename shape must still match"
    assert "ours-runner-1" not in reported
    assert "sibling-runner-1" not in reported
    assert "some-postgres" not in reported
