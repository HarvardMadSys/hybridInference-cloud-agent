"""Static checks on the self-hosted runner deployment.

A compose file cannot be unit-tested by running it, but the properties that
matter here are structural: an egress network that is not actually internal, or
a flag the runner does not implement, are silent failures — the first weakens
the sandbox boundary without anyone noticing, the second only shows up when a
runner container crash-loops in production.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from cloud_agent_host.runner import build_parser

REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = REPO_ROOT / "deploy/host/docker-compose.sandbox.yml"
_CONTROL_PLANE_COMPOSE = REPO_ROOT / "deploy/control-plane/docker-compose.yml"
_CONTROL_PLANE_DEPLOY = REPO_ROOT / "deploy/control-plane/deploy.sh"
_LOCAL_COMPOSE = REPO_ROOT / "deploy/local/docker-compose.yml"
_LOCAL_DEV_SCRIPT = REPO_ROOT / "deploy/local/dev.sh"
_LOCAL_EDGE_DOCKERFILE = REPO_ROOT / "deploy/local/Dockerfile.edge"
_LOCAL_NGINX = REPO_ROOT / "deploy/local/nginx.conf"
_DOCKERFILE = Path(__file__).resolve().parents[2] / "deploy/sandbox/Dockerfile.agent-sandbox"
_CODEX_REQUIREMENTS = Path(__file__).resolve().parents[2] / "deploy/sandbox/codex-requirements.toml"


@pytest.fixture(scope="module")
def compose() -> dict:
    """Parse the runner compose overlay."""
    return yaml.safe_load(_COMPOSE.read_text())


@pytest.fixture(scope="module")
def local_compose() -> dict:
    """Parse the Docker-only complete local stack overlay."""
    return yaml.safe_load(_LOCAL_COMPOSE.read_text())


def test_local_dev_script_upgrades_an_existing_env_without_rotating_it(tmp_path: Path) -> None:
    """A checkout update must not require deleting local credentials first."""
    script = tmp_path / "repo/deploy/local/dev.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(_LOCAL_DEV_SCRIPT, script)
    dotenv = script.parent / ".env.local"
    dotenv.write_text(
        "AGENT_SESSION_SECRET=keep-this-secret\n"
        "AGENT_WORKSPACE_BROKER_TOKEN=keep-this-broker-token\n"
        "LOCAL_EDGE_PORT=23002\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Force the script's documented standalone fallback even on CI hosts that
    # have a real Compose plugin. Otherwise `docker compose version` escapes
    # this hermetic fixture and the real plugin tries to render files that the
    # deliberately minimal temporary checkout does not contain.
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 1\n")
    fake_docker.chmod(0o755)
    fake_compose = fake_bin / "docker-compose"
    compose_env = tmp_path / "compose-env"
    fake_compose.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$GATEWAY_AUTHORIZE_URL" "$AGENT_SESSION_REDIRECT_URI" '
        '"$AGENT_WEB_BASE_URL" >"$COMPOSE_ENV_LOG"\n'
    )
    fake_compose.chmod(0o755)

    result = subprocess.run(
        ["sh", str(script), "config"],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "COMPOSE_ENV_LOG": str(compose_env),
            # A second local stack should not inherit the URLs persisted for
            # the first one's port.
            "LOCAL_EDGE_PORT": "24002",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    values = dict(
        line.split("=", 1)
        for line in dotenv.read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert values["AGENT_SESSION_SECRET"] == "keep-this-secret"
    assert values["AGENT_WORKSPACE_BROKER_TOKEN"] == "keep-this-broker-token"
    assert values["LOCAL_PROJECT_NAME"] == "freeinference-local"
    assert values["LOCAL_AGENT_EGRESS_NETWORK"] == "freeinference-local-agent-egress"
    assert values["LOCAL_CHAT_NETWORK_POOL"] == "10.240.0.0/16"
    assert values["GATEWAY_AUTHORIZE_URL"] == "http://127.0.0.1:23002/authorize"
    assert values["AGENT_SESSION_REDIRECT_URI"] == (
        "http://127.0.0.1:23002/api/v1/session/callback"
    )
    assert values["AGENT_WEB_BASE_URL"] == "http://127.0.0.1:23002"
    assert values["AGENT_WEB_GATEWAY_URL"] == ""
    assert compose_env.read_text().splitlines() == [
        "http://127.0.0.1:24002/authorize",
        "http://127.0.0.1:24002/api/v1/session/callback",
        "http://127.0.0.1:24002",
    ]


def test_local_dev_refuses_to_replace_lost_credentials_for_an_existing_database(
    tmp_path: Path,
) -> None:
    script = tmp_path / "repo/deploy/local/dev.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(_LOCAL_DEV_SCRIPT, script)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_compose = fake_bin / "docker-compose"
    fake_compose.write_text("#!/bin/sh\nexit 0\n")
    fake_compose.chmod(0o755)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  'volume ls --quiet --filter label=com.docker.compose.project=freeinference-local "
        "--filter label=com.docker.compose.volume=agent-postgres')\n"
        "    echo freeinference-local_agent-postgres ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    fake_docker.chmod(0o755)

    result = subprocess.run(
        ["sh", str(script), "config"],
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not (script.parent / ".env.local").exists()
    assert "still owns its Postgres volume" in result.stderr
    assert "will not delete the volume or invent a new password" in result.stderr


@pytest.mark.parametrize("prebuilt", [False, True])
def test_local_up_stops_before_full_stack_when_retained_database_auth_fails(
    tmp_path: Path,
    prebuilt: bool,
) -> None:
    script = tmp_path / "repo/deploy/local/dev.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(_LOCAL_DEV_SCRIPT, script)
    (script.parent / ".env.local").write_text(
        "AGENT_POSTGRES_PASSWORD=wrong-retained-password\n"
        "AGENT_DATABASE_URL=postgresql://cloudagent:wrong-retained-password@postgres/cloudagent\n"
        "AGENT_WORKSPACE_BROKER_TOKEN=keep\n"
        "LOCAL_PROJECT_NAME=freeinference-local\n"
        "LOCAL_AGENT_EGRESS_NETWORK=freeinference-local-agent-egress\n"
        "LOCAL_CHAT_NETWORK_POOL=10.240.0.0/16\n"
        "LOCAL_EDGE_PORT=13002\n"
        "GATEWAY_AUTHORIZE_URL=http://127.0.0.1:13002/authorize\n"
        "AGENT_SESSION_REDIRECT_URI=http://127.0.0.1:13002/api/v1/session/callback\n"
        "AGENT_WEB_BASE_URL=http://127.0.0.1:13002\n"
        "AGENT_WEB_GATEWAY_URL=\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    compose_log = tmp_path / "compose.log"
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 1\n")
    docker.chmod(0o755)
    compose = fake_bin / "docker-compose"
    compose.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >>"$COMPOSE_LOG"\n'
        'case "$*" in\n'
        "  *' exec -T postgres pg_isready') exit 0 ;;\n"
        "  *' run --rm --no-deps migrate python -c '*) exit 17 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    compose.chmod(0o755)

    result = subprocess.run(
        ["sh", str(script), "up", *(["--no-build"] if prebuilt else [])],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "COMPOSE_LOG": str(compose_log),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    calls = compose_log.read_text()

    assert result.returncode != 0
    assert (" build sandbox-image migrate" in calls) is not prebuilt
    assert " up --detach postgres" in calls
    assert " run --rm --no-deps migrate python -c" in calls
    assert " up --detach --build" not in calls
    assert "cannot authenticate to the existing Postgres volume" in result.stderr


def test_complete_local_stack_exposes_only_its_loopback_edge(local_compose: dict):
    services = local_compose["services"]

    assert services["local-edge"]["ports"] == ["127.0.0.1:${LOCAL_EDGE_PORT:-13002}:8088"]
    assert "edge" not in services
    assert services["local-gateway"]["environment"]["LOCAL_EDGE_PORT"] == (
        "${LOCAL_EDGE_PORT:-13002}"
    )
    assert services["local-gateway"]["environment"]["LOCAL_IDENTITY_ALLOWED_REDIRECTS"] == (
        "${LOCAL_IDENTITY_ALLOWED_REDIRECTS:-}"
    )
    assert "ports" not in services["local-gateway"]
    assert "ports" not in services["local-repo"]


def test_complete_local_sandbox_network_has_only_narrow_relays(local_compose: dict):
    on_egress = {
        name
        for name, service in local_compose["services"].items()
        if "agent-egress" in (service.get("networks") or {})
    }

    assert local_compose["networks"]["agent-egress"]["internal"] is True
    assert on_egress == {"local-gateway", "mcp-relay"}


def test_mcp_only_listener_receives_the_connection_decryption_key() -> None:
    """A healthy relay must be able to resolve the owner's stored connection."""
    control_compose = yaml.safe_load(_CONTROL_PLANE_COMPOSE.read_text())
    relay_env = control_compose["services"]["mcp-relay"]["environment"]

    assert "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY" in relay_env
    assert "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY" in relay_env["AGENT_SOURCE_CONTROL_ENCRYPTION_KEY"]


def test_complete_local_host_data_is_docker_owned(local_compose: dict):
    """Runner/broker may control Docker but may not map app data from the host."""
    named = set(local_compose["volumes"])
    control = local_compose["services"]["control-plane"]
    assert control["environment"]["AGENT_GITHUB_APP_PRIVATE_KEY_PATH"] == ""
    assert control["volumes"] == ["local-empty-github-key:/etc/cloud-agent/github-app.pem:ro"]
    for service_name in ("runner", "workspace-broker"):
        for mount in local_compose["services"][service_name]["volumes"]:
            source = _split_mount(mount)[0]
            if "docker.sock" in source:
                assert mount.endswith(":ro")
            else:
                assert source in named, f"{service_name} maps host data through {mount}"


def test_complete_local_stack_runs_every_native_session_driver(local_compose: dict):
    from cloud_agent_host.runtimes import registered_runtimes

    expected = {"claude-code", "codex", "pi", "opencode", "kilo"}
    assert set(registered_runtimes()) == expected
    for service_name in ("runner", "workspace-broker"):
        configured = local_compose["services"][service_name]["environment"]
        assert "AGENT_RESTARTABLE_RUNTIMES" not in configured
        assert "AGENT_SHARED_LIVE_RUNTIMES" not in configured


def test_complete_local_edge_reresolves_recreated_services():
    """Rebuilding one service must not strand the still-running edge on its old IP."""
    config = _LOCAL_NGINX.read_text()

    assert "FROM nginx:1.27.5-alpine" in _LOCAL_EDGE_DOCKERFILE.read_text()
    assert "resolver 127.0.0.11" in config
    for upstream in ("control-plane:8000", "web:3000", "local-gateway:8080"):
        assert f"server {upstream} resolve;" in config


def test_the_standalone_edge_is_opt_in_and_loopback():
    """The edge exists only for a deployment that owns its hostname.

    Under the gateway's hostname the console already splits `/agents/*`
    between the two services, so an edge that started unconditionally would
    be a second, unused listener on every deployment — and one that dropped
    its profile by accident would appear on the next deploy of all of them.
    Loopback by default for the same reason every other published port is:
    the tunnel is on the host, and nothing else should reach it.
    """
    # The `compose` fixture is the runner host's stack; the edge lives beside
    # the control plane and the web app, in the other file.
    edge = yaml.safe_load(_CONTROL_PLANE_COMPOSE.read_text())["services"]["edge"]
    assert edge["profiles"] == ["edge"]
    assert edge["ports"] == ["${AGENT_EDGE_BIND:-127.0.0.1}:${AGENT_EDGE_PORT:-3003}:80"]
    assert edge["networks"] == ["control-plane"]


def test_the_standalone_edge_strips_the_api_prefix():
    """`/api/` must reach the control plane as `/`.

    The control plane serves `/v1/...` at its root, exactly as it does behind
    the console (whose rewrite strips `/agents/api`). A `proxy_pass` without
    the trailing slash forwards the prefix, and every API call 404s while the
    HTML still loads — a failure that reads as a client bug.
    """
    text = (REPO_ROOT / "deploy/control-plane/edge.nginx.conf").read_text()
    api = text[text.index("location /api/") : text.index("location / ")]
    assert "proxy_pass http://cloud_agent_api/;" in api
    assert "proxy_buffering off;" in api, "the control plane streams events over SSE"


def test_the_standalone_edge_is_probed_before_a_deploy_succeeds():
    """An enabled edge must prove both routes before recording the deploy.

    Checking the services directly does not catch an nginx crash or a broken
    path split: both upstreams can be healthy while the public hostname is
    returning 502 or sending API requests to the web app.
    """
    text = _CONTROL_PLANE_DEPLOY.read_text()
    before_success = text[: text.index("printf '%s\\n' \"${SHA}\" > .deployed-sha")]
    edge_checks = before_success[before_success.index('if grep -qx edge <<<"${ACTIVE_SERVICES}"') :]

    assert 'ACTIVE_SERVICES="$(compose config --services)"' in before_success
    assert 'wait_for "edge web route" "http://127.0.0.1:${EDGE_PORT}/"' in edge_checks
    assert 'wait_for "edge API route" "http://127.0.0.1:${EDGE_PORT}/api/healthz"' in edge_checks


def test_root_mounted_web_build_accepts_an_explicit_empty_base_path():
    """Compose must distinguish unset from explicitly empty for root hosting."""
    text = _CONTROL_PLANE_COMPOSE.read_text()

    assert "${AGENT_WEB_BASE_PATH?" in text
    assert "${AGENT_WEB_BASE_PATH:?" not in text


def test_docker_build_context_excludes_local_dependency_trees():
    """Local installs must not turn every image build into a 700 MB upload."""
    ignored = (REPO_ROOT / ".dockerignore").read_text().splitlines()

    assert ".venv" in ignored
    assert "web/node_modules" in ignored
    assert "web/.next" in ignored


def test_egress_network_is_internal(compose: dict):
    """The sandbox network must have no route off the host.

    This is the PlatformOnly tier expressed as infrastructure: a sandbox on an
    internal network reaches the gateway and nothing else, whatever the agent
    inside decides to try.
    """
    assert compose["networks"]["agent-egress"]["internal"] is True


def test_runner_mounts_the_docker_socket_read_only(compose: dict):
    """The runner starts sandboxes; it has no business reconfiguring the daemon."""
    volumes = compose["services"]["runner"]["volumes"]
    socket_mounts = [v for v in volumes if "docker.sock" in v]
    assert socket_mounts, "the runner needs the daemon to spawn sandboxes"
    assert all(v.endswith(":ro") for v in socket_mounts)


def test_default_backend_is_vm_isolated(compose: dict):
    """Self-hosted defaults to a kernel boundary per job, not a shared one."""
    env = compose["services"]["runner"]["environment"]
    assert "kata" in env["AGENT_SANDBOX_BACKEND"]


def test_runner_uses_enrollment_not_a_shared_claim_secret(compose: dict):
    env = compose["services"]["runner"]["environment"]
    assert "AGENT_DISPATCHER_TOKEN" not in env
    assert "AGENT_ENROLL_TOKEN" in env
    assert "AGENT_HOST_CREDENTIAL_FILE" in env


def test_compose_only_uses_flags_the_runner_implements(compose: dict):
    """Every flag in the compose command must exist, or the container crash-loops."""
    command = compose["services"]["runner"]["command"]
    flags = {arg for arg in command if isinstance(arg, str) and arg.startswith("--")}
    known = {
        action_option
        for action in build_parser()._actions
        for action_option in action.option_strings
    }
    assert flags <= known, f"compose passes unknown flags: {flags - known}"


def test_sandbox_image_carries_no_credentials():
    """No secret may be baked into the image the agent runs in."""
    text = _DOCKERFILE.read_text()
    for forbidden in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "AGENT_DISPATCHER_TOKEN", "hyi-"):
        assert forbidden not in text, f"{forbidden} must never be baked into the sandbox image"


def test_sandbox_image_runs_as_non_root():
    """A kernel escape should have to start from an unprivileged user."""
    text = _DOCKERFILE.read_text()
    assert "USER agent" in text
    assert text.index("USER agent") < text.index("WORKDIR /workspace")


def test_sandbox_image_disables_agent_phone_home():
    """Telemetry and auto-update are off, so a blocked request is not a mystery timeout."""
    text = _DOCKERFILE.read_text()
    for flag in (
        "DISABLE_TELEMETRY",
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ):
        assert flag in text


def test_codex_mcp_is_constrained_to_an_empty_requirements_allowlist():
    """User, project, and plugin MCP servers are disabled after config merge.

    Codex 0.145 treats the presence of top-level ``mcp_servers`` requirements
    as the server allowlist. The same empty allowlist is applied a second time
    to plugin-provided MCP servers, so an empty table is stronger than
    ``--ignore-user-config`` alone.
    """
    requirements = tomllib.loads(_CODEX_REQUIREMENTS.read_text())
    assert requirements == {"mcp_servers": {}}
    assert "codex-requirements.toml /etc/codex/requirements.toml" in _DOCKERFILE.read_text()


# ── the chain from compose file to a running sandbox ───────────────────
#
# Each of these encodes a way the self-hosted deployment failed while every
# individual file looked correct in isolation. They are static because the
# alternative is discovering them one at a time on a remote host.

_RUNNER_DOCKERFILE = Path(__file__).resolve().parents[2] / "deploy/host/Dockerfile.agent-runner"


def _split_mount(mount: str) -> list[str]:
    """Split a compose volume on its separators, not on the ones inside ``${}``.

    ``${VAR:-/default}`` contains a colon of its own, so a naive split reports
    a mismatch that is not there.
    """
    parts: list[str] = []
    depth = 0
    current = ""
    for char in mount:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        if char == ":" and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return parts


def _expand(value: str) -> str:
    """Resolve ``${VAR:-default}`` to its default, as an unset environment would."""
    return re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", r"\1", value)


def test_the_egress_network_name_is_pinned(compose: dict):
    """Compose prefixes generated network names with the project name.

    The runner passes this value straight to ``docker run --network``, so
    without an explicit name the sandbox is asked to join `agent-egress` while
    the network that exists is `hybridinference_agent-egress`, and every spawn
    fails.
    """
    # Interpolated off the tier variable, so the network compose creates, the
    # one the gateway joins and the one the runner spawns onto cannot diverge.
    assert _expand(compose["networks"]["agent-egress"]["name"]) == "agent-egress"


def test_something_answers_on_the_sandbox_network(compose: dict):
    """An internal network with only the sandbox on it is a network to nowhere.

    Model calls and event reporting both go to the gateway; if nothing on this
    network can carry them, the sandbox fails at its first request. The source
    asserted the gateway service itself sat here; in this topology the tunnel
    stands in for it, which is the whole point of the remote form.
    """
    on_egress = {
        name
        for name, svc in compose["services"].items()
        if "agent-egress" in (svc.get("networks") or {})
    }
    # **Exactly these two, named.** The sandbox has two narrow destinations
    # now — the gateway for model calls, the relay for tools — and this is the
    # assertion that keeps "narrow" true. Loosening it to "at least one" or "a
    # non-empty set" is the natural way to make it pass after adding a service,
    # and it would let the next addition put anything at all on the closed
    # network without a word.
    assert on_egress == {"gateway-tunnel", "mcp-tunnel"}


def test_the_gateway_name_resolves_on_every_network_that_is_given_it(compose: dict):
    """**The name is handed to two services on two different networks.**

    The runner sits on `hybridinference` and the sandbox on `agent-egress`, and
    both are handed the same `AGENT_GATEWAY_URL`. A Docker network alias is
    per-network, so declaring it on one resolves for one of them and NXDOMAINs
    for the other — the runner claims a job and then cannot reach the gateway
    to report a single event.

    Caught in review: the alias was on `agent-egress` only, while the comment
    above it already claimed both. Nothing here compared the name a service is
    *given* against the networks it can resolve it on, so both halves looked
    right in isolation.
    """
    tunnel = compose["services"]["gateway-tunnel"]
    hostname = "${AGENT_GATEWAY_HOSTNAME:-agent-gateway}"

    # Which networks can resolve the name.
    resolves_on = {
        network
        for network, config in (tunnel.get("networks") or {}).items()
        if hostname in ((config or {}).get("aliases") or [])
    }

    # Which networks a service that was handed the name actually sits on.
    needs_it: set[str] = set()
    for svc in compose["services"].values():
        env = svc.get("environment") or {}
        if not any(hostname in str(value) for value in env.values()):
            continue
        networks = svc.get("networks")
        needs_it.update(networks if isinstance(networks, list) else (networks or {}).keys())

    # The sandbox is not a compose service — the runner starts it on the egress
    # network and passes the same URL down, so that network counts too.
    needs_it.add(
        compose["services"]["runner"]["environment"]["AGENT_EGRESS_NETWORK_PLATFORM_ONLY"]
        .split(":-")[-1]
        .rstrip("}")
    )

    unresolvable = sorted(needs_it - resolves_on - {"agent-egress"})
    assert not unresolvable, (
        f"services on {unresolvable} are given {hostname} but cannot resolve it; "
        f"the alias is declared only on {sorted(resolves_on)}"
    )
    assert "agent-egress" in resolves_on, "the sandbox network cannot resolve the gateway name"


def test_the_tunnel_key_is_allowed_the_one_thing_it_exists_to_do(compose: dict):
    """`restrict` disables port forwarding, and `permitopen` does not re-enable it.

    The documented authorized_keys line is the whole security model for this
    credential: one forwarded port, no shell. Written as `restrict,permitopen=`
    it is not a tight rule — it is a *broken* one, and the tunnel simply never
    comes up, which reads as a network problem rather than a typo in a doc.
    """
    # Whichever of these the branch has. `docs/operations.md` arrives later in
    # the migration; a test that assumed the whole stack was present would fail
    # on the very branch that owns the script.
    candidates = [
        REPO_ROOT / "deploy/host/sandbox_host.sh",
        REPO_ROOT / "docs/operations.md",
    ]
    checked = 0
    for path in (candidate for candidate in candidates if candidate.exists()):
        rel = path.relative_to(REPO_ROOT)
        for line in path.read_text().splitlines():
            if "permitopen" not in line or "restrict" not in line:
                continue
            checked += 1
            assert "port-forwarding" in line, (
                f"{rel} documents `restrict` with `permitopen` and no "
                "`port-forwarding`; ssh will refuse the forward this key exists for"
            )
    assert checked, "no authorized_keys example was found to check"


def test_a_failed_tunnel_probe_is_not_reported_as_a_successful_start() -> None:
    """`up` must not print "done" after the probe said a sandbox cannot reach
    the gateway.

    The subshell is deliberate — `die` exits, and the runner logs below it are
    what explain the failure — but discarding its status meant the diagnosis
    scrolled past and the last line said the deploy worked.
    """
    script = (REPO_ROOT / "deploy/host/sandbox_host.sh").read_text()
    assert "( cmd_check ) || true" not in script, (
        "the tunnel probe's verdict is discarded; a failed check still reports success"
    )
    assert "probe_ok" in script


def test_job_worktrees_are_not_host_mounted(compose: dict):
    """Docker writable layers are the only local chat storage."""
    volumes = compose["services"]["runner"]["volumes"]
    workdir_mounts = [v for v in volumes if "agent-jobs" in v]
    assert workdir_mounts == []


def test_workspace_broker_and_runner_share_no_host_worktree(compose: dict):
    """Files, Terminal, and Git resolve the shared container through Docker."""
    services = compose["services"]
    runner_mounts = {mount for mount in services["runner"]["volumes"] if "agent-jobs" in mount}
    broker_mounts = {
        mount for mount in services["workspace-broker"]["volumes"] if "agent-jobs" in mount
    }

    assert broker_mounts == runner_mounts
    assert broker_mounts == set()
    assert "tmpfs" not in compose["services"]["workspace-broker"]
    # The source also asserted the gateway's own `backend` service pointed at
    # the broker on :8092. Here the gateway is not a service — it is reached
    # through the tunnel — so the caller-side half of that pairing lives in the
    # control plane's configuration, not in this file.


def test_shared_chat_timeout_matches_and_no_legacy_gate_remains(compose: dict):
    services = compose["services"]
    runner_env = services["runner"]["environment"]
    broker_env = services["workspace-broker"]["environment"]

    for name in ("AGENT_RESTARTABLE_SANDBOX", "AGENT_SHARED_LIVE_SANDBOX"):
        assert name not in runner_env
        assert name not in broker_env
    assert runner_env["AGENT_CHAT_IDLE_TIMEOUT_S"] == broker_env["AGENT_CHAT_IDLE_TIMEOUT_S"]


def test_runner_receives_exact_sha_rollout_metadata(compose: dict) -> None:
    """Runners read deployment metadata without owning its host directory."""
    runner = compose["services"]["runner"]
    env = runner["environment"]

    assert env["AGENT_DEPLOYMENT_SHA"] == "${AGENT_DEPLOYMENT_SHA:-}"
    assert env["AGENT_SHARED_LIVE_READY_FILE"] == "/run/cloud-agent/shared-live-ready"
    marker_mount = next(
        mount
        for mount in runner["volumes"]
        if isinstance(mount, dict) and mount.get("target") == env["AGENT_SHARED_LIVE_READY_FILE"]
    )
    assert marker_mount["type"] == "bind"
    assert marker_mount["read_only"] is True
    assert marker_mount["bind"]["create_host_path"] is False
    compose_dir = REPO_ROOT / "deploy/host"
    assert (compose_dir / marker_mount["source"]).resolve() == REPO_ROOT / ".shared-live-ready"
    assert any("AGENT_HOST_STATE_ROOT" in str(mount) for mount in runner["volumes"])


def test_staging_deploy_runs_both_halves_in_parallel_with_a_runtime_fence() -> None:
    """Actions overlap, while deploy.sh still proves the target host before rollout."""
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/deploy-staging.yml").read_text())
    jobs = workflow["jobs"]
    assert "needs" not in jobs["deploy"]
    assert "needs" not in jobs["deploy-host"]
    control_deploy = next(step for step in jobs["deploy"]["steps"] if step.get("name") == "Deploy")
    host_deploy = next(
        step for step in jobs["deploy-host"]["steps"] if step.get("name") == "Deploy"
    )
    control_env = control_deploy["env"]
    host_env = host_deploy["env"]
    for job in (jobs["deploy"], jobs["deploy-host"]):
        checkouts = [step for step in job["steps"] if step.get("uses") == "actions/checkout@v6"]
        assert [step["with"]["ref"] for step in checkouts] == [
            "${{ github.workflow_sha }}",
            "${{ steps.target.outputs.sha }}",
        ]
        assert checkouts[0]["with"]["path"] == ".deploy-orchestrator"
        assert checkouts[1]["with"]["path"] == ".deploy-target"

    for env in (control_env, host_env):
        assert "AGENT_SHARED_LIVE_SANDBOX" not in env
        assert "AGENT_RESTARTABLE_SANDBOX" not in env
        assert "STAGING_SHARED_LIVE_SANDBOX" not in env
        assert env["SOURCE_REPO"].endswith("/.deploy-target")
    assert ".deploy-orchestrator/deploy/control-plane/deploy.sh" in control_deploy["run"]
    assert ".deploy-orchestrator/deploy/host/deploy.sh" in host_deploy["run"]
    assert control_env["COMPOSE_PROJECT"] == "${{ vars.STAGING_CONTROL_COMPOSE_PROJECT }}"
    assert "STAGING_HOST_SLOTS" in host_env["AGENT_HOST_SLOTS"]
    assert host_env["AGENT_RUNNER_REPLICAS"] == host_env["AGENT_HOST_SLOTS"]
    assert "STAGING_RETAINED_DOCKER_DATA_ROOT" in host_env["AGENT_RETAINED_DOCKER_DATA_ROOT"]


def test_production_deploy_uses_the_same_parallel_topology() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/deploy-prod.yml").read_text())
    jobs = workflow["jobs"]
    assert "needs" not in jobs["deploy"]
    assert "needs" not in jobs["deploy-host"]
    host_deploy = next(
        step for step in jobs["deploy-host"]["steps"] if step.get("name") == "Deploy"
    )
    control_deploy = next(step for step in jobs["deploy"]["steps"] if step.get("name") == "Deploy")
    host_env = host_deploy["env"]

    for removed in (
        "AGENT_RESTARTABLE_SANDBOX",
        "AGENT_SHARED_LIVE_SANDBOX",
        "STAGING_SHARED_LIVE_SANDBOX",
    ):
        assert removed not in host_env
    assert "PROD_HOST_SLOTS" in host_env["AGENT_HOST_SLOTS"]
    assert host_env["AGENT_RUNNER_REPLICAS"] == host_env["AGENT_HOST_SLOTS"]
    assert "PROD_RETAINED_DOCKER_DATA_ROOT" in host_env["AGENT_RETAINED_DOCKER_DATA_ROOT"]
    assert host_env["SIBLING_COMPOSE_PROJECTS"] == (
        "${{ vars.PROD_SIBLING_COMPOSE_PROJECTS || vars.STAGING_SANDBOX_COMPOSE_PROJECT }}"
    )
    assert "ALLOW_STRAY_STACK" not in host_env
    assert ".deploy-orchestrator/deploy/control-plane/deploy.sh" in control_deploy["run"]
    assert ".deploy-orchestrator/deploy/host/deploy.sh" in host_deploy["run"]


def test_staging_and_production_host_rollouts_are_independently_serialized() -> None:
    """Namespaced host stacks can roll out concurrently without cancelling peers."""
    staging = yaml.safe_load((REPO_ROOT / ".github/workflows/deploy-staging.yml").read_text())
    production = yaml.safe_load((REPO_ROOT / ".github/workflows/deploy-prod.yml").read_text())

    staging_jobs = staging["jobs"]
    production_jobs = production["jobs"]
    staging_group = staging_jobs["deploy-host"]["concurrency"]["group"]
    production_group = production_jobs["deploy-host"]["concurrency"]["group"]
    assert staging_group == "cloud-agent-host-deploy"
    assert production_group == "cloud-agent-prod-host-deploy"
    assert staging_group != production_group
    assert "concurrency" not in staging_jobs["deploy"]
    assert "concurrency" not in production_jobs["deploy"]


def test_target_commit_can_activate_staging_under_the_default_branch_workflow() -> None:
    """Readiness is unconditional and no stale feature flag can bypass it."""
    for relative in (
        "deploy/control-plane/deploy.sh",
        "deploy/host/sandbox_host.sh",
    ):
        script = (REPO_ROOT / relative).read_text()
        assert "STAGING_SHARED_LIVE_SANDBOX" not in script
        assert "AGENT_SHARED_LIVE_SANDBOX" not in script

    control_script = (REPO_ROOT / "deploy/control-plane/deploy.sh").read_text()
    assert "cloud_agent.deploy_readiness" in control_script
    assert "--expected-deployment-sha" in control_script


def test_workspace_broker_keeps_execution_off_the_public_gateway(compose: dict):
    """Only the private broker may spawn an isolated workspace command."""
    services = compose["services"]
    broker = services["workspace-broker"]
    socket_mounts = [mount for mount in broker["volumes"] if "docker.sock" in mount]

    assert socket_mounts and all(mount.endswith(":ro") for mount in socket_mounts)
    # The source additionally proved the *gateway* held no socket. There is no
    # gateway service here, so the equivalent guarantee is that the socket
    # holders are exactly the two that need it — asserted as a set in
    # test_deploy_security.py, which fails if a third service acquires one.
    assert broker["networks"] == ["internal"]


def test_chat_container_idle_timeout_is_configured(compose: dict):
    """A settled container remains inspectable until the one chat idle clock."""
    env = compose["services"]["runner"]["environment"]

    assert int(_expand(env["AGENT_CHAT_IDLE_TIMEOUT_S"])) > 0
    assert "AGENT_WORKSPACE_TTL_S" not in env


def test_the_runner_image_can_actually_start_a_sandbox():
    """The container backend shells out to `docker`; the backend image has none."""
    text = _RUNNER_DOCKERFILE.read_text()
    assert "docker:" in text and "/usr/local/bin/docker" in text


def test_sandbox_image_prepares_docker_owned_restartable_state_as_the_agent_user():
    """Retained workspace/session roots must be writable without a host bind."""
    text = _DOCKERFILE.read_text()

    create = "install -d -m 2770 -o 10001 -g 10001 /workspace /agent-state"
    assert create in text
    assert text.index(create) < text.index("USER agent")


def test_the_runner_image_can_check_a_repository_out():
    """No git in the runner means no worktree, and an agent with nothing to read."""
    assert "git" in _RUNNER_DOCKERFILE.read_text()


def test_the_runner_service_uses_the_runner_image(compose: dict):
    """Built from the backend image, the runner has neither docker nor git."""
    dockerfile = compose["services"]["runner"]["build"]["dockerfile"]
    assert dockerfile.endswith("Dockerfile.agent-runner")


def test_the_sandbox_uid_matches_what_the_runner_chowns_to():
    """The runner gives each worktree to this exact id before mounting it.

    Drift here does not fail loudly: the agent simply cannot write to its own
    working tree, and git refuses to run at all with "dubious ownership".
    """
    from cloud_agent_host.sandbox import SANDBOX_GID, SANDBOX_UID

    text = _DOCKERFILE.read_text()
    assert f"--uid {SANDBOX_UID}" in text
    assert f"--gid {SANDBOX_GID}" in text


def test_agent_runtimes_are_pinned_not_floating():
    """`latest` in the image is a dependency that changes under you between builds.

    It already cost a job: `latest` resolved to Claude Code 2.1.220, whose
    request shape the gateway rejected, and every run died at its first model
    call with an error that pointed at the model rather than the CLI version.
    """
    text = _DOCKERFILE.read_text()
    versions = dict(re.findall(r"^ARG (\w+_VERSION)=(\S+)$", text, re.MULTILINE))
    assert versions, "the image must declare runtime versions"
    for name, value in versions.items():
        assert value != "latest", f"{name} must be pinned, not 'latest'"
        assert re.fullmatch(r"\d+\.\d+\.\d+", value), f"{name}={value} is not an exact version"


def test_the_agent_phase_is_closed_in_the_shipped_configuration(compose: dict):
    """The turn driven by untrusted model output reaches the gateway and nothing else.

    This is the differentiator, not an implementation detail: the setup phase
    may be raised to an allowlist, but the phase that executes what a model
    decided to do stays on a network with no route. A default that opened it
    would be the one change here nobody would notice from the outside.
    """
    env = compose["services"]["runner"]["environment"]
    # And the spawn network tracks the same variable the network block names.
    assert _expand(env["AGENT_EGRESS_NETWORK_PLATFORM_ONLY"]) == _expand(
        compose["networks"]["agent-egress"]["name"]
    )
    assert "platform_only" in env["AGENT_EGRESS_AGENT_TIER"]


def test_the_setup_tier_the_overlay_selects_is_one_it_actually_ships(compose: dict):
    """Naming `trusted` obliges this file to ship the thing that honours it.

    The failure this prevents is a config that reads as "dependencies can be
    installed" over a network that reaches only the gateway — every setup
    script then dies at DNS, and nothing in the compose file looks wrong. The
    tier needs three things present together, so they are asserted together.
    """
    env = compose["services"]["runner"]["environment"]
    if "trusted" not in _expand(env["AGENT_EGRESS_SETUP_TIER"]):
        pytest.skip("this overlay does not select the trusted tier")

    trusted = _expand(env["AGENT_EGRESS_NETWORK_TRUSTED"])
    assert trusted, "the trusted tier is selected but names no network"
    declared = {
        _expand(network.get("name", name)): network for name, network in compose["networks"].items()
    }
    assert trusted in declared, f"{trusted} is selected but this file does not create it"

    # Internal like the closed tier. The difference between the two is the
    # proxy on it, not a route — a routable network here would hand the setup
    # phase the whole internet while still being called "trusted".
    assert declared[trusted]["internal"] is True

    proxy = compose["services"]["agent-egress-proxy"]
    assert trusted in [_expand(n) for n in proxy["networks"]], (
        "the proxy is not on the network whose traffic it is supposed to police"
    )
    for relay in ("gateway-tunnel", "mcp-tunnel"):
        assert "agent-egress-trusted" in compose["services"][relay]["networks"], (
            f"{relay} is absent from trusted discovery, so NO_PROXY makes its "
            "platform URL resolve to nowhere"
        )
    # And the runner must actually tell the sandbox to use it.
    url = _expand(env["AGENT_EGRESS_PROXY_URL_TRUSTED"])
    assert url.startswith("http://"), "the trusted tier is selected with no proxy URL"


def test_the_proxy_url_names_the_port_the_generated_config_listens_on(compose: dict):
    """Two places hold one number, so they are checked against each other.

    A mismatch is invisible in review and total at run time: Squid listens on
    one port, every sandbox dials another, and the first symptom is that no
    dependency install has ever worked.
    """
    from cloud_agent_host.egress_proxy import DEFAULT_PROXY_PORT

    env = compose["services"]["runner"]["environment"]
    assert _expand(env["AGENT_EGRESS_PROXY_URL_TRUSTED"]).endswith(f":{DEFAULT_PROXY_PORT}")


def test_the_proxy_cannot_reach_the_rest_of_the_stack(compose: dict):
    """A Squid reachable by an untrusted sandbox must not also see the database.

    The proxy is the one component here that takes input from inside the
    sandbox and holds a route to the internet. Putting it on `hybridinference`
    for convenience would mean a Squid compromise reaches Postgres.
    """
    proxy = compose["services"]["agent-egress-proxy"]
    assert "internal" not in proxy["networks"]
    assert not proxy.get("volumes") or all(
        "docker.sock" not in volume for volume in proxy["volumes"]
    )

    uplink = [n for n in proxy["networks"] if "uplink" in n]
    assert uplink, "the proxy has no routable leg, so it can police nothing"
    declared = compose["networks"][uplink[0]]
    assert not declared.get("internal"), "the proxy's uplink must actually route out"
    on_uplink = [
        name
        for name, service in compose["services"].items()
        if uplink[0] in (service.get("networks") or [])
    ]
    assert on_uplink == ["agent-egress-proxy"], (
        f"the routable network must hold only the proxy, but also holds {on_uplink}"
    )


def test_the_proxy_config_is_generated_by_the_validating_renderer(compose: dict):
    """The allowlist reaches Squid's config only through code that checks it.

    A domain carrying a newline appends directives to the file that decides
    what an untrusted sandbox may reach. The renderer refuses those; a
    hand-written config, or a shell loop in an entrypoint, would not.
    """
    config_service = compose["services"]["agent-egress-proxy-config"]
    assert "cloud_agent_host.egress_proxy" in config_service["command"]
    assert "AGENT_EGRESS_ALLOWLIST" in config_service["environment"]

    # It renders a file and must not be able to do anything else.
    assert config_service["network_mode"] == "none"

    proxy = compose["services"]["agent-egress-proxy"]
    assert proxy["depends_on"]["agent-egress-proxy-config"]["condition"] == (
        "service_completed_successfully"
    ), "the proxy may start before its config exists, i.e. with no config at all"
    fingerprint = proxy["environment"]["AGENT_EGRESS_CONFIG_FINGERPRINT"]
    assert "AGENT_EGRESS_ALLOWLIST" in fingerprint
    assert "AGENT_EGRESS_PROXY_PORT" in fingerprint
    assert any(volume.endswith(":ro") for volume in proxy["volumes"]), (
        "the proxy can rewrite the allowlist it is enforcing"
    )


def test_runner_and_broker_share_the_private_network_pool(compose: dict):
    runner_pool = compose["services"]["runner"]["environment"]["AGENT_EGRESS_CHAT_NETWORK_POOL"]
    broker_pool = compose["services"]["workspace-broker"]["environment"][
        "AGENT_EGRESS_CHAT_NETWORK_POOL"
    ]

    assert runner_pool == broker_pool
    assert runner_pool.endswith("/16}")
    runner_namespace = compose["services"]["runner"]["environment"]["AGENT_SANDBOX_NAMESPACE"]
    broker_namespace = compose["services"]["workspace-broker"]["environment"][
        "AGENT_SANDBOX_NAMESPACE"
    ]
    assert runner_namespace == broker_namespace
    assert "COMPOSE_PROJECT" in runner_namespace


def test_complete_local_stack_namespaces_daemon_wide_chat_discovery(local_compose: dict):
    broker_namespace = local_compose["services"]["workspace-broker"]["environment"][
        "AGENT_SANDBOX_NAMESPACE"
    ]
    assert "LOCAL_PROJECT_NAME" in broker_namespace
    # Runner inherits the exact same anchored environment.
    assert (
        local_compose["services"]["runner"]["environment"]["AGENT_SANDBOX_NAMESPACE"]
        == broker_namespace
    )


def test_the_runner_reads_the_bounds_the_overlay_configures(monkeypatch, compose: dict):
    """A configured lease and timeout must actually reach the runner.

    The overlay set both and the runner read neither, so every self-hosted job
    ran on the built-in defaults no matter what the operator wrote — a setting
    that looks applied and is not.
    """
    env = compose["services"]["runner"]["environment"]
    assert "AGENT_LEASE_TTL" in env and "AGENT_TIMEOUT_S" in env

    monkeypatch.setenv("AGENT_LEASE_TTL", "45")
    monkeypatch.setenv("AGENT_TIMEOUT_S", "600")
    args = build_parser().parse_args([])

    assert args.lease_ttl == 45.0
    assert args.agent_timeout == 600.0


def test_the_runner_reads_the_neutral_gateway_variable(monkeypatch):
    """The self-hosted runner uses the same gateway variable as compose."""
    monkeypatch.setenv("AGENT_GATEWAY_URL", "https://gateway.example.com")

    args = build_parser().parse_args([])

    assert args.base_url == "https://gateway.example.com"


def test_the_runner_keeps_the_deployment_gateway_alias(monkeypatch):
    """Existing hosts keep working while they migrate to the neutral name."""
    monkeypatch.delenv("AGENT_GATEWAY_URL", raising=False)
    monkeypatch.setenv("FREEINFERENCE_BASE_URL", "https://legacy-gateway.example.com")

    args = build_parser().parse_args([])

    assert args.base_url == "https://legacy-gateway.example.com"


def test_an_unparsable_bound_falls_back_rather_than_crash_looping(monkeypatch):
    """A typo in an env var must not take the runner down on every restart."""
    monkeypatch.setenv("AGENT_LEASE_TTL", "two minutes")
    args = build_parser().parse_args([])
    assert args.lease_ttl > 0


def test_every_tier_the_overlay_lets_you_select_is_passed_through(compose: dict):
    """Selecting a tier must also deliver that tier's network to the container.

    The overlay offered the tier variables while passing through only the
    `platform_only` network, so an operator who set
    AGENT_EGRESS_AGENT_TIER=custom *and* AGENT_EGRESS_NETWORK_CUSTOM in their
    .env got the first and not the second — and the runner refused at preflight
    with "names no network" for a variable they had plainly set. Found by
    running the overlay, not by reading it.
    """
    env = compose["services"]["runner"]["environment"]
    selectable = {
        "platform_only": "PLATFORM_ONLY",
        "trusted": "TRUSTED",
        "custom": "CUSTOM",
        "full": "FULL",
    }
    for tier, suffix in selectable.items():
        assert f"AGENT_EGRESS_NETWORK_{suffix}" in env, (
            f"tier {tier!r} is selectable but its network variable never reaches the runner"
        )


_STAGING_DEPLOY = Path(__file__).resolve().parents[2] / "ops/deploy/deploy_staging.sh"
_MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"


# `test_staging_deploy_owns_the_runner_when_the_host_opts_in` is not ported.
# It drives `ops/deploy/deploy_staging.sh`, which deploys the gateway and stays
# in that repository; there is nothing here for it to assert against. Its
# subject — that a host opting into running agents gets the runner started —
# becomes this repository's own deploy story, not a check on the gateway's.


def test_compose_file_has_no_duplicate_keys():
    """Duplicate mapping keys make docker compose reject the whole file.

    `yaml.safe_load` keeps the last of a repeated key, so every test in this
    module passed while `docker compose` refused to parse the overlay at all
    ("mapping key already defined") — the runner could not have started from
    it on any host. Python's tolerance is what hid it; this loader is as
    strict as the Go parser that actually reads the file.
    """

    class _StrictLoader(yaml.SafeLoader):
        pass

    def _no_duplicates(loader, node, deep=False):
        seen = set()
        for key_node, _value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise AssertionError(
                    f"duplicate key {key!r} in {_COMPOSE.name}: docker compose "
                    "rejects the entire file, so the runner never starts"
                )
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    _StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)
    yaml.load(_COMPOSE.read_text(), Loader=_StrictLoader)


_PI_WRAPPER = Path(__file__).resolve().parents[2] / "deploy/sandbox/pi-freeinference"


def test_pi_wrapper_writes_the_provider_config_and_execs_pi(tmp_path):
    """The wrapper turns its environment into pi's models.json, argv untouched.

    Executed for real rather than read: a stub ``pi`` on PATH records the argv
    it receives, HOME is a temp dir, and the assertions read the exact file the
    real pi would read. pi ignores OPENAI_BASE_URL, so this file is the ONLY
    thing standing between a job and api.openai.com.
    """
    import json as _json
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    record = tmp_path / "argv.json"
    stub = stub_dir / "pi"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
    )
    stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    activation = tmp_path / "activation" / "pi"

    prompt = 'prompt; with $(dangerous) `chars` "quoted"'
    result = _subprocess.run(
        [_sys.executable, str(_PI_WRAPPER), "--provider", "freeinference", "-p", prompt],
        env={
            "PATH": f"{stub_dir}:{_os.environ['PATH']}",
            "HOME": str(home),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            "OPENAI_API_KEY": "ajt.attempt.key",
            "PI_GATEWAY_MODEL": "glm-5.1",
            "PI_CODING_AGENT_DIR": str(activation),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    provider = _json.loads((activation / "models.json").read_text())["providers"]["freeinference"]
    assert provider["baseUrl"] == "http://backend:8080/v1"
    assert provider["apiKey"] == "ajt.attempt.key"
    assert provider["api"] == "openai-completions"
    assert provider["models"] == [{"id": "glm-5.1"}]
    assert not (home / ".pi/agent/models.json").exists()
    # argv passed through byte-for-byte: the shell-hostile prompt survived.
    assert _json.loads(record.read_text()) == [
        "--provider",
        "freeinference",
        "-p",
        prompt,
    ]


def test_pi_wrapper_refuses_to_run_half_configured(tmp_path):
    """Missing environment is a named refusal, not a job that dials OpenAI."""
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    result = _subprocess.run(
        [_sys.executable, str(_PI_WRAPPER), "-p", "hi"],
        env={
            "PATH": _os.environ["PATH"],
            "HOME": str(tmp_path),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            # OPENAI_API_KEY and PI_GATEWAY_MODEL deliberately absent.
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 64
    assert "OPENAI_API_KEY" in result.stderr


_OPENCODE_WRAPPER = Path(__file__).resolve().parents[2] / "deploy/sandbox/opencode-freeinference"


def test_opencode_wrapper_writes_config_sets_offline_flags_and_execs(tmp_path):
    """The wrapper produces OpenCode's config and kills its phone-home paths.

    A stub ``opencode`` on PATH records argv and the environment it received.
    The offline flags are the load-bearing part: without
    OPENCODE_DISABLE_MODELS_FETCH the CLI hard-fails fetching models.dev,
    which in the deny-all sandbox is every single run.
    """
    import json as _json
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    record = tmp_path / "seen.json"
    stub = stub_dir / "opencode"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "seen = {'argv': sys.argv[1:], 'env': {k: v for k, v in os.environ.items()"
        " if k.startswith('OPENCODE_')}}\n"
        f"open({str(record)!r}, 'w').write(json.dumps(seen))\n"
    )
    stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    activation = tmp_path / "activation" / "opencode"

    prompt = 'fix; the $(bug) "carefully"'
    result = _subprocess.run(
        [
            _sys.executable,
            str(_OPENCODE_WRAPPER),
            "run",
            "--format",
            "json",
            "--auto",
            "-m",
            "freeinference/glm-5.1",
            prompt,
        ],
        env={
            "PATH": f"{stub_dir}:{_os.environ['PATH']}",
            "HOME": str(home),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            "OPENAI_API_KEY": "ajt.attempt.key",
            "OPENCODE_GATEWAY_MODEL": "glm-5.1",
            "OPENCODE_FREEINFERENCE_CONFIG_DIR": str(activation),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    seen = _json.loads(record.read_text())
    assert seen["argv"] == [
        "run",
        "--format",
        "json",
        "--auto",
        "-m",
        "freeinference/glm-5.1",
        prompt,
    ]
    assert seen["env"]["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
    assert seen["env"]["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"
    assert seen["env"]["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    # OpenCode v1.18.9's supported switch prevents both project opencode.json
    # and project .opencode/ directories from entering the merged config.
    assert seen["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"

    config = _json.loads(Path(seen["env"]["OPENCODE_CONFIG"]).read_text())
    provider = config["provider"]["freeinference"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://backend:8080/v1"
    assert provider["options"]["apiKey"] == "ajt.attempt.key"
    assert provider["models"] == {"glm-5.1": {"name": "glm-5.1"}}
    # Credential-bearing config follows the runtime's activation-only path.
    assert seen["env"]["OPENCODE_CONFIG"].startswith(str(activation))


def test_opencode_wrapper_refuses_to_run_half_configured(tmp_path):
    """Missing environment is a named refusal, not a run that dials out."""
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    result = _subprocess.run(
        [_sys.executable, str(_OPENCODE_WRAPPER), "run", "hi"],
        env={
            "PATH": _os.environ["PATH"],
            "HOME": str(tmp_path),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            # OPENAI_API_KEY and OPENCODE_GATEWAY_MODEL deliberately absent.
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 64
    assert "OPENAI_API_KEY" in result.stderr


_KILO_WRAPPER = Path(__file__).resolve().parents[2] / "deploy/sandbox/kilo-freeinference"


def test_kilo_wrapper_writes_config_sets_offline_flags_and_execs(tmp_path):
    """The wrapper produces Kilo's config and kills its phone-home paths.

    A stub ``kilo`` on PATH records argv and the environment it received.
    Kilo is an OpenCode fork that renamed the kill switches to ``KILO_*`` and
    added its own phone-home paths (PostHog telemetry, session ingest and
    share links to app.kilo.ai); without KILO_DISABLE_MODELS_FETCH the CLI
    hard-fails fetching models.dev, which in the deny-all sandbox is every
    single run.
    """
    import json as _json
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    record = tmp_path / "seen.json"
    stub = stub_dir / "kilo"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "seen = {'argv': sys.argv[1:], 'env': {k: v for k, v in os.environ.items()"
        " if k.startswith('KILO_') or k == 'DO_NOT_TRACK'}}\n"
        f"open({str(record)!r}, 'w').write(json.dumps(seen))\n"
    )
    stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    activation = tmp_path / "activation" / "kilo"

    prompt = 'fix; the $(bug) "carefully"'
    result = _subprocess.run(
        [
            _sys.executable,
            str(_KILO_WRAPPER),
            "run",
            "--format",
            "json",
            "--auto",
            "-m",
            "freeinference/glm-5.1",
            prompt,
        ],
        env={
            "PATH": f"{stub_dir}:{_os.environ['PATH']}",
            "HOME": str(home),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            "OPENAI_API_KEY": "ajt.attempt.key",
            "KILO_GATEWAY_MODEL": "glm-5.1",
            "KILO_FREEINFERENCE_CONFIG_DIR": str(activation),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    seen = _json.loads(record.read_text())
    assert seen["argv"] == [
        "run",
        "--format",
        "json",
        "--auto",
        "-m",
        "freeinference/glm-5.1",
        prompt,
    ]
    # The OpenCode switches, under the fork's renamed prefix.
    assert seen["env"]["KILO_DISABLE_MODELS_FETCH"] == "1"
    assert seen["env"]["KILO_DISABLE_DEFAULT_PLUGINS"] == "1"
    assert seen["env"]["KILO_DISABLE_AUTOUPDATE"] == "1"
    assert seen["env"]["KILO_DISABLE_PROJECT_CONFIG"] == "1"
    # The fork's own phone-home paths, each with its supported switch.
    assert seen["env"]["KILO_DISABLE_SESSION_INGEST"] == "1"
    assert seen["env"]["KILO_DISABLE_SHARE"] == "1"
    assert seen["env"]["KILO_DISABLE_PRESENCE"] == "1"
    assert seen["env"]["KILO_DISABLE_LSP_DOWNLOAD"] == "1"
    assert seen["env"]["KILO_TELEMETRY_LEVEL"] == "off"
    assert seen["env"]["DO_NOT_TRACK"] == "1"

    config = _json.loads(Path(seen["env"]["KILO_CONFIG"]).read_text())
    provider = config["provider"]["freeinference"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://backend:8080/v1"
    assert provider["options"]["apiKey"] == "ajt.attempt.key"
    assert provider["models"] == {"glm-5.1": {"name": "glm-5.1"}}
    # Credential-bearing config follows the runtime's activation-only path.
    assert seen["env"]["KILO_CONFIG"].startswith(str(activation))


def test_kilo_wrapper_refuses_to_run_half_configured(tmp_path):
    """Missing environment is a named refusal, not a run that dials out."""
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    result = _subprocess.run(
        [_sys.executable, str(_KILO_WRAPPER), "run", "hi"],
        env={
            "PATH": _os.environ["PATH"],
            "HOME": str(tmp_path),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            # OPENAI_API_KEY and KILO_GATEWAY_MODEL deliberately absent.
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 64
    assert "OPENAI_API_KEY" in result.stderr


def test_concurrency_is_declared_not_typed_at_deploy_time(compose: dict):
    """One runner takes one job at a time, so replicas *is* the concurrency.

    A `--scale` passed by hand survives until the next `docker compose up`
    without it, which silently drops the fleet back to one — presenting as
    "every user is queueing" long after anyone remembers scaling it. The count
    therefore belongs in the file the deploy reads.
    """
    runner = compose["services"]["runner"]
    replicas = (runner.get("deploy") or {}).get("replicas")
    assert replicas, "runner declares no replica count, so a deploy resets concurrency to 1"
    assert "AGENT_RUNNER_REPLICAS" in str(replicas), "the replica count is not operator-tunable"


def test_replicas_are_distinguishable_in_the_lease_ledger(compose: dict):
    """Replicas must not all claim jobs under the same lease_owner.

    Fencing never reads this string, so a collision is not a correctness bug —
    it just makes "which runner is stuck" unanswerable exactly when a second
    replica makes it worth asking.
    """
    command = " ".join(compose["services"]["runner"].get("command") or [])
    assert "--worker-id" not in command, (
        "a literal --worker-id pins every replica to the same lease_owner; "
        "let the runner derive one that includes its hostname"
    )


def test_runner_derives_a_distinct_worker_id_per_container(monkeypatch):
    """The derived id groups by configuration and separates by host."""
    from cloud_agent_host.runner import default_worker_id

    monkeypatch.setattr("socket.gethostname", lambda: "abc123")
    monkeypatch.delenv("AGENT_WORKER_ID", raising=False)
    assert default_worker_id() == "runner-abc123"

    monkeypatch.setenv("AGENT_WORKER_ID", "staging")
    assert default_worker_id() == "staging-abc123"


# ── Remote runner host (no gateway on this machine) ────────────────────

_REMOTE_COMPOSE = REPO_ROOT / "deploy/host/docker-compose.sandbox.yml"
_TUNNEL_ENTRYPOINT = Path(__file__).resolve().parents[2] / "deploy/host/agent-gateway-tunnel.sh"


@pytest.fixture(scope="module")
def remote_compose() -> dict:
    """Parse the standalone remote-runner compose file."""
    return yaml.safe_load(_REMOTE_COMPOSE.read_text())


def test_remote_compose_starts_no_gateway(remote_compose: dict):
    """The whole reason this file exists separately from the overlay.

    `docker-compose.agent-runner.yml` layers on the main stack and its runner
    declares `depends_on: backend`, so aiming it at a machine with no gateway
    starts one there — on a box meant only to run jobs.
    """
    assert set(remote_compose["services"]) == {
        # Three tunnels, one per destination: the gateway for model calls, the
        # MCP relay for tools, the control plane for claims. Splitting them is
        # what keeps "may call models" and "may create jobs" separate
        # permissions; see `test_deploy_security.py` for which reaches what.
        "gateway-tunnel",
        "mcp-tunnel",
        "control-plane-tunnel",
        # Setup dependency traffic has its own allowlist proxy and an offline
        # renderer; neither is a gateway or control-plane service.
        "agent-egress-proxy-config",
        "agent-egress-proxy",
        "workspace-broker",
        "runner",
    }
    for name, service in remote_compose["services"].items():
        assert "backend" not in (service.get("depends_on") or []), name


def test_remote_sandbox_network_is_still_closed(remote_compose: dict):
    """A remote host must not buy reachability by opening the sandbox up."""
    assert remote_compose["networks"]["agent-egress"]["internal"] is True


def test_only_the_two_relays_are_on_both_networks(remote_compose: dict):
    """Everything else is either closed in with the sandbox or outside it.

    Two now, not one: MCP moved to the cloud agent's own relay, so a sandbox
    has two narrow destinations — the gateway for model calls, the relay for
    tools. Named exactly, because the natural way to make this pass after
    adding a service is to loosen it to "at least one", and that would let the
    next addition sit on the closed network without a word.
    """
    on_egress = {
        name
        for name, service in remote_compose["services"].items()
        if "agent-egress" in (service.get("networks") or {})
    }
    assert on_egress == {"gateway-tunnel", "mcp-tunnel"}


def test_runner_and_sandbox_are_pointed_at_the_same_name(remote_compose: dict):
    """They share one AGENT_GATEWAY_URL, so one name has to serve both.

    The runner claims over it and the sandbox is handed the same value; a URL
    that only the runner can resolve means jobs are claimed and then die at
    their first model call.
    """
    alias = remote_compose["services"]["gateway-tunnel"]["networks"]["agent-egress"]["aliases"]
    assert alias == ["${AGENT_GATEWAY_HOSTNAME:-agent-gateway}"]
    url = remote_compose["services"]["runner"]["environment"]["AGENT_GATEWAY_URL"]
    assert "${AGENT_GATEWAY_HOSTNAME:-agent-gateway}" in url


def test_the_tunnel_credential_is_mounted_read_only(remote_compose: dict):
    volumes = remote_compose["services"]["gateway-tunnel"]["volumes"]
    assert all(volume.endswith(":ro") for volume in volumes), volumes


def test_the_tunnel_refuses_to_skip_host_verification():
    """Encrypting the hop is pointless if anything may answer for the gateway.

    The credentials this tunnel exists to protect — the enrolled-host token and
    every job capability — would go straight to an impostor.
    """
    script = _TUNNEL_ENTRYPOINT.read_text()
    assert "StrictHostKeyChecking=yes" in script
    assert "StrictHostKeyChecking=no" not in script
    assert "UserKnownHostsFile" in script
    # And it must refuse to start without one, rather than quietly trusting
    # whatever answers the first time.
    assert re.search(r'\[ -s "\$KNOWN_HOSTS" \] \|\| die', script)


def test_the_tunnel_dies_rather_than_listening_on_nothing():
    """Without this, ssh stays up having bound no port and every sandbox gets
    connection-refused against a container that looks healthy."""
    assert "ExitOnForwardFailure=yes" in _TUNNEL_ENTRYPOINT.read_text()


def test_the_tunnel_forwards_one_fixed_destination():
    """A relay, not a proxy: nothing the caller sends chooses where it goes."""
    script = _TUNNEL_ENTRYPOINT.read_text()
    assert "-L " in script
    for proxy_flag in ("-D ", "DynamicForward", "ProxyCommand"):
        assert proxy_flag not in script, proxy_flag


def test_the_egress_proxy_does_not_inherit_the_docker_stop_timeout(compose: dict) -> None:
    """Squid winds down on SIGTERM, and this container is replaced every deploy.

    Left unset it takes the Docker default of ten seconds: measured on two
    consecutive staging rollouts at 10.30s and 10.31s, against 0.1-0.3s for
    every other container in this stack — and it sits upstream of the runners
    in the dependency order, so the whole rollout waited on it.
    """
    declared = str(compose["services"]["agent-egress-proxy"]["stop_grace_period"])
    default = re.search(r":-(\d+)s\}", declared)

    assert default, (
        "the egress proxy declares no bounded stop_grace_period, so a Squid that "
        f"does not exit on SIGTERM costs the ten-second Docker default: {declared!r}"
    )
    assert int(default.group(1)) <= 5, (
        f"a stateless egress proxy does not need {default.group(1)} to drain; "
        "the runners it serves are already stopped by then"
    )


def test_deployment_builds_disable_provenance_attestations() -> None:
    """A regenerated attestation makes an unchanged rebuild a new image.

    Buildx attaches provenance by default and rewrites it on every build, so an
    image's manifest index moves while the image itself does not. A daemon with
    the containerd image store identifies an image by that index, and Compose
    then treats every image it just rebuilt as changed: it replaces all eight
    containers of the sandbox stack on every deploy — 10.3s of that the egress
    proxy sitting out Docker's stop timeout — and the digest a retained group
    pinned stops resolving. Measured on the real runner image with every layer
    CACHED: two builds with the default give two ids, two with the flag give one.

    Asserted on the command lines because the flag changes nothing until it is
    missing on a host with that store, and because the compose file's
    `build.provenance` is the obvious place someone will move it to — a place
    `docker compose config` accepts and `docker compose build` ignores.
    """
    host = (REPO_ROOT / "deploy/host/sandbox_host.sh").read_text()
    control = _CONTROL_PLANE_DEPLOY.read_text()

    # `in`, not `startswith`: the host passes its image and replica count as a
    # command prefix, so the line can legitimately read
    # `AGENT_SANDBOX_IMAGE=… compose build …`. It happens to be a continuation
    # line today, which is the only reason `startswith` finds it — joining those
    # two lines, an ordinary tidy-up, would retire this assertion in silence.
    # Comments are skipped because three of them name these commands while
    # explaining the flag.
    compose_builds = [
        line.strip()
        for line in (host + control).splitlines()
        if "compose build" in line and not line.strip().startswith("#")
    ]
    assert compose_builds, "no compose build command found — has it moved?"
    for line in compose_builds:
        assert "--provenance=false" in line, f"compose build without the flag: {line!r}"

    sandbox_builds = [
        line.strip()
        for line in host.splitlines()
        if "docker build" in line and not line.strip().startswith("#")
    ]
    assert sandbox_builds, "no sandbox image build found — has it moved?"
    for line in sandbox_builds:
        assert "--provenance=false" in line, f"sandbox image build without the flag: {line!r}"

    for compose in (_COMPOSE, _CONTROL_PLANE_COMPOSE):
        # The *key*, not the word: a comment recording why `build.provenance`
        # cannot be used here is exactly what the next reader needs, and a
        # substring check would fail the file for carrying it.
        assert not re.search(r"^\s*provenance\s*:", compose.read_text(), re.MULTILINE), (
            f"{compose.name} sets build.provenance, which compose accepts and then ignores; "
            "the flag has to be on the build command"
        )
