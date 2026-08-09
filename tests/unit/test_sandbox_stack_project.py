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
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX_HOST = REPO_ROOT / "deploy/host/sandbox_host.sh"
HOST_DEPLOY = REPO_ROOT / "deploy/host/deploy.sh"


def _host(tmp_path: Path, *, dotenv: str = "") -> dict[str, str]:
    """An APP_DIR with a .env, and a docker that records what it was asked."""
    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    (app_dir / ".env").write_text(dotenv)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    docker = fake_bin / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\nexit 0\n')
    docker.chmod(0o755)

    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "APP_DIR": str(app_dir),
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        # The caller's own environment must not decide this, or the .env
        # precedence under test never runs.
        "COMPOSE_PROJECT": "",
    }


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
    assert not Path(tmp_path / "docker.log").exists(), "it reached docker before checking"


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

    result = _run(env, "down")

    assert result.returncode == 0, result.stderr
    assert "compose -p from-the-environment" in Path(env["DOCKER_LOG"]).read_text()


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
        "AGENT_DISPATCHER_TOKEN=t",
        "AGENT_SANDBOX_IMAGE=freeinference-cloud-agent-job:prod",
        "AGENT_TUNNEL_SSH_DESTINATION=user@gateway",
        "AGENT_TUNNEL_SSH_KEY=/dev/null",
        "AGENT_TUNNEL_SSH_KNOWN_HOSTS=/etc/hosts",
        "",
    ]
)


def test_a_shared_machine_must_name_its_own_state_roots(tmp_path: Path) -> None:
    """Two stacks accepting the default paths do not sit beside each other.

    The three roots are host directories bind-mounted into the containers, and
    they default to the same `/var/lib/hybridinference/*` for every stack. The
    dangerous one is the enrolment credential: the control plane reads a host
    from the credential rather than from the claim body, so a shared state root
    means the second stack registers as the first — and `AGENT_RUNNER_HOST`
    cannot correct it, because nothing consults it after enrolment.
    """
    result = _run(_host(tmp_path, dotenv=_SHARED_HOST_ENV), "up")

    assert result.returncode != 0
    assert "AGENT_HOST_STATE_ROOT" in result.stderr
    assert "AGENT_WORKDIR_ROOT" in result.stderr
    assert "AGENT_SNAPSHOT_ROOT" in result.stderr


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

    assert "AGENT_WORKDIR_ROOT" not in result.stderr


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
